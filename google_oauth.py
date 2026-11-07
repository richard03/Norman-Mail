"""Přihlášení přes Google (OAuth 2.0 pro desktopové aplikace s PKCE a loopback přesměrováním)."""

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from store import DATA_DIR

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://mail.google.com/ openid email"
CONFIG_FILE = DATA_DIR / "google_client.json"
PENDING_TTL = 600  # s – jak dlouho čekáme na dokončení přihlášení

GMAIL_SERVERS = {
    "imap_host": "imap.gmail.com", "imap_port": 993, "imap_security": "ssl",
    "smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_security": "ssl",
}


class OAuthError(Exception):
    """Chyba přihlášení přes Google s hláškou pro uživatele."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# --------------------------------------------------------------------------
# Konfigurace OAuth klienta (z Google Cloud Console)
# --------------------------------------------------------------------------

def load_config():
    try:
        cfg = json.loads(CONFIG_FILE.read_text("utf-8"))
        if cfg.get("client_id") and cfg.get("client_secret"):
            return cfg
    except (OSError, ValueError):
        pass
    return None


def save_config(client_id: str, client_secret: str):
    client_id, client_secret = (client_id or "").strip(), (client_secret or "").strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise OAuthError("Client ID by měl končit na „.apps.googleusercontent.com“.")
    if not client_secret:
        raise OAuthError("Zadejte Client Secret.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"client_id": client_id, "client_secret": client_secret}, indent=2), "utf-8")


def _require_config():
    cfg = load_config()
    if not cfg:
        raise OAuthError("Není nastaven OAuth klient Googlu (Client ID a Client Secret).")
    return cfg


def _post(data: dict) -> dict:
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except ValueError:
            body = {}
        code = body.get("error", f"HTTP {e.code}")
        if code == "invalid_grant":
            raise OAuthError("Přihlášení ke Googlu vypršelo nebo bylo zrušeno – v nastavení účtu se přihlaste znovu.")
        if code == "invalid_client":
            raise OAuthError("Google odmítl Client ID / Client Secret – zkontrolujte nastavení OAuth klienta.")
        raise OAuthError(f"Google vrátil chybu: {body.get('error_description') or code}")
    except OSError as e:
        raise OAuthError(f"Nelze se spojit se serverem Googlu: {e}")


# --------------------------------------------------------------------------
# Průběh přihlášení
# --------------------------------------------------------------------------

_pending = {}
_lock = threading.Lock()


def start(redirect_uri: str, meta: dict):
    """Připraví přihlášení. Vrací (URL pro prohlížeč, state)."""
    cfg = _require_config()
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    with _lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if now - v["created"] > PENDING_TTL]:
            del _pending[k]
        _pending[state] = {**meta, "verifier": verifier, "redirect_uri": redirect_uri,
                           "created": now, "status": "pending", "error": None, "account_id": None}
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",  # zajistí, že Google vrátí refresh token
        "state": state,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    if meta.get("email"):
        params["login_hint"] = meta["email"]
    return AUTH_URL + "?" + urllib.parse.urlencode(params), state


def get_pending(state):
    with _lock:
        entry = _pending.get(state or "")
        return dict(entry) if entry else None


def has_pending() -> bool:
    """Čeká nějaké přihlášení na návrat z prohlížeče?"""
    with _lock:
        now = time.time()
        return any(v["status"] == "pending" and now - v["created"] <= PENDING_TTL for v in _pending.values())


def finish(state, account_id=None, error=None):
    with _lock:
        if state in _pending:
            _pending[state].update(status="error" if error else "done", error=error, account_id=account_id)


def exchange(code: str, entry: dict) -> dict:
    """Vymění autorizační kód za tokeny. Vrací dict s email, refresh_token, access_token, expires_in."""
    cfg = _require_config()
    res = _post({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "redirect_uri": entry["redirect_uri"],
        "code_verifier": entry["verifier"],
    })
    if not res.get("refresh_token"):
        raise OAuthError("Google nevrátil obnovovací token. Odeberte aplikaci na myaccount.google.com/permissions a zkuste to znovu.")
    if "https://mail.google.com/" not in res.get("scope", ""):
        raise OAuthError("Nebyl udělen přístup ke Gmailu – při přihlášení povolte všechna požadovaná oprávnění.")
    # id_token přišel přímo od Googlu přes TLS, podpis proto není nutné ověřovat (OIDC Core 3.1.3.7)
    try:
        payload = res["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        email = claims["email"]
    except (KeyError, IndexError, ValueError):
        raise OAuthError("Nepodařilo se zjistit emailovou adresu účtu Google.")
    return {"email": email, "refresh_token": res["refresh_token"],
            "access_token": res["access_token"], "expires_in": int(res.get("expires_in", 3600))}


# --------------------------------------------------------------------------
# Přístupové tokeny
# --------------------------------------------------------------------------

_tokens = {}  # acc_id -> (access_token, expiry, refresh_token)


def seed(acc_id, access_token, expires_in, refresh_token):
    _tokens[acc_id] = (access_token, time.time() + expires_in, refresh_token)


def access_token(acc: dict) -> str:
    """Platný přístupový token pro účet (acc["password"] obsahuje refresh token)."""
    cached = _tokens.get(acc["id"])
    if cached and cached[2] == acc["password"] and cached[1] > time.time() + 60:
        return cached[0]
    if not acc.get("password"):
        raise OAuthError("Chybí přihlášení ke Googlu – v nastavení účtu se přihlaste znovu.")
    cfg = _require_config()
    res = _post({"grant_type": "refresh_token", "refresh_token": acc["password"],
                 "client_id": cfg["client_id"], "client_secret": cfg["client_secret"]})
    seed(acc["id"], res["access_token"], int(res.get("expires_in", 3600)), acc["password"])
    return res["access_token"]


def xoauth2_string(acc: dict) -> str:
    return f"user={acc['username']}\x01auth=Bearer {access_token(acc)}\x01\x01"
