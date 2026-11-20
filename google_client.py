"""OAuth klient Norman Mail pro přihlášení přes Google. Bez závislostí, protože ho používá i NormanMail.spec
a tools/release.py.

Client ID a Secret nejsou v repozitáři. Vývojář je má v souboru build_client_file() (stejně jako klíč pro
podepisování aktualizací) a NormanMail.spec je při sestavení přibalí do exe jako BUILTIN_NAME. U desktopových
aplikací Google nepovažuje Client Secret za tajný (RFC 8252), bezpečnost přihlášení zajišťuje PKCE.
Mimo repozitář je jen proto, aby ho z veřejného kódu nikdo nepřevzal pro vlastní aplikaci.
"""

import json
import os
from pathlib import Path

BUILTIN_NAME = "google_client.json"


def build_client_file() -> Path:
    """Soubor s OAuth klientem na počítači vývojáře (proměnná NORMANMAIL_GOOGLE_CLIENT, jinak
    %USERPROFILE%\\.normanmail\\google_client.json)."""
    return Path(os.environ.get("NORMANMAIL_GOOGLE_CLIENT") or Path.home() / ".normanmail" / BUILTIN_NAME)


def parse_client(text: str):
    """Client ID a Secret: buď {"client_id", "client_secret"}, nebo JSON stažený z Google Cloud Console
    ({"installed": {...}}). Vrací {"client_id", "client_secret"}, nebo None."""
    try:
        cfg = json.loads(text)
    except ValueError:
        return None
    if isinstance(cfg, dict) and isinstance(cfg.get("installed"), dict):
        cfg = cfg["installed"]
    if not isinstance(cfg, dict):
        return None
    cid, secret = str(cfg.get("client_id") or "").strip(), str(cfg.get("client_secret") or "").strip()
    if cid.endswith(".apps.googleusercontent.com") and secret:
        return {"client_id": cid, "client_secret": secret}
    return None
