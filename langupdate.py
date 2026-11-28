"""Oficiální překlady z GitHub Releases: stažení a aktualizace se zachováním úprav uživatele.

Každé vydání má mezi soubory překlady (cs.yaml …) a v podepsaném latest.json jejich SHA-256 a velikost
(tools/release.py). Stažený překlad se použije, jen když k podpisu sedí – texty se uživateli zobrazují.

Soubor překladu v podsložce languages datové složky si uživatel může upravit. V meta má app_version:
verzi aplikace, ke které patří oficiální znění, ze kterého vychází. Po aktualizaci aplikace (app_version
≠ běžící verze) se soubor sloučí s překladem nové verze:
  * text, který se shoduje s předchozím oficiálním zněním (překlad vydání app_version), uživatel neměnil
    → nové znění;
  * jinak je to úprava uživatele a zůstane;
  * nové texty přibudou, zrušené (neupravené) zmizí.
Soubor bez app_version (z doby před touto funkcí, nebo ručně stažený) se porovná se všemi vydáními, která
překlad mají. Předchozí znění slouží jen k porovnání, nic se z něj nezobrazuje. Před přepsáním se soubor
zazálohuje jako _<kód>.yaml.bak (soubory s podtržítkem aplikace nenačítá).
"""

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

import i18n
import updater
from version import VERSION

log = logging.getLogger(__name__)

FIRST_CHECK = 10          # s po startu
RETRY = 3600              # po neúspěchu (bez internetu …) znovu za hodinu
MAX_RELEASES_JSON = 4 * 1024 * 1024

_lock = threading.Lock()
_manifest = None          # ověřený latest.json běžící verze (cache)
generation = 0            # zvýší se po každé aktualizaci překladu (rozhraní si katalog načte znovu)


class LanguageError(updater.UpdateError):
    pass


# --------------------------------------------------------------------------
# Stahování
# --------------------------------------------------------------------------

def _get(url, limit) -> bytes:
    return updater._fetch(url, limit)


def manifest() -> dict:
    """Ověřený latest.json vydání běžící verze."""
    global _manifest
    if _manifest is None:
        _manifest = updater.fetch_release_manifest(VERSION)
    return _manifest


def official() -> dict:
    """Oficiální překlady pro běžící verzi: {kód: {name, sha256, size}}."""
    return manifest()["languages"]


def _parse(data: bytes):
    try:
        return i18n.validate(i18n.parse(data.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        raise LanguageError("err.language.badFile") from None


def fetch(version, code, expect=None):
    """(meta, texty) překladu z vydání; expect = {sha256, size} z podepsaného latest.json."""
    data = _get(updater.release_url(version, f"{code}.yaml"), i18n.MAX_FILE)
    if expect and (len(data) != expect["size"] or hashlib.sha256(data).hexdigest() != expect["sha256"]):
        raise LanguageError("err.language.badHash")
    return _parse(data)


def _base(version, code):
    """Texty předchozího oficiálního znění (vydání version); ověří se, pokud vydání kontrolní součty má."""
    try:
        expect = updater.fetch_release_manifest(version)["languages"].get(code)
    except updater.UpdateError as e:
        if e.key == "err.update.badSignature":
            raise
        expect = None  # starší vydání nemá podepsaný latest.json mezi soubory – jen k porovnání, nevadí
    return fetch(version, code, expect)[1]


def all_bases(code) -> list:
    """Texty překladu ze všech vydání na GitHubu, která ho mají (soubor bez app_version)."""
    try:
        data = _get(f"https://api.github.com/repos/{updater.REPO}/releases?per_page=100", MAX_RELEASES_JSON)
        releases = json.loads(data.decode("utf-8"))
    except (updater.UpdateError, UnicodeDecodeError, ValueError):
        return []
    out = []
    for r in releases if isinstance(releases, list) else []:
        tag = str(r.get("tag_name") or "") if isinstance(r, dict) else ""
        version = tag[1:] if tag.startswith("v") else ""
        if not updater.parse_version(version):
            continue
        if not any(isinstance(a, dict) and a.get("name") == f"{code}.yaml" for a in r.get("assets") or []):
            continue
        try:
            out.append(fetch(version, code)[1])
        except updater.UpdateError:
            continue
    return out


# --------------------------------------------------------------------------
# Sloučení a zápis
# --------------------------------------------------------------------------

def merge(user: dict, bases: list, new: dict) -> dict:
    """Texty uživatele × předchozí oficiální znění (bases) × nové znění → sloučené texty (pořadí nového)."""
    def unchanged(k, v):
        return any(k in b and b[k] == v for b in bases)
    out = {}
    for k, v in new.items():
        u = user.get(k)
        out[k] = v if u is None or unchanged(k, u) else u
    for k, u in user.items():
        if k not in new and not unchanged(k, u):  # vlastní text uživatele (zrušený oficiální se zahodí)
            out[k] = u
    return out


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


def dump(meta: dict, texts: dict) -> str:
    """Katalog jako text souboru (podmnožina YAML, kterou čte i18n.parse)."""
    lines = [f"# {meta.get('name') or meta['language']} – překlad Norman Mail",
             "# Upravené texty se při aktualizaci aplikace zachovají, ostatní se aktualizují.", "meta:"]
    lines += [f"  {k}: {_quote(meta[k])}" for k in i18n.META_FIELDS if meta.get(k)]
    lines.append("")
    for k, v in texts.items():
        if isinstance(v, dict):
            lines.append(f"{k}:")
            lines += [f"  {form}: {_quote(v[form])}" for form in i18n.PLURAL_FORMS if form in v]
        else:
            lines.append(f"{k}: {_quote(v)}")
    return "\n".join(lines) + "\n"


def _write(path: Path, meta, texts):
    """Zápis (s předchozí zálohou _<kód>.yaml.bak) přes dočasný soubor, ať se soubor nepoškodí."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(f"_{path.stem}.yaml.bak"))
    tmp = path.with_name(f"_{path.stem}.yaml.tmp")
    tmp.write_text(dump(meta, texts), "utf-8")
    i18n.load_file(tmp)  # pojistka: zapsaný soubor musí jít načíst
    os.replace(tmp, path)


def _updated():
    global generation
    i18n.reload()
    generation += 1


def update_file(path: Path) -> bool:
    """Aktualizuje soubor překladu na znění běžící verze (viz popis modulu). True = soubor se změnil."""
    meta, user = i18n.load_file(path)
    code = meta["language"]
    expect = official().get(code)
    if expect is None or meta.get("app_version") == VERSION:
        return False
    new_meta, new = fetch(VERSION, code, expect)
    old = meta.get("app_version")
    bases = []
    if old and updater.parse_version(old):
        try:
            bases = [_base(old, code)]
        except updater.UpdateError as e:
            if e.key != "err.update.notFound":
                raise
    if not bases:  # bez verze, nebo předchozí vydání chybí
        bases = all_bases(code)
    if not bases:
        # bez předchozího znění nejde poznat úpravy uživatele; zapsat app_version by staré texty natrvalo
        # vydávalo za úpravy – raději počkat (zkusí se znovu)
        raise LanguageError("err.language.noBase")
    out_meta = {**new_meta, "app_version": VERSION}
    if meta.get("author"):
        out_meta["author"] = meta["author"]
    _write(path, out_meta, merge(user, bases, new))
    log.info("Překlad %s aktualizován na verzi %s (%s)", code, VERSION, path.name)
    return True


def update_all() -> bool:
    """Aktualizuje všechny oficiální překlady ve složce s jazyky. Vrací True, když se nic nepokazilo."""
    with _lock:
        try:
            langs = official()
        except updater.UpdateError as e:
            if e.key == "err.update.notFound":
                return True  # vydání běžící verze není (vývoj) – nic k aktualizaci
            log.warning("Překlady: latest.json nejde načíst: %s %s", e.key, e.params)
            return False
        if not langs:
            return True
        ok, changed = True, False
        try:
            files = sorted(i18n.user_dir().glob("*.yaml"))
        except OSError:
            files = []
        for f in files:
            if f.name.startswith("_"):
                continue
            try:
                changed |= update_file(f)
            except (OSError, ValueError) as e:  # vadný soubor uživatele (ukáže Nastavení) – nechat být
                log.warning("Překlad %s nejde aktualizovat: %s", f.name, e)
            except updater.UpdateError as e:
                log.warning("Překlad %s nejde aktualizovat: %s %s", f.name, e.key, e.params)
                ok = False
        if changed:
            _updated()
        return ok


def install(code) -> Path:
    """Stáhne oficiální překlad (Nastavení → výběr jazyka). Existující soubor se jen aktualizuje."""
    with _lock:
        expect = official().get(code)
        if expect is None:
            raise LanguageError("err.language.notOfficial")
        path = i18n.user_dir() / f"{code}.yaml"
        if path.exists():
            update_file(path)
        else:
            meta, texts = fetch(VERSION, code, expect)
            _write(path, {**meta, "app_version": VERSION}, texts)
        _updated()
        return path


def start(enabled):
    """Na pozadí po startu: aktualizace překladů (jen se zapnutými aktualizacemi; i při spuštění ze zdrojáků –
    verze bez vydání na GitHubu se tiše přeskočí). Při neúspěchu se zkouší znovu."""
    def loop():
        time.sleep(FIRST_CHECK)
        while True:
            try:
                if enabled() and update_all():
                    return
            except Exception as e:  # noqa: BLE001 – nesmí shodit aplikaci
                log.warning("Aktualizace překladů selhala: %s", e)
            time.sleep(RETRY)

    threading.Thread(target=loop, name="language-update", daemon=True).start()
