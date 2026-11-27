"""Automatické aktualizace z GitHub Releases.

Každé vydání (tools/release.py) má soubory NormanMail.exe, latest.json (verze, SHA-256 a velikost exe,
poznámky) a latest.json.sig (podpis Ed25519 souboru latest.json). Aplikace věří jen podpisu: veřejný
klíč je zabudovaný níže, soukromý má jen vývojář. Ani převzatý účet na GitHubu tak uživatelům nepodstrčí
cizí program. Stažený exe musí sedět na velikost a SHA-256 z podepsaného latest.json.

Průběh: kontrola minutu po startu a pak jednou denně (jen sestavená aplikace, lze vypnout v Nastavení)
→ novější verze se stáhne vedle exe jako NormanMail.exe.new → po kliknutí na „Restartovat“ nebo při
zavření aplikace se běžící exe přejmenuje na .old (Windows to u běžícího programu dovolí) a .new zaujme
jeho místo → nová verze po startu smaže .old. Kde do složky s exe nejde zapisovat, aplikace jen nabídne
stránku ke stažení. Na GitHub se neposílá nic o uživateli, jen běžný požadavek s verzí aplikace.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import ed25519
from version import VERSION

REPO = "richard03/Norman-Mail"
ASSET = "NormanMail.exe"
MANIFEST = "latest.json"
PUBLIC_KEY = bytes.fromhex("9ba184047f594af0421e1afa9f9fcb0e60dbc3de37d09e865d2834ea6c696c49")
DOWNLOAD_PAGE = f"https://github.com/{REPO}/releases/latest"

FIRST_CHECK = 60          # s po startu
INTERVAL = 24 * 3600      # jak často kontrolovat
TIMEOUT = 30
MAX_MANIFEST = 64 * 1024
MAX_EXE = 300 * 1024 * 1024
CHUNK = 256 * 1024


class UpdateError(Exception):
    """Chyba aktualizace; text se skládá až při zobrazení (klíč katalogu + parametry)."""

    def __init__(self, key, /, **params):
        super().__init__(key)
        self.key, self.params = key, params


def parse_version(s):
    """„1.2.3“ → (1, 2, 3); cokoli jiného → None."""
    if not isinstance(s, str) or not re.fullmatch(r"\d{1,6}(\.\d{1,6}){1,3}", s):
        return None
    return tuple(int(x) for x in s.split("."))


def _urls(version=None):
    """(adresa latest.json, adresa exe dané verze). NORMANMAIL_UPDATE_URL přesměruje kontrolu jinam
    (zkoušení aktualizací): složka s latest.json, latest.json.sig a NormanMail.exe. Podpis platí i tam."""
    base = os.environ.get("NORMANMAIL_UPDATE_URL", "").rstrip("/")
    if base and (base.startswith("https://") or re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", base)):
        return f"{base}/{MANIFEST}", f"{base}/{ASSET}"
    return (f"https://github.com/{REPO}/releases/latest/download/{MANIFEST}",
            f"https://github.com/{REPO}/releases/download/v{version}/{ASSET}")


def _open(url):
    req = urllib.request.Request(url, headers={"User-Agent": f"NormanMail/{VERSION}"})
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("err.update.notFound") from None
        raise UpdateError("err.update.network", error=f"HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError("err.update.network", error=str(getattr(e, "reason", e))) from None


def _fetch(url, limit):
    try:
        with _open(url) as r:
            data = r.read(limit + 1)
    except (urllib.error.URLError, OSError) as e:  # přerušené spojení během čtení
        raise UpdateError("err.update.network", error=str(getattr(e, "reason", e))) from None
    if len(data) > limit:
        raise UpdateError("err.update.badManifest")
    return data


LANG_FILE_MAX = 1024 * 1024  # stejně jako i18n.MAX_FILE


def _languages(raw) -> dict:
    """Oficiální překlady vydání z latest.json: {kód: {name, sha256, size}}; neplatné položky se vynechají
    (vydání do 1.0.4 je nemají vůbec)."""
    out = {}
    for code, v in (raw.items() if isinstance(raw, dict) else ()):
        if (isinstance(code, str) and re.fullmatch(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*", code) and isinstance(v, dict)
                and isinstance(v.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", v["sha256"])
                and type(v.get("size")) is int and 0 < v["size"] <= LANG_FILE_MAX):
            name = v.get("name")
            out[code] = {"name": name if isinstance(name, str) and name else code,
                         "sha256": v["sha256"], "size": v["size"]}
    return out


def verify_manifest(body: bytes, sig_text: bytes) -> dict:
    """Ověří podpis a obsah latest.json. Vrací {version, sha256, size, notes, languages}."""
    try:
        sig = bytes.fromhex(sig_text.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        raise UpdateError("err.update.badSignature") from None
    if not ed25519.verify(PUBLIC_KEY, body, sig):
        raise UpdateError("err.update.badSignature")
    try:
        m = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UpdateError("err.update.badManifest") from None
    if not (isinstance(m, dict) and parse_version(m.get("version"))
            and isinstance(m.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", m["sha256"])
            and type(m.get("size")) is int and 0 < m["size"] <= MAX_EXE):
        raise UpdateError("err.update.badManifest")
    notes = m.get("notes")
    return {"version": m["version"], "sha256": m["sha256"], "size": m["size"],
            "notes": notes if isinstance(notes, str) else "", "languages": _languages(m.get("languages"))}


def fetch_manifest() -> dict:
    url, _ = _urls()
    body = _fetch(url, MAX_MANIFEST)
    return verify_manifest(body, _fetch(url + ".sig", 1024))


def release_url(version, name) -> str:
    """Adresa souboru konkrétního vydání (latest.json, překlad cs.yaml …). S NORMANMAIL_UPDATE_URL
    podsložka v<verze> místní složky (zkoušení)."""
    base = _urls()[0].rsplit("/", 1)[0]
    if base.endswith("/releases/latest/download"):
        return f"https://github.com/{REPO}/releases/download/v{version}/{name}"
    return f"{base}/v{version}/{name}"


def fetch_release_manifest(version) -> dict:
    """Ověřený latest.json konkrétního vydání (každé vydání ho má mezi soubory)."""
    url = release_url(version, MANIFEST)
    body = _fetch(url, MAX_MANIFEST)
    return verify_manifest(body, _fetch(url + ".sig", 1024))


# --------------------------------------------------------------------------
# Soubory vedle exe
# --------------------------------------------------------------------------

def frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and Path(sys.executable).suffix.lower() == ".exe"


def exe_path() -> Path:
    return Path(sys.executable)


def _sibling(suffix) -> Path:
    exe = exe_path()
    return exe.with_name(exe.name + suffix)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _matches(path: Path, m) -> bool:
    try:
        return path.stat().st_size == m["size"] and _sha256(path) == m["sha256"]
    except OSError:
        return False


class _NotWritable(Exception):
    pass


def download(m) -> Path:
    """Stáhne exe verze m do NormanMail.exe.new (ověřené velikostí a SHA-256)."""
    new = _sibling(".new")
    if _matches(new, m):
        return new  # staženo už dřív
    part = _sibling(".part")
    try:
        f = open(part, "wb")
    except OSError:
        raise _NotWritable() from None
    h, size = hashlib.sha256(), 0
    try:
        with f, _open(_urls(m["version"])[1]) as r:
            while True:
                block = r.read(CHUNK)
                if not block:
                    break
                size += len(block)
                if size > m["size"]:
                    break
                h.update(block)
                f.write(block)
    except (urllib.error.URLError, OSError) as e:
        part.unlink(missing_ok=True)
        raise UpdateError("err.update.network", error=str(getattr(e, "reason", e))) from None
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if size != m["size"] or h.hexdigest() != m["sha256"]:
        part.unlink(missing_ok=True)
        raise UpdateError("err.update.badHash")
    os.replace(part, new)
    return new


# --------------------------------------------------------------------------
# Stav a kontrola
# --------------------------------------------------------------------------

_lock = threading.Lock()
_state = {"status": "idle", "version": None, "notes": "", "error": None, "checked": None}
_manifest = None      # ověřený manifest staženého .new
_restart = False      # po zavření okna spustit novou verzi


def status() -> dict:
    """status: idle (zatím nekontrolováno) / checking / downloading / current (nejnovější) /
    available (nová verze, ale nainstalovat se sama nedá) / ready (stažená) / installed / error."""
    with _lock:
        return {**_state, "current": VERSION, "can_install": frozen(), "download_page": DOWNLOAD_PAGE}


def _set(**kw):
    with _lock:
        _state.update(kw)


def check():
    """Zkontroluje (a stáhne) aktualizaci. Volá se z vlákna na pozadí; souběžné volání se přeskočí."""
    global _manifest
    with _lock:
        if _state["status"] in ("checking", "downloading", "installed"):
            return
        _state.update(status="checking", error=None)
    try:
        try:
            m = fetch_manifest()
        except UpdateError as e:
            if e.key != "err.update.notFound":
                raise
            m = None  # zatím žádné vydání
        if m is None or parse_version(m["version"]) <= parse_version(VERSION):
            _set(status="current", version=None, notes="")
        elif not frozen():
            _set(status="available", version=m["version"], notes=m["notes"])
        else:
            _set(status="downloading", version=m["version"], notes=m["notes"])
            try:
                download(m)
            except _NotWritable:
                _set(status="available")
            else:
                _manifest = m
                _set(status="ready")
    except UpdateError as e:
        _set(status="error", error=(e.key, e.params))
    except Exception as e:  # noqa: BLE001 – kontrola aktualizací nesmí shodit aplikaci
        _set(status="error", error=("err.update.failed", {"error": str(e)}))
    finally:
        _set(checked=time.time())


def check_async():
    threading.Thread(target=check, name="update-check", daemon=True).start()


def start(enabled):
    """Pravidelná kontrola na pozadí (jen sestavená aplikace). enabled() = je zapnutá v Nastavení?"""
    if not frozen():
        return

    def loop():
        time.sleep(FIRST_CHECK)
        while True:
            last = _state["checked"] or 0
            try:
                if enabled() and time.time() - last >= INTERVAL and _state["status"] not in ("ready", "installed"):
                    check()
            except Exception as e:  # noqa: BLE001
                print("Kontrola aktualizací selhala:", e)
            time.sleep(3600)

    threading.Thread(target=loop, name="update-loop", daemon=True).start()


# --------------------------------------------------------------------------
# Instalace
# --------------------------------------------------------------------------

def install(restart: bool):
    """Vymění exe za staženou verzi. restart=True: po zavření okna se spustí nová verze (relaunch)."""
    global _restart
    with _lock:
        if _state["status"] != "ready" or _manifest is None:
            raise UpdateError("err.update.notReady")
        exe, new = exe_path(), _sibling(".new")
        if not _matches(new, _manifest):  # soubor se mezitím změnil
            _state.update(status="error", error=("err.update.badHash", {}))
            raise UpdateError("err.update.badHash")
        old = _sibling(".old")
        try:
            old.unlink(missing_ok=True)
        except OSError:  # starou verzi ještě drží předchozí proces
            old = _sibling(f".{os.getpid()}.old")
        try:
            os.replace(exe, old)  # běžící exe přejmenovat jde, smazat ne
            try:
                os.replace(new, exe)
            except OSError:
                os.replace(old, exe)
                raise
        except OSError as e:
            raise UpdateError("err.update.install", error=str(e)) from None
        _state["status"] = "installed"
        _restart = restart


def install_on_exit():
    """Při zavření aplikace: stažená aktualizace se nainstaluje (spustí se až příště)."""
    try:
        if _state["status"] == "ready":
            install(restart=False)
    except UpdateError as e:
        print("Aktualizaci nejde nainstalovat:", e.key, e.params)


def relaunch():
    """Spustí novou verzi (po install(restart=True)) – až po zavření okna a odeslání čekajících zpráv."""
    if not _restart:
        return
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")  # nová instance si rozbalí vlastní soubory
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen([str(exe_path()), "--after-update"], env=env, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print("Novou verzi nejde spustit:", e)


def cleanup():
    """Smaže starou verzi (.old) a nedokončené stažení. Volá se, když nová verze úspěšně naběhla;
    předchozí proces může ještě chvíli končit, proto několik pokusů."""
    if not frozen():
        return

    def run():
        exe = exe_path()
        for _ in range(30):
            left = False
            for f in [*exe.parent.glob(exe.name + ".*.old"), _sibling(".old"), _sibling(".part")]:
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    left = True
            if not left:
                return
            time.sleep(2)

    threading.Thread(target=run, name="update-cleanup", daemon=True).start()
