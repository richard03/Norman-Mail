"""Vydání nové verze Norman Mail na GitHub Releases (odtud si ji aplikace sama stáhne, viz updater.py).

Jednorázově vytvořit pár klíčů pro podepisování aktualizací:
    .venv\\Scripts\\python tools\\release.py --genkey
  Soukromý klíč se uloží do %USERPROFILE%\\.normanmail\\release_key (mimo repozitář, zálohujte ho,
  bez něj už žádnou aktualizaci nepodepíšete). Vypsaný veřejný klíč patří do updater.PUBLIC_KEY.

Vydání (nejdřív zvyšte VERSION ve version.py, commitněte a pushněte):
    .venv\\Scripts\\python tools\\release.py --notes "Co je nového"      (nebo --notes-file soubor.txt)
  Zkontroluje čistý strom a novou verzi, sestaví dist\\NormanMail.exe, vytvoří a podepíše
  dist\\latest.json a přes GitHub CLI (gh) založí release vX.Y.Z se soubory NormanMail.exe,
  latest.json a latest.json.sig a s překlady z languages\\*.yaml a šablonou translation-template.yaml. Bez gh (nebo s --no-publish) jen připraví soubory k ručnímu nahrání.
"""

import argparse
import datetime
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ed25519  # noqa: E402
import updater  # noqa: E402
from version import VERSION  # noqa: E402

KEY_FILE = Path(os.environ.get("NORMANMAIL_RELEASE_KEY") or Path.home() / ".normanmail" / "release_key")
DIST = ROOT / "dist"
TEMPLATE = "translation-template.yaml"  # anglická šablona pro nový překlad (příloha release)


def die(msg):
    print("CHYBA:", msg)
    sys.exit(1)


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def genkey():
    if KEY_FILE.exists():
        die(f"{KEY_FILE} už existuje. Nový klíč by znamenal, že nainstalované verze odmítnou další aktualizace.")
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    with open(KEY_FILE, "x", encoding="ascii") as f:
        f.write(secret.hex() + "\n")
    print("Soukromý klíč:", KEY_FILE, "(zálohujte ho a nikomu nedávejte)")
    print("Veřejný klíč do updater.PUBLIC_KEY:", ed25519.public_key(secret).hex())


def load_key() -> bytes:
    try:
        secret = bytes.fromhex(KEY_FILE.read_text("ascii").strip())
    except (OSError, ValueError) as e:
        die(f"Soukromý klíč {KEY_FILE} nejde načíst ({e}). Vytvořte ho přes --genkey.")
    if ed25519.public_key(secret) != updater.PUBLIC_KEY:
        die("Soukromý klíč nepatří k updater.PUBLIC_KEY – aplikace by aktualizaci odmítla.")
    return secret


def check_repo(tag):
    if git("status", "--porcelain"):
        die("V repozitáři jsou necommitnuté změny.")
    git("fetch", "--quiet", "origin")
    if git("rev-parse", "HEAD") != git("rev-parse", "origin/main"):
        die("HEAD není totožný s origin/main – pushněte (nebo stáhněte) změny.")
    if git("tag", "-l", tag) or git("ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
        die(f"Verze {tag} už existuje – zvyšte VERSION ve version.py.")


def build():
    pyinstaller = ROOT / ".venv" / "Scripts" / "pyinstaller.exe"
    subprocess.run([str(pyinstaller), "--noconfirm", "--clean", "NormanMail.spec"], cwd=ROOT, check=True)


def manifest(exe: Path, notes: str) -> bytes:
    data = exe.read_bytes()
    info = {
        "version": VERSION,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "date": datetime.date.today().isoformat(),
        "notes": notes,
    }
    return (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main():
    ap = argparse.ArgumentParser(description="Vydání Norman Mail na GitHub Releases")
    ap.add_argument("--genkey", action="store_true", help="jednorázově vytvořit klíč pro podepisování")
    ap.add_argument("--notes", default="", help="poznámky k vydání (ukážou se v aplikaci)")
    ap.add_argument("--notes-file", help="poznámky k vydání ze souboru (UTF-8)")
    ap.add_argument("--no-build", action="store_true", help="použít už sestavený dist\\NormanMail.exe")
    ap.add_argument("--no-publish", action="store_true", help="jen připravit soubory v dist\\")
    ap.add_argument("--allow-dirty", action="store_true", help="přeskočit kontrolu repozitáře (jen na zkoušku)")
    args = ap.parse_args()
    if args.genkey:
        return genkey()

    tag = f"v{VERSION}"
    notes = Path(args.notes_file).read_text("utf-8").strip() if args.notes_file else args.notes.strip()
    secret = load_key()
    if not args.allow_dirty:
        check_repo(tag)
    if not args.no_build:
        build()
    exe = DIST / updater.ASSET
    if not exe.is_file():
        die(f"{exe} neexistuje.")

    body = manifest(exe, notes)
    sig = ed25519.sign(secret, body)
    if not ed25519.verify(updater.PUBLIC_KEY, body, sig):
        die("Podpis nejde ověřit.")
    (DIST / updater.MANIFEST).write_bytes(body)
    (DIST / (updater.MANIFEST + ".sig")).write_text(sig.hex() + "\n", "ascii")
    files = [str(DIST / n) for n in (updater.ASSET, updater.MANIFEST, updater.MANIFEST + ".sig")]
    # překlady (nejsou v exe) jako samostatné soubory ke stažení, k tomu anglická šablona pro nový překlad
    files += [str(p) for p in sorted((ROOT / "languages").glob("*.yaml")) if not p.name.startswith("_")]
    template = DIST / TEMPLATE
    template.write_text((ROOT / "static" / "i18n" / "en.yaml").read_text("utf-8"), "utf-8")
    files.append(str(template))
    print(body.decode("utf-8"))

    if args.no_publish or args.allow_dirty:
        print("Soubory k nahrání do release", tag, ":\n  " + "\n  ".join(files))
        return
    gh = shutil.which("gh") or next((str(p) for p in (Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                                                      / "GitHub CLI" / "gh.exe",) if p.is_file()), None)
    if not gh:
        print("GitHub CLI (gh) není nainstalované (winget install GitHub.cli). Založte release", tag,
              f"na https://github.com/{updater.REPO}/releases/new ručně a nahrajte:\n  " + "\n  ".join(files))
        return
    subprocess.run([gh, "release", "create", tag, *files, "--repo", updater.REPO, "--target", "main",
                    "--title", f"Norman Mail {VERSION}", "--notes", notes or f"Norman Mail {VERSION}"],
                   cwd=ROOT, check=True)
    print("Vydáno:", f"https://github.com/{updater.REPO}/releases/tag/{tag}")


if __name__ == "__main__":
    main()
