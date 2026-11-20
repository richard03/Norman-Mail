"""Norman Mail – jádro aplikace (Flask). V desktopové verzi (main.py) neběží žádný server: rozhraní volá
endpointy přes most pywebview uvnitř procesu. Jako skutečný server na 127.0.0.1 běží jen při vývoji
(tools/dev_server.py) a krátce při přihlášení přes Google (návrat z prohlížeče na /oauth/callback)."""

import base64
import io
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from html import escape
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, redirect, request, send_file
from werkzeug.serving import make_server

import bayes
import db
import google_oauth
import i18n
from i18n import t as tr
import mail
import sync
import taskbar
import updater
from store import DATA_DIR, KEYRING_OK, AccountStore, ValidationError, normalize, portable_dir
from version import VERSION

HOST = "127.0.0.1"
PORT = int(os.environ.get("MAILER_PORT", "8765"))
EMBEDDED = False  # desktopová verze: žádný trvalý server (nastavuje main.py)
QUIT = None  # zavře okno desktopové aplikace (nastavuje main.py; restart po aktualizaci)
PAGE_SIZE = 50
UNIFIED_PAGE_SIZE = 30
LOCAL_HOSTS = {"127.0.0.1", "localhost"}

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024  # přílohy v base64
store = AccountStore()
pool = mail.ImapPool()     # spojení pro akce uživatele
bg_pool = mail.ImapPool()  # samostatná spojení pro práci na pozadí (kontext, hromadný spam)
jobs = {}                  # id -> stav úlohy na pozadí
jobs_lock = threading.Lock()
context_seq = {}           # acc_id -> pořadové číslo posledního požadavku na kontext
database = db.Database()   # lokální cache metadat a těl zpráv
syncer = sync.SyncManager(store, database, mail.ImapPool())  # vlastní spojení pro synchronizaci


def refresh_my_addresses():
    """Adresy účtů do databáze – podle nich se pozná, zda je zpráva přímo pro mě (Důležité / Vzít na vědomí)."""
    for a in store.list_public():
        database.set_my_addresses(a["id"], {a["email"], a.get("username") or ""})


def migrate_language():
    """Do verze 1.0.2 znamenal nezvolený jazyk „podle Windows“, teď angličtinu. Kdo aplikaci už používá
    (má účet), dostane natrvalo jazyk, který viděl dosud, aby se mu po aktualizaci nepřepnul."""
    if not database.settings()["language"] and store.list_public():
        win = i18n.windows_language()
        database.update_settings({"language": win if win in {l["code"] for l in i18n.languages()} else i18n.BASE})


refresh_my_addresses()
migrate_language()
i18n.set_language(database.settings()["language"])


def start_background():
    """Spustí synchronizaci na pozadí (volá desktopová aplikace i webový režim)."""
    syncer.start()


def drop_connections(acc_id):
    pool.drop(acc_id)
    bg_pool.drop(acc_id)
    syncer.pool.drop(acc_id)


# --------------------------------------------------------------------------
# Zabezpečení: jen lokální přístup, ochrana proti CSRF a DNS rebindingu
# --------------------------------------------------------------------------

@app.before_request
def guard():
    if (request.host or "").rsplit(":", 1)[0] not in LOCAL_HOSTS:
        abort(403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # Vlastní hlavička vynutí CORS preflight, který cizí stránka neprojde.
        if request.headers.get("X-Mailer") != "1":
            abort(403)
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).hostname not in LOCAL_HOSTS:
            abort(403)


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; object-src 'none'; frame-src 'self'; "
        "img-src 'self' data: https: http:; style-src 'self' 'unsafe-inline' https:; font-src 'self' data: https:"
    )
    return resp


@app.errorhandler(mail.MailError)
@app.errorhandler(ValidationError)
@app.errorhandler(google_oauth.OAuthError)
def handle_user_error(e):
    return jsonify(error=str(e)), 400


@app.errorhandler(Exception)
def handle_error(e):
    if hasattr(e, "code") and hasattr(e, "description"):  # HTTP výjimky Flasku
        return jsonify(error=e.description), e.code
    app.logger.exception("Chyba")
    return jsonify(error=mail.friendly_error(e)), 502


def account_or_404(acc_id):
    acc = store.get(acc_id)
    if acc is None:
        abort(404, tr("err.accountMissing"))
    return acc


def folder_arg(data=None):
    folder = (data or request.args).get("folder")
    if not folder:
        abort(400, tr("err.folderParamMissing"))
    return folder


# --------------------------------------------------------------------------
# Stránky a účty
# --------------------------------------------------------------------------

@app.get("/")
def index():  # jen vývojový server – desktop načítá static/index.html přímo
    return redirect("/static/index.html")


@app.get("/api/accounts")
def accounts_list():
    return jsonify(store.list_public())


@app.get("/api/info")
def info():
    return jsonify(keyring=KEYRING_OK)


SHORTCUT_NAME = "Norman Mail"


@app.get("/api/app")
def app_info():
    """Verze a stav zástupců (Nastavení → Aplikace)."""
    shortcuts = {}
    if EMBEDDED:
        for where in taskbar.CSIDL:
            try:
                shortcuts[where] = os.path.exists(taskbar.shortcut_path(where, SHORTCUT_NAME))
            except OSError:
                pass
    return jsonify(version=VERSION, portable=bool(portable_dir()), shortcuts=shortcuts, data_dir=str(DATA_DIR))


@app.get("/api/update")
def update_status():
    """Stav automatické aktualizace (Nastavení → O aplikaci)."""
    st = updater.status()
    err = st.pop("error")
    st["error"] = tr(err[0], **err[1]) if err else None
    st["auto"] = database.settings()["auto_update"]
    return jsonify(st)


@app.post("/api/update/check")
def update_check():
    updater.check_async()
    return jsonify(ok=True)


@app.post("/api/update/install")
def update_install():
    """Nainstaluje staženou verzi a restartuje aplikaci (zavře okno, main.py spustí novou verzi)."""
    if not EMBEDDED or QUIT is None:
        return jsonify(error=tr("error.desktopOnly")), 400
    try:
        updater.install(restart=True)
    except updater.UpdateError as e:
        return jsonify(error=tr(e.key, **e.params)), 409
    threading.Timer(0.3, QUIT).start()  # odpověď ještě stihne dojít do okna
    return jsonify(ok=True)


@app.post("/api/app/shortcut")
def app_shortcut():
    """Zástupce v nabídce Start / na ploše (aplikace je jediný exe bez instalátoru)."""
    where = (request.get_json(force=True) or {}).get("where")
    if not EMBEDDED or where not in taskbar.CSIDL:
        return jsonify(error=tr("error.desktopOnly")), 400
    try:
        taskbar.create_shortcut(where, SHORTCUT_NAME)
    except OSError as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True)


LOGIN_FIELDS = ("username", "imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port", "smtp_security")


def _login_target(acc):
    """Kam a pod jakým jménem se posílá heslo. Při změně je potřeba heslo zadat znovu."""
    return tuple(str(acc.get(k) or "").strip().lower() for k in LOGIN_FIELDS)


def _save_account(acc_id):
    data = request.get_json(force=True)
    existing = store.get(acc_id) if acc_id else None
    # Typ přihlášení se z formuláře měnit nedá – OAuth účty vznikají přes /oauth/callback.
    data["auth"] = (existing or {}).get("auth") or "password"
    if data["auth"] == "oauth2":
        # Účet přes Google: servery jsou vždy Gmail a adresa ta, pro kterou Google vydal token –
        # jinak by se token Googlu poslal na libovolný server.
        data.update(google_oauth.GMAIL_SERVERS, email=existing["email"], username=existing["username"])
    acc = normalize(data)
    password = "" if acc["auth"] == "oauth2" else (data.get("password") or "")
    if existing and not password:
        if acc["auth"] != "oauth2" and _login_target(acc) != _login_target(existing):
            # uložené heslo se smí poslat jen tam, kam patří – na nový server nebo pro jiné jméno ho nepoužijeme
            raise ValidationError(tr("err.passwordAgain"))
        password = existing["password"]
    if not password:
        raise ValidationError(tr("err.passwordMissing"))
    mail.test_account({**acc, "password": password, "id": acc_id or "test"})
    new_id = store.save(acc_id, acc, password)
    refresh_my_addresses()
    drop_connections(new_id)
    if existing and (existing["imap_host"], existing["username"]) != (acc["imap_host"], acc["username"]):
        database.delete_account(new_id)  # jiná schránka – stará cache neplatí
    syncer.trigger()
    return jsonify(id=new_id)


@app.post("/api/accounts")
def accounts_create():
    return _save_account(None)


@app.put("/api/accounts/<acc_id>")
def accounts_update(acc_id):
    account_or_404(acc_id)
    return _save_account(acc_id)


@app.delete("/api/accounts/<acc_id>")
def accounts_delete(acc_id):
    drop_connections(acc_id)
    store.delete(acc_id)
    database.delete_account(acc_id)
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# Přihlášení přes Google
# --------------------------------------------------------------------------

class OAuthListener:
    """Dočasný server pro návrat z přihlášení Googlu (desktop nemá trvalý server). Přijímá jen
    GET /oauth/callback, běží na náhodném portu a vypne se, jakmile žádné přihlášení nečeká
    (nebo po vypršení PENDING_TTL)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._srv = None
        self._deadline = 0.0

    @staticmethod
    def _wsgi(environ, start_response):
        if environ.get("PATH_INFO") != "/oauth/callback" or environ.get("REQUEST_METHOD") != "GET":
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"Not found"]
        return app.wsgi_app(environ, start_response)

    def redirect_uri(self):
        with self._lock:
            if self._srv is None:
                self._srv = make_server(HOST, 0, self._wsgi, threaded=True)
                threading.Thread(target=self._srv.serve_forever, name="oauth-callback", daemon=True).start()
                threading.Thread(target=self._reaper, args=(self._srv,), daemon=True).start()
            self._deadline = time.time() + google_oauth.PENDING_TTL
            return f"http://{HOST}:{self._srv.server_port}/oauth/callback"

    def _reaper(self, srv):
        time.sleep(5)  # start() mezitím založí čekající přihlášení
        while True:
            with self._lock:
                if self._srv is not srv:
                    return
                if time.time() > self._deadline or not google_oauth.has_pending():
                    self._srv = None
                    break
            time.sleep(2)
        srv.shutdown()
        srv.server_close()

    @property
    def running(self):
        return self._srv is not None


oauth_listener = OAuthListener()


def redirect_uri():
    if EMBEDDED:
        return oauth_listener.redirect_uri()
    return f"http://{HOST}:{PORT}/oauth/callback"


@app.get("/api/google/config")
def google_config():
    cfg = google_oauth.load_config()
    return jsonify(configured=bool(cfg), client_id=(cfg or {}).get("client_id", ""))


@app.post("/api/google/config")
def google_config_save():
    data = request.get_json(force=True)
    google_oauth.save_config(data.get("client_id"), data.get("client_secret"))
    return jsonify(ok=True)


@app.post("/api/oauth/start")
def oauth_start():
    data = request.get_json(force=True)
    acc_id = data.get("account_id") or None
    if acc_id:
        account_or_404(acc_id)
    meta = {k: str(data.get(k) or "").strip() for k in ("name", "display_name", "email")}
    url, state = google_oauth.start(redirect_uri(), {**meta, "target_id": acc_id})
    return jsonify(url=url, state=state)


@app.get("/api/oauth/status")
def oauth_status():
    entry = google_oauth.get_pending(request.args.get("state"))
    if not entry:
        return jsonify(status="error", error=tr("err.oauthExpired"))
    return jsonify(status=entry["status"], error=entry["error"], account_id=entry["account_id"])


CALLBACK_PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>{page_title}</title>
<style>body{{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;margin:0;
background:#f6f7f9;color:#1c2430}}div{{background:#fff;padding:28px 36px;border-radius:12px;max-width:480px;
box-shadow:0 4px 20px rgba(0,0,0,.08)}}h1{{font-size:20px;color:{color}}}</style></head>
<body><div><h1>{title}</h1><p>{text}</p></div></body></html>"""


def callback_page(ok, text):
    title, color = (tr("oauth.pageOk"), "#059669") if ok else (tr("oauth.pageFailed"), "#c62828")
    html = CALLBACK_PAGE.format(title=escape(title), color=color, text=escape(text), lang=escape(i18n.current()),
                                page_title=escape(tr("oauth.pageTitle")))
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/oauth/callback")
def oauth_callback():
    state = request.args.get("state")
    entry = google_oauth.get_pending(state)
    if not entry or entry["status"] != "pending":
        return callback_page(False, tr("oauth.invalidRequest"))
    if request.args.get("error") or not request.args.get("code"):
        google_oauth.finish(state, error=tr("oauth.cancelled"))
        return callback_page(False, tr("oauth.cancelledClose"))
    try:
        tok = google_oauth.exchange(request.args["code"], entry)
        email_addr = tok["email"]
        if entry["target_id"]:
            existing = store.get(entry["target_id"])
            if existing and existing["email"].lower() != email_addr.lower():
                raise google_oauth.OAuthError(
                    tr("oauth.wrongAccount", email=email_addr, expected=existing["email"]))
        else:
            existing = store.find_by_email(email_addr)
        existing = existing or {}
        acc = normalize({
            **google_oauth.GMAIL_SERVERS,
            "email": email_addr, "username": email_addr, "auth": "oauth2",
            "name": entry["name"] or existing.get("name") or email_addr,
            "display_name": entry["display_name"] or existing.get("display_name") or "",
        })
        test_id = "oauth-" + state[:8]
        google_oauth.seed(test_id, tok["access_token"], tok["expires_in"], tok["refresh_token"])
        mail.test_account({**acc, "id": test_id, "password": tok["refresh_token"]})
        new_id = store.save(existing.get("id"), acc, tok["refresh_token"])
        google_oauth.seed(new_id, tok["access_token"], tok["expires_in"], tok["refresh_token"])
        drop_connections(new_id)
        refresh_my_addresses()
        syncer.trigger()
        google_oauth.finish(state, account_id=new_id)
        return callback_page(True, tr("oauth.connected", email=email_addr))
    except Exception as e:
        msg = mail.friendly_error(e)
        google_oauth.finish(state, error=msg)
        return callback_page(False, msg)


# --------------------------------------------------------------------------
# Složky a zprávy
# --------------------------------------------------------------------------

@app.get("/api/accounts/<acc_id>/folders")
def folders(acc_id):
    """Seznam složek z databáze (udržuje ho synchronizace) – bez čekání na server.
    Ze serveru jen u účtu, který ještě nebyl synchronizován."""
    acc = account_or_404(acc_id)
    cached = [f for f in database.folders(acc_id) if f.get("display")]
    if any(f["role"] == "inbox" for f in cached):
        unread = database.unread_counts().get(acc_id, 0)
        order = lambda f: (mail.ROLE_ORDER.index(f["role"]) if f["role"] in mail.ROLE_ORDER else len(mail.ROLE_ORDER),
                           mail.decode_utf7(f["name"]).lower())
        return jsonify([{"name": f["name"], "path": mail.decode_utf7(f["name"]), "display": f["display"],
                         "depth": f["depth"] or 0, "role": f["role"],
                         "unseen": unread if f["role"] == "inbox" else 0} for f in sorted(cached, key=order)])
    with pool.get(acc) as M:
        return jsonify(mail.list_folders(M))


def list_window(default_size):
    """offset/limit z dotazu (UI: další zprávy od počtu zobrazených; obnovení všech zobrazených najednou)."""
    offset = request.args.get("offset", type=int)
    limit = request.args.get("limit", type=int) or default_size
    return (max(0, offset) if offset is not None else None), max(1, min(limit, 2000))


def unread_arg():
    """Přepínač „jen nepřečtené“: None = všechny zprávy, jinak (keep,) – keep = (účet, složka, uid)
    právě otevřené zprávy, která v seznamu zůstane, i když už je přečtená (?unread=1&keep_acc=…)."""
    if request.args.get("unread") != "1":
        return None
    acc, folder, uid = request.args.get("keep_acc"), request.args.get("keep_folder"), request.args.get("keep_uid", type=int)
    return ((acc, folder, uid) if acc and folder and uid is not None else None,)


def query_arg():
    """Text filtru seznamu (?q=) – filtruje se v uložených zprávách (předmět, odesílatel, příjemci)."""
    return " ".join((request.args.get("q") or "").split()) or None


def unread_only(messages, unread):
    """Výsledky hledání na serveru: stejné omezení jako unread_arg v databázi."""
    if unread is None:
        return messages
    keep = unread[0]
    return [m for m in messages if not m["seen"] or (m["account_id"], m["folder"], m["uid"]) == keep]


@app.get("/api/accounts/<acc_id>/messages")
def messages(acc_id):
    acc = account_or_404(acc_id)
    page = max(0, request.args.get("page", 0, type=int))
    folder = folder_arg()
    syncer.ensure_synced(acc, folder)
    offset, limit = list_window(PAGE_SIZE)
    version = database.version  # verze dat, ze které seznam vzniká (UI podle ní pozná, že je zastaralý)
    return jsonify(**database.list_messages([acc_id], folder, page, limit, offset=offset, unread=unread_arg(),
                                            search=query_arg()), version=version)


@app.get("/api/unified")
def unified():
    """Sloučená doručená pošta ze všech účtů – z lokální databáze."""
    page = max(0, request.args.get("page", 0, type=int))
    view = request.args.get("filter", "all")
    if view not in db.VIEW_FILTERS:
        abort(400, tr("err.unknownView"))
    accounts = store.all()
    if not accounts:
        return jsonify(messages=[], has_more=False, errors=[])
    errors = []

    # účty, které ještě nikdy nebyly synchronizovány, stáhneme hned (paralelně)
    fresh = [a for a in accounts if not (database.folder(a["id"], "INBOX") or {}).get("last_sync")]
    with ThreadPoolExecutor(max_workers=8) as ex:
        for acc, fut in [(a, ex.submit(syncer.ensure_synced, a)) for a in fresh]:
            try:
                fut.result()
            except Exception as e:
                errors.append(f"{acc['name']}: {mail.friendly_error(e)}")
    for acc in accounts:
        if acc["id"] in syncer.errors and acc not in fresh:
            errors.append(tr("err.showingCached", account=acc["name"], error=syncer.errors[acc["id"]]))
    offset, limit = list_window(UNIFIED_PAGE_SIZE)
    version = database.version
    res = database.list_messages([a["id"] for a in accounts], "INBOX", page, limit, view, offset=offset,
                                 unread=unread_arg(), search=query_arg())
    return jsonify(messages=res["messages"], has_more=res["has_more"], errors=errors, version=version)


def _mark_seen_in_background(acc, folder, uid):
    syncer.enqueue(acc, folder, uid, tr("op.markRead"),
                   lambda M: mail.set_flag(M, folder, uid, "\\Seen", True))


@app.get("/api/accounts/<acc_id>/messages/<int:uid>")
def message(acc_id, uid):
    """Zpráva z cache; ze serveru se stahuje jen poprvé."""
    acc = account_or_404(acc_id)
    folder = folder_arg()
    row = database.message(acc_id, folder, uid)
    raw = database.get_body(acc_id, folder, uid)
    if raw is None:
        try:
            with pool.get(acc) as M:
                raw, flags, size = mail.fetch_raw_mark_seen(M, folder, uid)
        except mail.MessageGone as e:  # zastaralá cache – zprávu odebrat, ať zmizí i ze seznamu
            database.remove_message(acc_id, folder, uid)
            database.bump()
            return jsonify(error=str(e), gone=True), 404
        if size <= mail.MAX_MESSAGE:  # useknutý začátek obří zprávy se neukládá
            database.put_body(acc_id, folder, uid, raw)
    else:
        size = None
        flags = ["\\Seen"] + (["\\Flagged"] if row and row["flagged"] else [])
        if row and not row["seen"]:
            _mark_seen_in_background(acc, folder, uid)
    database.set_flag(acc_id, folder, uid, "seen", True)
    data = mail.parse_message(raw, uid, folder, flags, size)
    data["snoozed_until"] = row["snoozed_until"] if row else None
    data["todo"] = bool(row and row["todo"])
    data["view"] = row["view"] if row else None
    if row:
        data["notification"], data["unimportant"] = row["notification"], row["unimportant"]
    else:
        cat = database.categorizer()({**data, "account_id": acc_id,
                                      "base_subject": mail.base_subject(data["subject"]).lower()})
        data["notification"], data["unimportant"] = cat == "notifications", cat == "unimportant"
    data["bayes"] = None
    if row and database.spam_folders().get(acc_id) == folder:  # proč to filtr dal do spamu
        v = syncer.bayes.verdict(bayes.record_key(row))
        if v and v["moved"]:
            data["bayes"] = {"score": v["score"], "tokens": syncer.bayes.explain(v["tokens"])}
    return jsonify(data)


@app.get("/api/accounts/<acc_id>/messages/<int:uid>/attachments/<int:index>")
def attachment(acc_id, uid, index):
    acc = account_or_404(acc_id)
    folder = folder_arg()
    raw = database.get_body(acc_id, folder, uid)
    if raw is not None:
        (filename, _ctype, data), = mail.attachments_from_raw(raw, [index])
    else:
        with pool.get(acc) as M:
            (filename, _ctype, data), = mail.get_attachments(M, folder, uid, [index])
    # Vždy jako stažení – HTML příloha se nesmí vykreslit v kontextu aplikace.
    return send_file(io.BytesIO(data), mimetype="application/octet-stream",
                     as_attachment=True, download_name=filename)


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/flags")
def message_flags(acc_id, uid):
    acc = account_or_404(acc_id)
    data = request.get_json(force=True)
    flag, value = data.get("flag"), bool(data.get("value"))
    with pool.get(acc) as M:
        mail.set_flag(M, folder_arg(data), uid, flag, value)
    database.set_flag(acc_id, folder_arg(data), uid, "seen" if flag == "\\Seen" else "flagged", value)
    return jsonify(ok=True)


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/delete")
def message_delete(acc_id, uid):
    """Smazání: z databáze (a tím z UI) hned, na serveru na pozadí."""
    acc = account_or_404(acc_id)
    folder = folder_arg(request.get_json(force=True))
    syncer.enqueue(acc, folder, uid, tr("op.delete"), lambda: syncer.trash_messages(acc, folder, [uid]),
                   remove=True, use_conn=False)
    database.remove_message(acc_id, folder, uid)
    database.bump()
    return jsonify(ok=True)


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/archive")
def message_archive(acc_id, uid):
    acc = account_or_404(acc_id)
    folder = folder_arg(request.get_json(force=True))
    syncer.enqueue(acc, folder, uid, tr("op.archive"), lambda M: mail.archive_message(M, folder, uid), remove=True)
    database.remove_message(acc_id, folder, uid)
    database.bump()
    return jsonify(ok=True)


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/todo")
def message_todo(acc_id, uid):
    """ToDo: {value: true} přesune zprávu doručené pošty do pohledu ToDo, {value: false} (Vyřešeno) ji vrátí
    do pohledu, kam patří. Jen místní značka, na serveru se nic nemění. Vrací pohled, kam zpráva teď patří."""
    account_or_404(acc_id)
    data = request.get_json(force=True) or {}
    folder = folder_arg(data)
    rec = database.message(acc_id, folder, uid)
    if folder != "INBOX" or rec is None:
        raise ValidationError(tr("err.todoInboxOnly"))
    database.set_todo(rec, bool(data.get("value", True)))
    database.bump()
    return jsonify(ok=True, view=database.view_of(acc_id, folder, uid))


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/snooze")
def message_snooze(acc_id, uid):
    """Odložení: označí jako přečtenou a v čase `until` (unix ts) zase jako nepřečtenou.
    V databázi hned, příznak na serveru na pozadí."""
    acc = account_or_404(acc_id)
    data = request.get_json(force=True)
    folder = folder_arg(data)
    try:
        until = float(data.get("until"))
    except (TypeError, ValueError):
        abort(400, tr("err.snoozeMissing"))
    if until <= time.time():
        abort(400, tr("err.snoozePast"))
    row = database.message(acc_id, folder, uid)
    if row:
        message_id = row["message_id"]
    else:
        with pool.get(acc) as M:
            mail._select(M, folder, readonly=True)
            recs = mail.fetch_header_records(M, [uid])
        message_id = recs[0]["message_id"] if recs else ""
    database.add_snooze(acc_id, folder, uid, message_id, until)
    database.set_flag(acc_id, folder, uid, "seen", True)
    syncer.enqueue(acc, folder, uid, tr("op.snooze"), lambda M: mail.set_flag(M, folder, uid, "\\Seen", True))
    return jsonify(ok=True, until=until)


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/unsnooze")
def message_unsnooze(acc_id, uid):
    account_or_404(acc_id)
    database.remove_snooze(acc_id, folder_arg(request.get_json(force=True)), uid)
    return jsonify(ok=True)


def _unmark_others(rec, category):
    """Zruší označení zprávy jinou kategorií nebo jako důležitá (to by mělo před pravidlem přednost)."""
    for mark in db.MARKS:
        if mark != category:
            database.unmark_message(rec, mark)


def _message_record(acc, acc_id, folder, uid):
    """Záznam zprávy z databáze, jinak hlavičky ze serveru."""
    rec = database.message(acc_id, folder, uid)
    if rec is None:
        with pool.get(acc) as M:
            mail._select(M, folder, readonly=True)
            recs = mail.fetch_header_records(M, [uid])
        if not recs:
            raise mail.MailError(tr("err.messageGoneShort"))
        rec = {**recs[0], "account_id": acc_id, "folder": folder}
    return rec


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/important")
def message_important(acc_id, uid):
    """Tlačítko Důležité (ve Vzít na vědomí, Nedůležitých a Notifikacích): zpráva přestane patřit do kategorií
    (zruší se i pravidla, která jí odpovídají – jako dřív „Není nedůležité“) a je označená jako důležitá."""
    acc = account_or_404(acc_id)
    folder = folder_arg(request.get_json(force=True))
    rec = _message_record(acc, acc_id, folder, uid)
    for cat in db.CATEGORIES:
        database.uncategorize(rec, cat)
    database.mark_message(rec, "important")
    database.bump()
    view = database.view_of(acc_id, folder, uid) if folder == "INBOX" and database.message(acc_id, folder, uid) else None
    return jsonify(ok=True, view=view)


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/categorize")
def message_categorize(acc_id, uid):
    """Notifikace / Nedůležité. scope: message = jen tato zpráva (bez pravidla), sender / subject = pravidlo
    pro všechny zprávy od odesílatele / se stejným předmětem, custom = vlastní pravidlo z data.rule
    (odesílatel, příjemce, část předmětu); value=false zprávu z kategorie vyřadí (zruší i pravidla,
    která jí odpovídají)."""
    acc = account_or_404(acc_id)
    data = request.get_json(force=True)
    folder = folder_arg(data)
    category, scope = data.get("category"), data.get("scope") or "message"
    if category not in db.CATEGORIES or scope not in ("message", "sender", "subject", "custom"):
        abort(400, tr("err.unknownCategory"))
    rule = _category_rule_fields(data.get("rule") or {}) if scope == "custom" and data.get("value", True) else None
    rec = _message_record(acc, acc_id, folder, uid)
    sender = (rec["from"][0]["addr"] if rec["from"] else "").lower()
    subject = rec.get("base_subject") or ""
    if not data.get("value", True):
        database.uncategorize(rec, category)
    elif scope == "message":
        database.mark_message(rec, category)
    elif scope == "custom":
        _unmark_others(rec, category)  # označení zprávy by mělo před pravidlem přednost
        database.add_category_rule(category, **rule)
    else:
        value = sender if scope == "sender" else subject
        if not value:
            raise mail.MailError(tr("err.noSender") if scope == "sender" else tr("err.noSubject"))
        _unmark_others(rec, category)  # označení zprávy by mělo před pravidlem přednost
        database.add_category_rule(category, **{scope: value})
    database.bump()
    view = database.view_of(acc_id, folder, uid) if folder == "INBOX" and database.message(acc_id, folder, uid) else None
    return jsonify(ok=True, sender=sender, subject=subject, view=view)


# --------------------------------------------------------------------------
# Uživatelské složky (na serveru: stejnojmenná složka v každém účtu, u Gmailu štítek)
# --------------------------------------------------------------------------

RESERVED_FOLDER_NAMES = {"inbox"} | {n for names in mail.ROLE_NAMES.values() for n in names}


def _folder_or_404(folder_id):
    f = database.user_folder(folder_id)
    if f is None:
        abort(404, tr("err.folderMissing"))
    return f


def _folder_config(data, current_id=None):
    """Ověří konfiguraci složky: (název, účet, imap název složky, po přečtení, označit přečtené)."""
    name = " ".join(str(data.get("name") or "").split())
    if not name or len(name) > 60:
        raise ValidationError(tr("err.folderName"))
    if any(f["name"].lower() == name.lower() and f["id"] != current_id for f in database.user_folders()):
        raise ValidationError(tr("err.folderExists", name=name))
    acc_id = data.get("account_id")
    if not acc_id or store.get(acc_id) is None:
        raise ValidationError(tr("err.folderAccount"))
    if data.get("new_folder"):  # nová složka na serveru
        new = " ".join(str(data["new_folder"]).split())
        if len(new) > 60 or any(ch in new for ch in '/\\"*%'):
            raise ValidationError(tr("err.serverFolderName"))
        if new.lower() in RESERVED_FOLDER_NAMES:
            raise ValidationError(tr("err.folderReserved", name=new))
        imap_name = mail.encode_utf7(new)
    else:  # existující složka na serveru
        imap_name = str(data.get("server_folder") or "")
        known = {f["name"]: f for f in database.folders(acc_id)}
        if imap_name not in known:
            raise ValidationError(tr("err.serverFolderChoose"))
        if known[imap_name]["role"] in ("inbox", "trash", "junk", "drafts", "sent"):
            raise ValidationError(tr("err.systemFolder"))
    if any(f["account_id"] == acc_id and f["imap_name"] == imap_name and f["id"] != current_id
           for f in database.user_folders()):
        raise ValidationError(tr("err.folderTaken"))
    return name, acc_id, imap_name, bool(data.get("after_read")), bool(data.get("mark_read"))


@app.get("/api/user-folders")
def user_folders():
    return jsonify(database.user_folders())


@app.post("/api/user-folders")
def user_folder_create():
    folder_id = database.create_user_folder(*_folder_config(request.get_json(force=True)))
    syncer.trigger()
    return jsonify(database.user_folder(folder_id))


@app.put("/api/user-folders/<int:folder_id>")
def user_folder_update(folder_id):
    _folder_or_404(folder_id)
    database.update_user_folder(folder_id, *_folder_config(request.get_json(force=True), folder_id))
    database.bump()
    syncer.trigger()  # nový cíl / režim uplatnit hned
    return jsonify(database.user_folder(folder_id))


@app.delete("/api/user-folders/<int:folder_id>")
def user_folder_delete(folder_id):
    """Smaže složku z aplikace (konfiguraci a pravidla). S ?server=1 i na serveru včetně všech zpráv
    (zprávy do koše) – to běží na pozadí."""
    f = _folder_or_404(folder_id)
    on_server = request.args.get("server") == "1"
    acc = store.get(f["account_id"]) if f["account_id"] else None
    if on_server and acc is None:
        raise ValidationError(tr("err.folderNoAccountDelete"))
    database.delete_user_folder(folder_id)
    if on_server:
        database.clear_folder(acc["id"], f["imap_name"])  # z UI hned
        threading.Thread(target=syncer.delete_server_folder, args=(acc, f["imap_name"], f["name"]),
                         daemon=True).start()
    database.bump()
    return jsonify(ok=True, server=on_server)


@app.get("/api/user-folders/<int:folder_id>/rules")
def user_folder_rules(folder_id):
    _folder_or_404(folder_id)
    return jsonify(database.folder_rules(folder_id))


@app.post("/api/user-folders/<int:folder_id>/rules")
def user_folder_rule_add(folder_id):
    _folder_or_404(folder_id)
    data = request.get_json(force=True)
    kind, value = _rule_arg(data)
    database.add_folder_rule(folder_id, kind, value, bool(data.get("after_read")))
    syncer.trigger()  # existující zprávy podle pravidla přesunout
    return jsonify(database.folder_rules(folder_id))


def _rule_arg(data):
    """Typ a hodnota pravidla složky z požadavku (zkontrolované, hodnota malými písmeny)."""
    kind = data.get("kind") or "sender"
    value = " ".join(str(data.get("value") or data.get("sender") or "").split()).lower()
    if kind not in db.RULE_KINDS:
        raise ValidationError(tr("err.unknownRuleKind"))
    if kind in ("sender", "recipient") and not mail.ADDR_RE.match(value):
        raise ValidationError(tr("err.invalidEmail"))
    if kind == "subject" and not 2 <= len(value) <= 100:
        raise ValidationError(tr("err.subjectRuleLength"))
    return kind, value


def _update_rule(table, rule_id, kind, value, after_read=None):
    if not database.update_rule(table, rule_id, kind, value, after_read):
        raise ValidationError(tr("err.ruleExists"))
    database.bump()
    syncer.trigger()  # zprávy podle upraveného pravidla
    return jsonify(ok=True, kind=kind, value=value)


@app.put("/api/user-folder-rules/<int:rule_id>")
def user_folder_rule_update(rule_id):
    data = request.get_json(force=True)
    kind, value = _rule_arg(data)
    return _update_rule("user_folder_rules", rule_id, kind, value, bool(data.get("after_read")))


@app.delete("/api/user-folder-rules/<int:rule_id>")
def user_folder_rule_delete(rule_id):
    database.delete_folder_rule(rule_id)
    return jsonify(ok=True)


@app.get("/api/user-folders/<int:folder_id>/messages")
def user_folder_messages(folder_id):
    f = _folder_or_404(folder_id)
    if not f["account_id"] or store.get(f["account_id"]) is None:
        return jsonify(messages=[], has_more=False, version=database.version,
                       errors=[tr("err.folderNoAccountConfig")])
    page = max(0, request.args.get("page", 0, type=int))
    offset, limit = list_window(UNIFIED_PAGE_SIZE)
    version = database.version
    res = database.list_messages([f["account_id"]], f["imap_name"], page, limit, offset=offset, unread=unread_arg(),
                                 search=query_arg())
    return jsonify(messages=res["messages"], has_more=res["has_more"], errors=[], version=version)


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/to-folder")
def message_to_folder(acc_id, uid):
    """Nastavit pravidlo: vytvoří pravidlo složky (odesílatel / příjemce / část předmětu, ihned / po přečtení)
    a zprávu do složky hned přesune (i na jiný účet). Bez kind/value = pravidlo podle odesílatele.
    UI a databáze hned, server na pozadí."""
    acc = account_or_404(acc_id)
    data = request.get_json(force=True)
    folder = folder_arg(data)
    target = _folder_or_404(int(data.get("target_id") or 0))
    if not target["account_id"]:
        raise ValidationError(tr("err.folderNoAccountNamed", name=target["name"]))
    row = database.message(acc_id, folder, uid)
    if not data.get("value"):  # původní chování: pravidlo podle odesílatele zprávy
        data = {**data, "kind": "sender", "value": row["from"][0]["addr"] if row and row["from"] else ""}
    kind, value = _rule_arg(data)
    rule_id = database.add_folder_rule(target["id"], kind, value, bool(data.get("after_read")))
    in_target = target["account_id"] == acc_id and target["imap_name"] == folder
    if row and not in_target and db.rule_matches({"kind": kind, "value": value}, row):
        database.add_folder_rule_hits(rule_id, 1)
    if not in_target:
        syncer.enqueue(acc, folder, uid, tr("op.moveToFolder", folder=target["name"]),
                       lambda: syncer.move_messages(acc, folder, [uid], target), remove=True, use_conn=False)
        database.remove_message(acc_id, folder, uid)
    database.bump()
    syncer.trigger()  # další zprávy podle pravidla
    return jsonify(ok=True, kind=kind, value=value, folder=target["name"], moved=not in_target)


# --------------------------------------------------------------------------
# Přesun více zpráv najednou (drag & drop, hromadné mazání) a složka Smazané
# --------------------------------------------------------------------------

MAX_BULK = 5000
INBOX_VIEWS = ("important", "fyi", "unimportant", "notifications", "all")
CATEGORY_SETTING = {"notifications": "notify", "unimportant": "unimp"}  # předpona klíčů nastavení


def _parse_move(data):
    """Zprávy a cíl přesunu. Cíl: {type: trash} | {type: spam} | {type: ufolder, id} | {type: inbox, view?, account_id?}."""
    t = data.get("target") or {}
    kind = t.get("type")
    if kind in ("trash", "spam"):
        target = {"type": kind}
    elif kind == "ufolder":
        f = _folder_or_404(int(t.get("id") or 0))
        if not f["account_id"] or store.get(f["account_id"]) is None:
            raise ValidationError(tr("err.folderNoAccountNamed", name=f["name"]))
        target = {"type": "ufolder", "folder": f}
    elif kind == "inbox":
        view = t.get("view") or "all"
        if view not in INBOX_VIEWS:
            abort(400, tr("err.unknownView"))
        if t.get("account_id"):
            account_or_404(t["account_id"])
        target = {"type": "inbox", "view": view, "account_id": t.get("account_id") or None}
    else:
        abort(400, tr("err.unknownTarget"))
    raw = data.get("items") or []
    if not raw or len(raw) > MAX_BULK:
        abort(400, tr("err.nothingToMove") if not raw else tr("err.tooMany", count=MAX_BULK))
    trash, junk = database.trash_folders(), database.spam_folders()
    items, seen_keys = [], set()
    for it in raw:
        acc_id, folder, uid = it.get("account_id"), it.get("folder"), it.get("uid")
        if not acc_id or not folder or not isinstance(uid, int) or (acc_id, folder, uid) in seen_keys:
            continue
        if store.get(acc_id) is None:
            continue
        seen_keys.add((acc_id, folder, uid))
        row = database.message(acc_id, folder, uid)
        if row:
            sender = row["from"][0]["addr"].lower() if row["from"] else ""
            info = {"sender": sender, "date": row["date"], "seen": row["seen"], "snoozed": bool(row["snoozed_until"]),
                    "rec": row}
        else:  # např. výsledek hledání na serveru – údaje od klienta
            info = {"sender": str(it.get("sender") or "").lower(), "date": it.get("date"),
                    "seen": bool(it.get("seen")), "snoozed": False, "rec": None}
        if target["type"] == "trash":
            dst = (acc_id, trash.get(acc_id))
        elif target["type"] == "spam":
            dst = (acc_id, junk.get(acc_id, "Spam"))  # chybějící složku Spam vytvoří server
        elif target["type"] == "ufolder":
            dst = (target["folder"]["account_id"], target["folder"]["imap_name"])
        else:
            dst = (target["account_id"] or acc_id, "INBOX")
        items.append({"account_id": acc_id, "folder": folder, "uid": uid, "dst": dst,
                      "stays": dst == (acc_id, folder), "from_spam": junk.get(acc_id) == folder, **info})
    if not items:
        abort(400, tr("err.nothingToMove"))
    return items, target


def _move_warnings(items, target):
    """Co by se se zprávami stalo hned po přesunu (smazání, spam, přesun pravidlem) – pro varování v UI."""
    warnings = []
    if target["type"] == "trash":
        final = [i for i in items if i["dst"][1] is None or i["stays"]]
        if final:
            warnings.append(tr("warn.alreadyInTrash", count=len(final)))
        return warnings
    settings = database.settings()
    if target["type"] == "spam":
        age = db.delete_age(settings, "spam")
        old = [i for i in items if age and i["date"] and i["date"] < time.time() - age]
        if old:
            key = "warn.spamOldWeek" if age == db.WEEK else "warn.spamOldMonth"
            warnings.append(tr(key, count=len(old)))
        return warnings
    if target["type"] != "inbox":
        return warnings
    view = target["view"]
    senders = {i["sender"] for i in items if i["sender"]}
    new_senders = senders - database.category_senders(view) if view in db.CATEGORIES else set()
    category_of = database.categorizer()

    def category_after(i):
        """Kategorie zprávy po přesunu (přesun do pohledu Notifikace / Nedůležité / Důležité ji mění)."""
        if view in db.CATEGORIES:
            return view
        if view in ("important", "fyi"):
            return None
        return category_of(i["rec"]) if i["rec"] else None
    arriving = [i for i in items if not i["stays"]]
    rules = sorted(database.folder_rules_active(), key=lambda r: db.RULE_KINDS.index(r["kind"]))
    by_folder, moved_away = {}, set()
    for i in arriving:
        if i["rec"] is None or (i["account_id"], i["folder"], i["uid"]) in moved_away:
            continue
        rule = next((r for r in rules if (not r["after_read"] or i["seen"]) and db.rule_matches(r, i["rec"])), None)
        if rule:
            by_folder[rule["name"]] = by_folder.get(rule["name"], 0) + 1
            moved_away.add((i["account_id"], i["folder"], i["uid"]))
    for name, n in by_folder.items():
        warnings.append(tr("warn.folderRule", count=n, folder=name))
    now = time.time()

    def doomed(cat, seen, date, snoozed):
        """Smazal by ji hned úklid? Vrací stáří, od kterého se maže (týden / měsíc), jinak None."""
        if cat not in db.CATEGORIES or snoozed or not date:
            return None
        age = db.delete_age(settings, CATEGORY_SETTING[cat], seen)
        return age if age and date < now - age else None
    dragged = {}
    for i in items:
        cat = category_after(i)
        age = doomed(cat, i["seen"], i["date"], i["snoozed"])
        if (i["account_id"], i["folder"], i["uid"]) not in moved_away and age:
            dragged[(cat, age)] = dragged.get((cat, age), 0) + 1
    for (cat, age), n in dragged.items():
        period = "week" if age == db.WEEK else "month"
        warnings.append(tr(f"warn.oldAs.{cat}.{period}", count=n))
    if new_senders:
        keys = {(i["account_id"], i["folder"], i["uid"]) for i in items}
        others = {(r["account_id"], r["uid"]) for r in database.inbox_from_senders(new_senders)
                  if (r["account_id"], "INBOX", r["uid"]) not in keys
                  and doomed(view, r["seen"], r["date"], r["snoozed"])}
        if others:
            warnings.append(tr("warn.sameSenders", count=len(others)))
    return warnings


@app.post("/api/messages/move/preview")
def messages_move_preview():
    items, target = _parse_move(request.get_json(force=True))
    return jsonify(warnings=_move_warnings(items, target), count=len(items))


@app.post("/api/messages/move")
def messages_move():
    """Přesun / smazání více zpráv. Databáze (a tím UI) hned, server na pozadí."""
    items, target = _parse_move(request.get_json(force=True))
    senders = {i["sender"] for i in items if i["sender"]}
    own = {a["email"].lower() for a in store.list_public()}
    if target["type"] == "inbox" and target["view"] in db.CATEGORIES:
        for s in senders - own:
            database.add_category_rule(target["view"], sender=s)
        for i in items:  # jiné označení zprávy (druhá kategorie, důležitá) by mělo před pravidlem přednost
            if i["rec"]:
                _unmark_others(i["rec"], target["view"])
    elif target["type"] == "inbox" and target["view"] in ("important", "fyi"):
        for i in items:
            for cat in db.CATEGORIES:
                if i["rec"]:
                    database.uncategorize(i["rec"], cat)
                elif i["sender"]:
                    database.remove_category_sender(cat, i["sender"])
            if i["rec"] and target["view"] == "important":  # i kopie (jinak by zůstala ve Vzít na vědomí)
                database.mark_message(i["rec"], "important")
            elif i["rec"]:
                database.unmark_message(i["rec"], "important")
    if target["type"] == "spam":
        for s in senders - own:
            database.add_spam_rule(s)
    elif target["type"] == "inbox":  # „Není spam“ / přetažení do doručené pošty – jinak by se zprávy hned vrátily
        database.remove_spam_senders(senders)
    else:
        database.remove_spam_senders({i["sender"] for i in items if i["from_spam"]})
    groups = {}
    for i in items:
        if not i["stays"] or target["type"] == "trash":  # ze Smazaných se maže natrvalo
            groups.setdefault((i["account_id"], i["folder"]), []).append(i["uid"])
    for (acc_id, folder), uids in groups.items():
        acc = store.get(acc_id)
        if target["type"] == "trash":
            syncer.enqueue(acc, folder, uids, tr("op.deleteMany"),
                           lambda a=acc, f=folder, u=uids: syncer.trash_messages(a, f, u), remove=True, use_conn=False)
        elif target["type"] == "spam":
            syncer.enqueue(acc, folder, uids, tr("op.spam"),
                           lambda a=acc, f=folder, u=uids: syncer.spam_messages(a, f, u), remove=True, use_conn=False)
        else:
            if target["type"] == "ufolder":
                f = target["folder"]
                dest = {"account_id": f["account_id"], "imap_name": f["imap_name"], "mark_read": f["mark_read"],
                        "name": f["name"]}
            else:
                dest = {"account_id": target["account_id"] or acc_id, "imap_name": "INBOX", "mark_read": False,
                        "name": tr("folder.inbox")}
            ham = database.spam_folders().get(acc_id) == folder  # ze spamu ven = „Není spam“ (učení)
            syncer.enqueue(acc, folder, uids, tr("op.moveToFolder", folder=dest["name"]),
                           lambda a=acc, f=folder, u=uids, d=dest, h=ham: syncer.move_messages(a, f, u, d, learn_ham=h),
                           remove=True, use_conn=False)
        database.delete_uids(acc_id, folder, uids)
    database.bump()
    syncer.trigger()
    return jsonify(ok=True, moved=sum(len(u) for u in groups.values()))


@app.post("/api/mark-all-read")
def mark_all_read():
    """Označit vše jako přečtené (kontextové menu levého panelu): pohled doručené pošty, složka aplikace
    nebo doručená pošta účtu. Databáze hned, server na pozadí."""
    t = request.get_json(force=True) or {}
    kind = t.get("type")
    if kind == "unified":
        if t.get("view") not in db.VIEW_FILTERS:
            abort(400, tr("err.unknownView"))
        groups = database.unseen_in("INBOX", t["view"])
        folder = "INBOX"
    elif kind == "account":
        account_or_404(t.get("account_id"))
        groups, folder = database.unseen_in("INBOX", acc_id=t["account_id"]), "INBOX"
    elif kind == "ufolder":
        f = _folder_or_404(int(t.get("id") or 0))
        if not f["account_id"] or store.get(f["account_id"]) is None:
            raise ValidationError(tr("err.folderNoAccount"))
        groups, folder = database.unseen_in(f["imap_name"], acc_id=f["account_id"]), f["imap_name"]
    elif kind == "spam":
        groups, folder = {}, None
    else:
        abort(400, tr("err.unknownTarget"))
    # [(účet, složka, uid)] – Spam má na každém účtu jinou složku
    work = [(acc_id, folder, uids) for acc_id, uids in groups.items()]
    if kind == "spam":
        work = [(acc_id, name, database.unseen_uids(acc_id, name)) for acc_id, name in database.spam_folders().items()]
    total = 0
    for acc_id, folder, uids in work:
        acc = store.get(acc_id)
        if acc is None or not uids:
            continue
        database.set_seen_bulk(acc_id, folder, uids)
        syncer.enqueue(acc, folder, uids, tr("op.markReadMany"),
                       lambda M, f=folder, u=uids: mail.mark_seen_uids(M, f, u))
        total += len(uids)
    if total:
        database.bump()
    return jsonify(ok=True, count=total)


@app.get("/api/trash")
def trash_list():
    """Složka Smazané: koše všech účtů dohromady (z databáze)."""
    return _role_list(database.trash_folders())


@app.get("/api/sent")
def sent_list():
    """Složka Odeslané: odeslaná pošta všech účtů dohromady (z databáze)."""
    return _role_list(database.sent_folders())


@app.get("/api/spam")
def spam_list():
    """Složka Spam: spam všech účtů dohromady (z databáze)."""
    return _role_list(database.spam_folders())


def _role_list(by_acc):
    locations = {a["id"]: by_acc[a["id"]] for a in store.all() if a["id"] in by_acc}
    page = max(0, request.args.get("page", 0, type=int))
    offset, limit = list_window(UNIFIED_PAGE_SIZE)
    version = database.version
    res = database.list_messages([], locations, page, limit, offset=offset, unread=unread_arg(),
                                 search=query_arg())
    return jsonify(messages=res["messages"], has_more=res["has_more"], total=res["total"], errors=[], version=version)


@app.post("/api/trash/empty")
def trash_empty():
    """Vyprázdnit: natrvalo smaže všechny zprávy v koších všech účtů."""
    trash = database.trash_folders()
    accounts = [a for a in store.all() if a["id"] in trash]
    deleted, errors = 0, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for acc, fut in [(a, ex.submit(syncer.empty_trash, a, trash[a["id"]])) for a in accounts]:
            try:
                deleted += fut.result()
            except Exception as e:
                errors.append(f"{acc['name']}: {mail.friendly_error(e)}")
    syncer.trigger()
    if errors:
        raise mail.MailError(tr("err.emptyTrashFailed", errors="; ".join(errors)))
    return jsonify(ok=True, deleted=deleted)


@app.get("/api/settings")
def settings_get():
    return jsonify(database.settings())


@app.put("/api/settings")
def settings_put():
    changes = request.get_json(force=True) or {}
    if "language" in changes and changes["language"] not in {"", *(l["code"] for l in i18n.languages())}:
        changes.pop("language")
    result = database.update_settings(changes)
    i18n.set_language(result["language"])
    syncer.trigger()  # případný úklid podle nového nastavení hned
    return jsonify(result)


@app.get("/api/i18n")
def i18n_get():
    """Katalog textů pro rozhraní a seznam jazyků. ?reload=1 znovu projde složky s jazyky (Nastavení)."""
    if request.args.get("reload"):
        i18n.reload()
        i18n.set_language(database.settings()["language"])
    # jazyk zatím nezvolený a ve složce s jazyky leží překlad → rozhraní hned na začátku nabídne výběr
    choose = not database.settings()["language"] and i18n.has_user_files()
    return jsonify(catalog=i18n.catalog(), languages=i18n.languages(), problems=i18n.problems(),
                   choose=choose, folder=str(i18n.user_dir()))


def _open_folder(folder):
    """Otevře složku v Průzkumníku (vytvoří ji). Jen desktopová aplikace."""
    if not EMBEDDED:
        return jsonify(error=i18n.t("error.desktopOnly")), 400
    folder.mkdir(parents=True, exist_ok=True)
    os.startfile(folder)
    return jsonify(folder=str(folder))


@app.post("/api/app/data-folder")
def app_open_data_folder():
    return _open_folder(DATA_DIR)


@app.post("/api/i18n/folder")
def i18n_open_folder():
    return _open_folder(i18n.user_dir())


@app.get("/api/bayes")
def bayes_status():
    return jsonify(syncer.bayes.status())


@app.post("/api/bayes/reset")
def bayes_reset():
    """Zapomenout naučené a naučit se znovu z existujících zpráv (spam z pravidel, přečtená doručená pošta)."""
    syncer.bayes.reset()
    syncer.start_bayes_bootstrap()
    return jsonify(syncer.bayes.status())


def _category_or_404(category):
    if category not in db.CATEGORIES:
        abort(404, tr("err.unknownCategory"))
    return category


@app.post("/api/categories/<category>/mark-read")
def category_mark_read(category):
    """Po opuštění Notifikací / Nedůležitých (podle nastavení): vše označit jako přečtené."""
    return jsonify(ok=True, count=syncer.mark_category_read(_category_or_404(category)))


@app.get("/api/categories/<category>/rules")
def category_rules(category):
    return jsonify(database.category_rules(_category_or_404(category)))


def _category_rule_fields(data) -> dict:
    """Ověřené podmínky pravidla Nedůležitých / Notifikací: {sender, recipient, subject}."""
    sender, recipient = (str(data.get(k) or "").strip().lower() for k in ("sender", "recipient"))
    subject = mail.base_subject(str(data.get("subject") or "")).lower()
    for key, addr in (("err.invalidSender", sender), ("err.invalidRecipient", recipient)):
        if addr and not mail.ADDR_RE.match(addr):
            raise ValidationError(tr(key))
    if len(subject) > 200:
        raise ValidationError(tr("err.subjectTooLong"))
    if not (sender or recipient or subject):
        raise ValidationError(tr("err.fillOne"))
    return {"sender": sender, "recipient": recipient, "subject": subject}


@app.put("/api/categories/<category>/rules/<int:rule_id>")
def category_rule_update(category, rule_id):
    """Úprava pravidla Nedůležitých / Notifikací: odesílatel, příjemce, část předmětu (vyplněné musí platit všechny)."""
    _category_or_404(category)
    rule = _category_rule_fields(request.get_json(force=True))
    if not database.update_category_rule(category, rule_id, **rule):
        raise ValidationError(tr("err.ruleExists"))
    database.bump()
    syncer.trigger()
    return jsonify(ok=True)


@app.delete("/api/categories/<category>/rules/<int:rule_id>")
def category_rule_delete(category, rule_id):
    database.delete_category_rule(_category_or_404(category), rule_id)
    database.bump()
    return jsonify(ok=True)


@app.get("/api/spam-rules")
def spam_rules():
    return jsonify(database.spam_rules())


@app.put("/api/spam-rules/<int:rule_id>")
def spam_rule_update(rule_id):
    value = str(request.get_json(force=True).get("value") or "").strip().lower()
    if not mail.ADDR_RE.match(value):
        raise ValidationError(tr("err.invalidEmail"))
    return _update_rule("spam_rules", rule_id, "sender", value)


@app.delete("/api/spam-rules/<int:rule_id>")
def spam_rule_delete(rule_id):
    database.delete_spam_rule(rule_id)
    return jsonify(ok=True)


@app.get("/api/sync/status")
def sync_status():
    """Levný dotaz pro UI: změnila se data? (verze), nepřečtené v doručené poště, chyby synchronizace."""
    return jsonify(version=database.version, unread=database.unread_counts(), views=database.view_unread(),
                   folders=database.folder_unread(), trash=database.trash_total(a["id"] for a in store.all()),
                   spam=database.role_count("junk", [a["id"] for a in store.all()], unread=True),
                   errors=syncer.errors, notices=syncer.notices, update=_update_brief())


def _update_brief():
    st = updater.status()
    return {"status": st["status"], "version": st["version"]}


@app.post("/api/sync")
def sync_now():
    syncer.trigger()
    return jsonify(ok=True)


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/spam")
def message_spam(acc_id, uid):
    """Přesun do spamu: z UI hned, na serveru na pozadí; pak hromadný přesun starších zpráv odesílatele."""
    acc = account_or_404(acc_id)
    folder = folder_arg(request.get_json(force=True))
    row = database.message(acc_id, folder, uid)
    if row is None:  # zpráva není v databázi (např. výsledek hledání) – odesílatele zjistíme na serveru
        with pool.get(acc) as M:
            mail._select(M, folder, readonly=True)
            recs = mail.fetch_header_records(M, [uid])
        if not recs:
            raise mail.MailError(tr("err.messageNotFoundDeleted"))
        row = recs[0]
    sender = row["from"][0]["addr"].lower() if row["from"] else None
    ts = row["date"]
    own = {a["email"].lower() for a in store.list_public()}
    job_id = None
    if sender and sender not in own:  # z vlastní adresy pravidlo ani hromadný přesun neděláme
        database.add_spam_rule(sender)
        database.add_rule_hits(sender)
        job_id = uuid.uuid4().hex[:12]
        with jobs_lock:
            jobs[job_id] = {"status": "running", "moved": 0, "errors": [], "sender": sender}

    def op(M):
        syncer.bayes_learn(M, acc, folder, [uid], True)
        mail.spam_message(M, folder, uid)
        if job_id:
            threading.Thread(target=_spam_sender_job, args=(job_id, sender, ts), daemon=True).start()

    def failed(msg):
        if job_id:
            with jobs_lock:
                jobs[job_id].update(status="done", errors=[msg])

    syncer.enqueue(acc, folder, uid, tr("op.spam"), op, remove=True, on_error=failed)
    database.remove_message(acc_id, folder, uid)
    database.bump()
    return jsonify(ok=True, job_id=job_id, sender=sender)


def _spam_sender_job(job_id, sender, ts):
    moved, errors = 0, []
    for acc in store.all():
        try:
            with bg_pool.get(acc) as M:
                moved += mail.move_sender_to_spam(M, sender, ts)
        except Exception as e:
            errors.append(f"{acc['name']}: {mail.friendly_error(e)}")
        with jobs_lock:
            jobs[job_id].update(moved=moved, errors=errors)
    with jobs_lock:
        jobs[job_id]["status"] = "done"
    if moved:
        database.add_rule_hits(sender, moved)
    syncer.trigger()


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            abort(404, tr("err.jobMissing"))
        result = dict(job)
        if job["status"] == "done":
            del jobs[job_id]
    return jsonify(result)


@app.get("/api/accounts/<acc_id>/messages/<int:uid>/locate")
def message_locate(acc_id, uid):
    """Kde zprávu najít v levém sloupci: pohled a pořadí zprávy v jeho seznamu.
    Přednost má složka aplikace, pak Důležité / Vzít na vědomí / Notifikace (Všechny doručené),
    pak Spam a Smazané; zprávy z ostatních složek (Odeslané, Archiv…) se ukážou v dané složce účtu."""
    account_or_404(acc_id)
    folder = folder_arg()
    if database.message(acc_id, folder, uid) is None:
        return jsonify(view=None)
    accounts = [a["id"] for a in store.all()]
    pos = lambda where, view="all": database.list_position(accounts, where, view, acc_id, folder, uid)
    for f in database.user_folders():
        if f["account_id"] == acc_id and f["imap_name"] == folder:
            return jsonify(view={"type": "ufolder", "id": f["id"], "title": f["name"]},
                           position=database.list_position([acc_id], folder, "all", acc_id, folder, uid))
    if folder == "INBOX":
        view = database.view_of(acc_id, folder, uid)
        return jsonify(view={"type": "unified", "filter": view}, position=pos("INBOX", view))
    for kind, by_acc in (("spam", database.spam_folders()), ("trash", database.trash_folders()),
                         ("sent", database.sent_folders())):
        if by_acc.get(acc_id) == folder:
            locations = {a: by_acc[a] for a in accounts if a in by_acc}
            return jsonify(view={"type": kind}, position=pos(locations))
    title = (database.folder(acc_id, folder) or {}).get("display") or mail.decode_utf7(folder)
    return jsonify(view={"type": "folder", "accountId": acc_id, "folder": folder, "title": title},
                   position=database.list_position([acc_id], folder, "all", acc_id, folder, uid))


# --------------------------------------------------------------------------
# Hledání: nejdřív okamžitě v uložených zprávách, pak dohledání na serverech (text zpráv, nestažené zprávy)
# --------------------------------------------------------------------------

REMOTE_PER_FOLDER = 200     # nejvýš tolik nejnovějších výsledků z jedné složky
REMOTE_FETCH = 50           # z toho nejvýš tolik nestažených zpráv (hlavičky ze serveru)
search_seq = {"n": 0}       # novější hledání ukončí starší, aby se nehromadila


@app.get("/api/search")
def search_all():
    """Hledání ve veškeré poště – z lokální databáze (okamžité). Pokročilé hledání:
    ?from= &to= &subject= (část textu) a ?att=1 / 0 (má / nemá přílohu)."""
    q = query_arg()
    adv = {k: " ".join((request.args.get(a) or "").split()) or None
           for k, a in (("sender", "from"), ("recipient", "to"), ("subject", "subject"))}
    att = request.args.get("att")
    adv["attachments"] = {"1": True, "0": False}.get(att)
    if not q and not any(adv[k] for k in ("sender", "recipient", "subject")):
        return jsonify(messages=[], has_more=False, version=database.version)
    version = database.version
    found, more = database.search_all(q, [a["id"] for a in store.all()], **adv)
    return jsonify(messages=found, has_more=more, version=version)


def _search_locations():
    """Kde hledat na serveru: ({účet: [složky]}, pohled doručené pošty nebo None) podle ?scope=."""
    scope = request.args.get("scope") or "all"
    accounts = store.all()
    if scope == "all":  # veškerá pošta: synchronizované složky + koš
        locs = {}
        for a in accounts:
            folders = database.folders(a["id"])
            names = [f["name"] for f in sync.context_folders(folders, database.user_folder_names(a["id"]))]
            names += [f["name"] for f in folders if f["role"] == "trash" and f["name"] not in names]
            locs[a["id"]] = names
        return locs, None
    if scope == "unified":
        view = request.args.get("filter") or "all"
        if view not in db.VIEW_FILTERS:
            abort(400, tr("err.unknownView"))
        return {a["id"]: ["INBOX"] for a in accounts}, view
    if scope == "ufolder":
        f = _folder_or_404(request.args.get("id", type=int) or 0)
        return ({f["account_id"]: [f["imap_name"]]} if f["account_id"] and store.get(f["account_id"]) else {}), None
    if scope in ("spam", "trash", "sent"):
        by_acc = {"spam": database.spam_folders, "trash": database.trash_folders,
                  "sent": database.sent_folders}[scope]()
        return {a["id"]: [by_acc[a["id"]]] for a in accounts if a["id"] in by_acc}, None
    if scope == "folder":
        acc = account_or_404(request.args.get("account"))
        return {acc["id"]: [folder_arg()]}, None
    abort(400, tr("err.unknownScope"))


def _local_match(m, term):
    """Našel by zprávu už lokální filtr? (pak ji dohledání na serveru nevrací znovu)"""
    t = term.lower()
    people = m["from"] + m["to"] + m.get("cc", [])
    return t in (m["subject"] or "").lower() or any(t in f"{a.get('name') or ''} {a.get('addr') or ''}".lower()
                                                   for a in people)


@app.get("/api/search/remote")
def search_remote():
    """Dohledání na serverech: zprávy, které lokální filtr nenajde – shoda v textu zprávy nebo zprávy,
    které ještě nejsou stažené. Novější hledání starší ukončí (stale)."""
    q = query_arg()
    if not q or len(q) < 2:
        return jsonify(messages=[], errors=[])
    locations, view = _search_locations()
    with jobs_lock:
        search_seq["n"] += 1
        seq = search_seq["n"]
    is_stale = lambda: search_seq["n"] != seq
    if view not in (None, "all"):
        category_of, mine = database.categorizer(), database.my_addresses()

    def in_view(m):
        if view in (None, "all"):
            return True
        cat = category_of(m)
        if cat:
            return view == cat
        direct = any(a["addr"].lower() in mine.get(m["account_id"], ()) for a in m["to"])
        return view == ("important" if direct else "fyi")

    def work(acc_id, folders):
        acc, out = store.get(acc_id), []
        if acc is None:
            return out
        with bg_pool.get(acc) as M:
            for folder in folders:
                if is_stale():
                    break
                try:
                    mail._select(M, folder, readonly=True)
                    uids = sorted((int(u) for u in mail._search(M, q)), reverse=True)[:REMOTE_PER_FOLDER]
                except mail.MailError:
                    continue  # složka mezitím zmizela / server hledání v ní neumí
                known = database.messages_by_uids(acc_id, folder, uids)
                out += [m for m in known.values() if not _local_match(m, q)]
                unknown = [u for u in uids if u not in known][:REMOTE_FETCH]
                if unknown:
                    label = database.folder_label(acc_id, folder)
                    for r in mail.fetch_header_records(M, unknown):
                        if r.get("deleted"):
                            continue
                        m = {k: v for k, v in r.items() if k in mail.LIST_FIELDS or k in ("message_id", "base_subject")}
                        m.update(account_id=acc_id, folder=folder, folder_label=label, cc=r.get("cc", []),
                                 prio=0 if folder == "INBOX" else 1)
                        out.append(m)
        return out

    found, errors = [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [(acc_id, ex.submit(work, acc_id, folders)) for acc_id, folders in locations.items() if folders]
        for acc_id, fut in futures:
            try:
                found += fut.result()
            except Exception as e:
                errors.append(f"{(store.get(acc_id) or {}).get('name', acc_id)}: {mail.friendly_error(e)}")
    if is_stale():
        return jsonify(stale=True, messages=[], errors=[])
    found = [m for m in database.dedupe(found) if in_view(m)]
    return jsonify(messages=unread_only(found, unread_arg()), errors=errors)


@app.get("/api/accounts/<acc_id>/messages/<int:uid>/context")
def message_context(acc_id, uid):
    """Související zprávy. Běží na samostatném spojení; zastaralé požadavky (uživatel mezitím
    otevřel jinou zprávu) se ukončí, aby se nehromadily."""
    acc = account_or_404(acc_id)
    folder = folder_arg()
    row = database.message(acc_id, folder, uid)
    if row and syncer.context_ready(acc_id):
        me = acc["email"].lower()
        counterpart = row["from"][0]["addr"] if row["from"] else ""
        if counterpart.lower() == me:  # moje odeslaná zpráva – protistranou je adresát
            counterpart = next((a["addr"] for a in row["to"] if a["addr"].lower() != me), "")
        related = database.related(acc_id, sync.context_folders(database.folders(acc_id), database.user_folder_names(acc_id)),
                                   me, counterpart,
                                   row["base_subject"], row["message_id"])
        return jsonify(messages=related, counterpart=counterpart, cached=True)

    with jobs_lock:
        seq = context_seq[acc_id] = context_seq.get(acc_id, 0) + 1
    is_stale = lambda: context_seq.get(acc_id) != seq
    with bg_pool.get(acc) as M:
        if is_stale():
            return jsonify(stale=True, messages=[])
        mail._select(M, folder, readonly=True)
        headers = mail._fetch_headers(M, [uid])
        if not headers:
            raise mail.MailError(tr("err.messageNotFound"))
        _, _, h = headers[0]
        me = acc["email"].lower()
        frm, to = mail.parse_addrs(str(h.get("From") or "")), mail.parse_addrs(str(h.get("To") or ""))
        counterpart = frm[0]["addr"] if frm else ""
        if counterpart.lower() == me:  # moje odeslaná zpráva – protistranou je adresát
            counterpart = next((a["addr"] for a in to if a["addr"].lower() != me), "")
        related = mail.find_related(M, me, counterpart, mail.get_header(h, "Subject"),
                                    mail.get_header(h, "Message-ID").strip(), is_stale)
    if related is None:
        return jsonify(stale=True, messages=[])
    for m in related:
        m["account_id"] = acc_id
    return jsonify(messages=related, counterpart=counterpart)


# --------------------------------------------------------------------------
# Odeslání
# --------------------------------------------------------------------------

@app.post("/api/send")
def send():
    data = request.get_json(force=True)
    acc = account_or_404(data.get("account_id"))
    to, cc, bcc = (str(data.get(k) or "").strip() for k in ("to", "cc", "bcc"))
    recipients = mail.parse_recipients(to=to, cc=cc, bcc=bcc)  # validace ještě před načítáním příloh
    to, cc = (v.strip().strip(",;").replace(";", ",") for v in (to, cc))

    attachments = []
    for a in data.get("attachments") or []:
        try:
            content = base64.b64decode(a["data"], validate=True)
        except Exception:
            raise mail.MailError(tr("err.badAttachment", name=a.get("filename")))
        attachments.append((a.get("filename") or "priloha", a.get("content_type") or "application/octet-stream", content))

    fwd = data.get("forward")  # přílohy z přeposílané zprávy
    if fwd and fwd.get("indexes"):
        src = account_or_404(fwd.get("account_id"))
        with pool.get(src) as M:
            attachments += mail.get_attachments(M, fwd["folder"], int(fwd["uid"]), [int(i) for i in fwd["indexes"]])

    msg = mail.build_message(acc, to, cc, str(data.get("subject") or ""), str(data.get("body") or ""),
                             data.get("in_reply_to") or None, data.get("references") or None, attachments)
    job = lambda: _deliver(acc, msg, recipients, (to, cc, bcc))
    delay = min(max(float(data.get("delay") or 0), 0), 30)
    if not delay:
        return jsonify(ok=True, warning=job())
    return jsonify(ok=True, pending_id=_schedule_send(job, delay))


def _deliver(acc, msg, recipients, fields):
    """Odeslání připravené zprávy (SMTP + kopie v Odeslaných). Vrací varování, nebo None."""
    warning = mail.send_message(acc, msg, recipients, pool)
    try:
        database.add_sent_contacts([(a["name"], a["addr"]) for v in fields for a in mail.parse_addrs(v)])
    except Exception:
        pass  # zpráva odešla; adresáti se do kontaktů dostanou i synchronizací Odeslaných
    syncer.trigger()  # kopie v Odeslaných se hned objeví i ve složce Odeslané
    return warning


# --- odložené odeslání: zpráva je připravená a ověřená a čeká. Klient po odpočtu (a zavření lišty s tlačítkem
# Zrušit odeslání) pošle commit – teprve pak zpráva odejde. Časovač je jen pojistka pro případ, že se klient
# už neozve (obnovená stránka); při ukončení aplikace čekající zprávy odejdou hned (flush_pending_sends).

SEND_FALLBACK = 60          # s navíc k prodlevě, po kterých zpráva odejde i bez commitu

pending_sends = {}           # id -> {"state": waiting|sending|done, "job", "timer", "done": Event, "result"}
pending_lock = threading.Lock()


def _schedule_send(job, delay):
    sid = uuid.uuid4().hex
    entry = {"state": "waiting", "job": job, "done": threading.Event(), "result": None}
    entry["timer"] = threading.Timer(delay + SEND_FALLBACK, _run_pending, (sid,))
    entry["timer"].daemon = True
    with pending_lock:
        pending_sends[sid] = entry
    entry["timer"].start()
    return sid


def _run_pending(sid):
    with pending_lock:
        entry = pending_sends.get(sid)
        if not entry or entry["state"] != "waiting":
            return
        entry["state"] = "sending"
    try:
        entry["result"] = {"ok": True, "warning": entry["job"]()}
    except Exception as e:
        entry["result"] = {"ok": False, "error": str(e) if isinstance(e, mail.MailError) else mail.friendly_error(e)}
    entry["state"] = "done"
    entry["done"].set()


def flush_pending_sends():
    """Při ukončení aplikace: zprávy, které ještě čekají na odeslání, odeslat hned (jinak by se ztratily)."""
    with pending_lock:
        waiting = [sid for sid, e in pending_sends.items() if e["state"] == "waiting"]
    for sid in waiting:
        pending_sends[sid]["timer"].cancel()
        _run_pending(sid)
    for e in list(pending_sends.values()):  # i ty, které se právě odesílají
        e["done"].wait(120)


@app.post("/api/send/<sid>/cancel")
def send_cancel(sid):
    """Zrušení odloženého odeslání – jen dokud zpráva ještě neodchází."""
    with pending_lock:
        entry = pending_sends.get(sid)
        if not entry:
            abort(404, tr("err.sendCannotStop"))
        if entry["state"] != "waiting":
            raise ValidationError(tr("err.sendInProgress"))
        entry["timer"].cancel()
        del pending_sends[sid]
    return jsonify(ok=True)


@app.post("/api/send/<sid>/commit")
def send_commit(sid):
    """Odpočet skončil: odeslat čekající zprávu hned a vrátit výsledek."""
    with pending_lock:
        entry = pending_sends.get(sid)
    if not entry:
        abort(404, tr("err.sendMissing"))
    entry["timer"].cancel()
    _run_pending(sid)        # nic neudělá, pokud už zprávu odeslala pojistka – pak jen počkáme na výsledek
    entry["done"].wait(120)
    with pending_lock:
        pending_sends.pop(sid, None)
    result = entry["result"] or {"ok": False, "error": tr("err.sendTimeout")}
    if not result["ok"]:
        raise mail.MailError(result["error"])
    return jsonify(ok=True, warning=result["warning"])


@app.get("/api/contacts")
def contacts():
    """Adresy pro našeptávání v polích Komu / Kopie / Skrytá (filtruje se v prohlížeči)."""
    return jsonify(contacts=database.contacts())

