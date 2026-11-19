"""Lokalizace: katalogy textů v jednoduchých YAML souborech.

V aplikaci je zabudovaná jen angličtina (static/i18n/en.yaml), aktualizuje se s ní. Ostatní jazyky
(i oficiální čeština, v repozitáři languages/cs.yaml a jako soubor u každého vydání) si uživatel dá jako
soubor <kód>.yaml do podsložky languages ve složce dat (%APPDATA%\\Mailer\\languages, v portable režimu
languages vedle programu). Chybějící texty se doplní anglicky.

Formát je podmnožina YAML, která zná jen texty – nic se nepřevádí na čísla ani true/false:

    meta:
      language: de
      name: Deutsch
      locale: de-DE
    nav.important: Wichtig
    messages.count:          # množné číslo (podklíče zero/one/two/few/many/other)
      one: "{count} Nachricht"
      other: "{count} Nachrichten"
    about.intro: |
      Víceřádkový text …

Texty se v rozhraní vkládají vždy jako prostý text; jediné formátování je **tučně**.
"""

import re
import sys
import threading
from pathlib import Path

from store import DATA_DIR

BASE = "en"
MAX_FILE = 1024 * 1024
PLURAL_FORMS = ("zero", "one", "two", "few", "many", "other")
META_FIELDS = ("language", "name", "locale", "app_version", "author")
KEY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
LANG_RE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class CatalogError(ValueError):
    """Chyba souboru s překladem. Text se skládá až při zobrazení (t), protože při načítání
    katalogů ještě žádný katalog není k dispozici."""

    def __init__(self, msg_key, line=None, /, **params):
        super().__init__(msg_key)
        self.key, self.line, self.params = msg_key, line, params

    def text(self):
        msg = t(self.key, **self.params)
        return t("i18n.error.line", line=self.line, message=msg) if self.line else msg

    def __str__(self):
        return self.text() if _catalogs is not None else f"{self.key} (line {self.line})"


class ParseError(CatalogError):
    def __init__(self, line, msg_key, /, **params):
        super().__init__(msg_key, line, **params)


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def _unquote(raw, line):
    """Hodnota na řádku: v uvozovkách (jako v YAML), jinak prostý text bez komentáře za „ #“."""
    if raw[:1] == '"':
        out, i = [], 1
        while i < len(raw):
            c = raw[i]
            if c == "\\" and i + 1 < len(raw):
                nxt = raw[i + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, "\\" + nxt))
                i += 2
                continue
            if c == '"':
                rest = raw[i + 1:].strip()
                if rest and not rest.startswith("#"):
                    raise ParseError(line, "i18n.error.afterQuote")
                return "".join(out)
            out.append(c)
            i += 1
        raise ParseError(line, "i18n.error.missingQuote", quote='"')
    if raw[:1] == "'":
        out, i = [], 1
        while i < len(raw):
            if raw[i] == "'":
                if raw[i + 1:i + 2] == "'":  # '' = apostrof
                    out.append("'")
                    i += 2
                    continue
                rest = raw[i + 1:].strip()
                if rest and not rest.startswith("#"):
                    raise ParseError(line, "i18n.error.afterQuote")
                return "".join(out)
            out.append(raw[i])
            i += 1
        raise ParseError(line, "i18n.error.missingQuote", quote="'")
    cut = re.search(r"\s#", raw)
    return (raw[:cut.start()] if cut else raw).strip()


def parse(text):
    """Text souboru → {klíč: text nebo {podklíč: text}}. Chyba → ParseError s číslem řádku."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    data, i = {}, 0

    def indent_of(s):
        return len(s) - len(s.lstrip(" "))

    def split_key(s, n):
        m = re.match(r"\s*([^:#\s][^:]*?)\s*:(\s|$)(.*)$", s)
        if not m:
            raise ParseError(n, "i18n.error.expectedKey")
        key = m.group(1).strip("\"'")
        if not KEY_RE.match(key):
            raise ParseError(n, "i18n.error.badKey", key=key)
        return key, m.group(3).strip()

    def block(start, parent_indent, folded=False):
        """Řádky odsazené víc než parent_indent od řádku start (blok za | nebo >)."""
        body, j = [], start
        while j < len(lines):
            s = lines[j]
            if s.strip() and indent_of(s) <= parent_indent:
                break
            if "\t" in s[:indent_of(s) + 1] and s.strip():
                raise ParseError(j + 1, "i18n.error.tab")
            body.append(s)
            j += 1
        while body and not body[-1].strip():
            body.pop()
        if not body:
            raise ParseError(start, "i18n.error.emptyBlock")
        ind = min(indent_of(s) for s in body if s.strip())
        rows = [s[ind:] if s.strip() else "" for s in body]
        if folded:  # > : řádky spojit mezerou, prázdný řádek = nový odstavec
            text_, para = [], []
            for r in rows:
                if r:
                    para.append(r)
                else:
                    text_.append(" ".join(para))
                    para = []
            text_.append(" ".join(para))
            return "\n".join(t for t in text_ if t), j
        return "\n".join(rows), j

    def value(raw, n, start, parent_indent):
        if raw in ("|", "|-", "|+", ">", ">-", ">+"):
            return block(start, parent_indent, folded=raw.startswith(">"))
        if raw[:1] in ("[", "{", "&", "*", "!", "%", "@", "`"):
            raise ParseError(n, "i18n.error.quoteNeeded")
        return _unquote(raw, n), start

    while i < len(lines):
        s, n = lines[i], i + 1
        if not s.strip() or s.lstrip().startswith("#") or s.strip() in ("---", "..."):
            i += 1
            continue
        if "\t" in s[:indent_of(s) + 1]:
            raise ParseError(n, "i18n.error.tab")
        if indent_of(s):
            raise ParseError(n, "i18n.error.unexpectedIndent")
        key, raw = split_key(s, n)
        if key in data:
            raise ParseError(n, "i18n.error.duplicateKey", key=key)
        if raw and not raw.startswith("#"):
            data[key], i = value(raw, n, i + 1, 0)
            continue
        # vnořené podklíče (meta, množné číslo)
        sub, j = {}, i + 1
        sub_indent = None
        while j < len(lines):
            t, m = lines[j], j + 1
            if not t.strip() or t.lstrip().startswith("#"):
                j += 1
                continue
            ind = indent_of(t)
            if ind == 0:
                break
            if sub_indent is None:
                sub_indent = ind
            elif ind != sub_indent:
                raise ParseError(m, "i18n.error.subIndent")
            k2, raw2 = split_key(t, m)
            if k2 in sub:
                raise ParseError(m, "i18n.error.duplicateKey", key=k2)
            if not raw2 or raw2.startswith("#"):
                raise ParseError(m, "i18n.error.subText")
            sub[k2], j = value(raw2, m, j + 1, ind)
        if not sub:
            raise ParseError(n, "i18n.error.noText", key=key)
        data[key] = sub
        i = j
    return data


def validate(data):
    """Zkontroluje strukturu katalogu. Vrací (meta, texty); chyba → ValueError."""
    meta = data.get("meta")
    if not isinstance(meta, dict):
        raise CatalogError("i18n.error.noMeta")
    unknown = set(meta) - set(META_FIELDS)
    if unknown:
        raise CatalogError("i18n.error.metaUnknown", fields=", ".join(sorted(unknown)))
    lang = meta.get("language", "")
    if not LANG_RE.match(lang):
        raise CatalogError("i18n.error.badLanguage", language=lang)
    texts = {}
    for k, v in data.items():
        if k == "meta":
            continue
        if isinstance(v, dict):
            bad = set(v) - set(PLURAL_FORMS)
            if bad:
                raise CatalogError("i18n.error.pluralForms", key=k, forms=", ".join(PLURAL_FORMS))
            if "other" not in v:
                raise CatalogError("i18n.error.pluralOther", key=k)
        texts[k] = v
    meta = {"language": lang, "name": meta.get("name") or lang, "locale": meta.get("locale") or lang,
            **{k: meta[k] for k in ("app_version", "author") if k in meta}}
    return meta, texts


def load_file(path):
    """(meta, texty) ze souboru; chyba → ValueError s popisem pro uživatele."""
    path = Path(path)
    if path.stat().st_size > MAX_FILE:
        raise CatalogError("i18n.error.tooBig")
    try:
        text = path.read_text("utf-8")
    except UnicodeDecodeError:
        raise CatalogError("i18n.error.notUtf8")
    return validate(parse(text))


# --------------------------------------------------------------------------
# Katalogy
# --------------------------------------------------------------------------

def builtin_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "static" / "i18n"


def user_dir() -> Path:
    """Složka s uživatelskými jazyky: vždy podsložka languages ve složce dat (vedle databáze a nastavení)."""
    return DATA_DIR / "languages"


_lock = threading.Lock()
_catalogs = None       # {jazyk: {"meta", "texts", "sources": [...]}}
_problems = []         # [{"file", "error"}] – soubory, které nešly použít
_current = BASE


def _scan():
    catalogs, problems = {}, []
    for f in sorted(builtin_dir().glob("*.yaml")):
        meta, texts = load_file(f)  # vestavěný katalog musí být v pořádku (testy)
        catalogs[meta["language"]] = {"meta": meta, "texts": dict(texts), "sources": ["builtin"]}
    try:
        files = sorted(user_dir().glob("*.yaml")) + sorted(user_dir().glob("*.yml"))
    except OSError:
        files = []
    for f in files:
        if f.name.startswith("_"):  # _template.yaml a jiné odložené soubory
            continue
        try:
            meta, texts = load_file(f)
        except (OSError, ValueError) as e:
            problems.append({"file": str(f), "error": e})
            continue
        cat = catalogs.setdefault(meta["language"], {"meta": meta, "texts": {}, "sources": []})
        if cat["sources"] == ["builtin"] and meta["language"] != BASE:
            # úprava vestavěného jazyka: meta (název, formát data) může přepsat
            cat["meta"] = {**cat["meta"], **{k: v for k, v in meta.items() if v}}
        cat["texts"].update(texts)
        cat["sources"].append(str(f))
    return catalogs, problems


def reload():
    global _catalogs, _problems
    with _lock:
        _catalogs, _problems = _scan()


def _ensure():
    if _catalogs is None:
        reload()


def languages():
    """Dostupné jazyky pro výběr v Nastavení."""
    _ensure()
    base_keys = set(_catalogs[BASE]["texts"])
    out = []
    for code, cat in sorted(_catalogs.items(), key=lambda kv: kv[1]["meta"]["name"].lower()):
        missing = len(base_keys - set(cat["texts"])) if code != BASE else 0
        out.append({"code": code, "name": cat["meta"]["name"], "missing": missing,
                    "builtin": "builtin" in cat["sources"],
                    "files": [s for s in cat["sources"] if s != "builtin"]})
    return out


def problems():
    """Soubory, které nešly použít: [{file, error}] s textem chyby v aktuálním jazyce."""
    _ensure()
    return [{"file": p["file"], "error": p["error"].text() if isinstance(p["error"], CatalogError) else str(p["error"])}
            for p in _problems]


def windows_language():
    """Jazyk rozhraní Windows (např. „cs“), nebo None."""
    try:
        import ctypes
        import locale
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        name = locale.windows_locale.get(lcid, "")
        return name.split("_")[0].lower() or None
    except Exception:
        return None


def resolve(preferred=""):
    """Kód jazyka, který se použije: zvolený (pokud je k dispozici), jinak angličtina."""
    _ensure()
    return preferred if preferred and preferred in _catalogs else BASE


def has_user_files():
    """Leží ve složce s jazyky aspoň jeden použitelný překlad? (pak se při prvním spuštění nabídne výběr)"""
    _ensure()
    return any(s != "builtin" for cat in _catalogs.values() for s in cat["sources"])


def set_language(preferred=""):
    global _current
    _current = resolve(preferred)
    return _current


def current():
    return _current


def catalog(code=None):
    """Sloučený katalog pro rozhraní: angličtina doplněná textem zvoleného jazyka."""
    _ensure()
    code = code or _current
    cat = _catalogs.get(code) or _catalogs[BASE]
    texts = {**_catalogs[BASE]["texts"], **cat["texts"]}
    return {"language": cat["meta"]["language"], "locale": cat["meta"]["locale"], "texts": texts}


def template_text():
    """Šablona pro nový překlad: vestavěná angličtina."""
    return (builtin_dir() / f"{BASE}.yaml").read_text("utf-8")


def _format(text, params):
    # žádné str.format – to by z překladu umělo číst atributy objektů ({x.__class__})
    return PARAM_RE.sub(lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), text)


def _plural_form(lang, n):
    """Tvar množného čísla pro Python (stačí běžné jazyky; frontend používá Intl.PluralRules)."""
    lang = lang.split("-")[0]
    if lang in ("cs", "sk"):
        return "one" if n == 1 else "few" if 2 <= n <= 4 else "other"
    if lang == "pl":
        return "one" if n == 1 else "few" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "many"
    if lang in ("ja", "zh", "ko", "vi", "th"):
        return "other"
    return "one" if n == 1 else "other"


def t(key, **params):
    """Text v aktuálním jazyce (chybí-li, anglicky; chybí-li i tam, samotný klíč)."""
    _ensure()
    texts = _catalogs.get(_current, _catalogs[BASE])["texts"]
    v = texts.get(key, _catalogs[BASE]["texts"].get(key, key))
    if isinstance(v, dict):
        n = params.get("count", 0)
        v = v.get(_plural_form(_current, n)) or v.get("other", key)
    return _format(v, params)
