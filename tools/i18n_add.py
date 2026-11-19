"""Přidá texty do katalogů (static/i18n/en.yaml a languages/cs.yaml) (pomocník pro převod; klíče, které už v katalogu jsou, přeskočí).

    .venv\\Scripts\\python tools\\i18n_add.py cs.json en.json "# Nadpis oddílu"

cs.json a en.json jsou {klíč: text} nebo {klíč: {"one": …, "other": …}} se stejnými klíči.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import i18n  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def catalog_path(lang):
    """Angličtina je zabudovaná v aplikaci, ostatní jazyky jsou samostatné soubory v languages/."""
    return ROOT / "static" / "i18n" / "en.yaml" if lang == "en" else ROOT / "languages" / f"{lang}.yaml"


def quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def render(key, value):
    if isinstance(value, dict):
        return f"{key}:\n" + "".join(f"  {k}: {quote(v)}\n" for k, v in value.items())
    return f"{key}: {quote(value)}\n"


def add(lang, entries, heading):
    path = catalog_path(lang)
    text = path.read_text("utf-8")
    existing = i18n.parse(text)
    new = [render(k, v) for k, v in entries.items() if k not in existing]
    if not new:
        return 0
    if not text.endswith("\n"):
        text += "\n"
    text += f"\n{heading}\n" + "".join(new)
    i18n.validate(i18n.parse(text))  # po zápisu musí katalog zůstat platný
    path.write_text(text, "utf-8", newline="\n")
    return len(new)


def main():
    cs = json.loads(Path(sys.argv[1]).read_text("utf-8"))
    en = json.loads(Path(sys.argv[2]).read_text("utf-8"))
    heading = sys.argv[3] if len(sys.argv) > 3 else ""
    missing = [k for k in cs if k not in en]
    extra = [k for k in en if k not in cs]
    if missing or extra:
        sys.exit(f"Chybí anglicky: {missing}\nNavíc anglicky: {extra}")
    print("cs:", add("cs", cs, heading), "en:", add("en", en, heading))


if __name__ == "__main__":
    main()
