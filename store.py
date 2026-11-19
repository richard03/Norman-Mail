"""Úložiště nastavení účtů. Hesla jdou do systémové klíčenky (Správce pověření Windows)."""

import json
import os
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

try:
    import keyring
    from keyring.backends import fail as _fail

    KEYRING_OK = not isinstance(keyring.get_keyring(), _fail.Keyring)
except Exception:  # keyring není nainstalovaný nebo nemá backend
    keyring = None
    KEYRING_OK = False

SERVICE = "Mailer"  # neměnit (původní název aplikace): pod tímto jménem jsou uložená hesla


PORTABLE_MARKER = "portable.txt"  # leží-li vedle NormanMail.exe, data jsou přímo ve složce s ním
PORTABLE_LEGACY = "data"          # do verze 1.0.2 byla data portable verze v podsložce data


def portable_dir():
    """Složka portable verze (sestavená aplikace se souborem portable.txt vedle exe), jinak None."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        if (base / PORTABLE_MARKER).exists():
            return base
    return None


def _migrate_portable(base: Path) -> Path:
    """Přesune data portable verze z dřívější podsložky data přímo do složky s exe. Vrací složku dat.

    Po automatické aktualizaci se nová verze spouští, zatímco stará se ještě ukončuje a může mít databázi
    otevřenou – proto se přesun chvíli zkouší znovu. Když se nepovede celý, vrátí se, co se už přesunulo,
    a aplikace tentokrát použije původní složku (přesun zkusí při dalším spuštění)."""
    old = base / PORTABLE_LEGACY
    if not old.is_dir() or (base / "accounts.json").exists() or (base / "mailer.db").exists():
        return base
    items = [p for p in old.iterdir() if not (base / p.name).exists()]
    moved = []
    deadline = time.monotonic() + 30
    for item in items:
        while True:
            try:
                item.rename(base / item.name)
                moved.append(item)
                break
            except OSError:
                if time.monotonic() > deadline:
                    for m in reversed(moved):
                        try:
                            (base / m.name).rename(m)
                        except OSError:
                            pass
                    return old
                time.sleep(0.5)
    try:
        old.rmdir()  # jen když je prázdná
    except OSError:
        pass
    return base


def _data_dir() -> Path:
    if os.environ.get("MAILER_DATA"):
        return Path(os.environ["MAILER_DATA"])
    if portable_dir():
        return _migrate_portable(portable_dir())
    target = Path(os.environ.get("APPDATA") or Path.home()) / "Mailer"  # původní název aplikace, neměnit
    legacy = Path(__file__).resolve().parent / "data"  # dřívější umístění vedle zdrojáků
    if not target.exists() and legacy.is_dir():
        shutil.copytree(legacy, target)
    return target


DATA_DIR = _data_dir()
FILE = DATA_DIR / "accounts.json"

FIELDS = ["name", "display_name", "email", "username", "imap_host", "imap_port", "imap_security",
          "smtp_host", "smtp_port", "smtp_security", "auth"]
REQUIRED = ["email", "imap_host", "imap_port", "smtp_host", "smtp_port"]


class ValidationError(Exception):
    pass


def normalize(data: dict) -> dict:
    acc = {k: str(data.get(k) or "").strip() for k in FIELDS}
    missing = [k for k in REQUIRED if not acc[k]]
    if missing:
        from i18n import t as tr  # store se načítá dřív než i18n (i18n z něj bere DATA_DIR)
        raise ValidationError(tr("err.requiredFields", fields=", ".join(missing)))
    if "@" not in acc["email"]:
        from i18n import t as tr
        raise ValidationError(tr("err.invalidEmail"))
    for k in ("imap_port", "smtp_port"):
        try:
            port = int(acc[k])
            if not 0 < port < 65536:
                raise ValueError
        except ValueError:
            from i18n import t as tr
            raise ValidationError(tr("err.invalidPort", port=acc[k]))
        acc[k] = port
    for k in ("imap_security", "smtp_security"):
        if acc[k] not in ("ssl", "starttls"):
            acc[k] = "ssl"
    if acc["auth"] not in ("password", "oauth2"):
        acc["auth"] = "password"
    acc["username"] = acc["username"] or acc["email"]
    acc["name"] = acc["name"] or acc["email"]
    return acc


class AccountStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._accounts = json.loads(FILE.read_text("utf-8")) if FILE.exists() else []

    def _save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._accounts, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, FILE)

    # --- hesla ---
    def _get_password(self, acc):
        if acc.get("password_in_keyring") and keyring:
            try:
                return keyring.get_password(SERVICE, acc["id"]) or ""
            except Exception:
                return ""
        return acc.get("password", "")

    def _set_password(self, acc, password):
        if KEYRING_OK:
            keyring.set_password(SERVICE, acc["id"], password)
            acc["password_in_keyring"] = True
            acc.pop("password", None)
        else:
            acc["password"] = password
            acc["password_in_keyring"] = False

    # --- veřejné API ---
    def list_public(self):
        with self._lock:
            return [{k: a.get(k) for k in ["id"] + FIELDS} for a in self._accounts]

    def get(self, acc_id):
        with self._lock:
            acc = next((a for a in self._accounts if a["id"] == acc_id), None)
            if acc is None:
                return None
            full = dict(acc)
        full["password"] = self._get_password(full)
        return full

    def find_by_email(self, address):
        with self._lock:
            acc = next((a for a in self._accounts if a["email"].lower() == address.lower()), None)
        return self.get(acc["id"]) if acc else None

    def all(self):
        accounts = (self.get(a["id"]) for a in self.list_public())
        return [a for a in accounts if a is not None]  # účet mohl být mezitím smazán

    def save(self, acc_id, acc: dict, password: str):
        """Vytvoří (acc_id=None) nebo aktualizuje účet. Vrací id."""
        with self._lock:
            if acc_id is None:
                record = {"id": uuid.uuid4().hex[:12]}
                self._accounts.append(record)
            else:
                record = next(a for a in self._accounts if a["id"] == acc_id)
            record.update({k: acc[k] for k in FIELDS})
            self._set_password(record, password)
            self._save()
            return record["id"]

    def delete(self, acc_id):
        with self._lock:
            acc = next((a for a in self._accounts if a["id"] == acc_id), None)
            if acc is None:
                return
            self._accounts.remove(acc)
            self._save()
        if acc.get("password_in_keyring") and keyring:
            try:
                keyring.delete_password(SERVICE, acc_id)
            except Exception:
                pass
