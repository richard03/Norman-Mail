"""Mailer – jednoduchý emailový klient pro více účtů. Spuštění: python app.py"""

import base64
import io
import os
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from html import escape
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import db
import google_oauth
import mail
import sync
from store import KEYRING_OK, AccountStore, ValidationError, normalize

HOST = "127.0.0.1"
PORT = int(os.environ.get("MAILER_PORT", "8765"))
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
        abort(404, "Účet neexistuje.")
    return acc


def folder_arg(data=None):
    folder = (data or request.args).get("folder")
    if not folder:
        abort(400, "Chybí složka.")
    return folder


# --------------------------------------------------------------------------
# Stránky a účty
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/accounts")
def accounts_list():
    return jsonify(store.list_public())


@app.get("/api/info")
def info():
    return jsonify(keyring=KEYRING_OK)


def _save_account(acc_id):
    data = request.get_json(force=True)
    existing = store.get(acc_id) if acc_id else None
    # Typ přihlášení se z formuláře měnit nedá – OAuth účty vznikají přes /oauth/callback.
    data["auth"] = (existing or {}).get("auth") or "password"
    acc = normalize(data)
    password = "" if acc["auth"] == "oauth2" else (data.get("password") or "")
    if existing and not password:
        password = existing["password"]
    if not password:
        raise ValidationError("Zadejte heslo.")
    mail.test_account({**acc, "password": password, "id": acc_id or "test"})
    new_id = store.save(acc_id, acc, password)
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

def redirect_uri():
    return f"http://{HOST}:{PORT}/oauth/callback"


@app.get("/api/google/config")
def google_config():
    cfg = google_oauth.load_config()
    return jsonify(configured=bool(cfg), client_id=(cfg or {}).get("client_id", ""), redirect_uri=redirect_uri())


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
        return jsonify(status="error", error="Přihlášení vypršelo, zkuste to znovu.")
    return jsonify(status=entry["status"], error=entry["error"], account_id=entry["account_id"])


CALLBACK_PAGE = """<!doctype html><html lang="cs"><head><meta charset="utf-8"><title>Mailer – přihlášení</title>
<style>body{{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;margin:0;
background:#f6f7f9;color:#1c2430}}div{{background:#fff;padding:28px 36px;border-radius:12px;max-width:480px;
box-shadow:0 4px 20px rgba(0,0,0,.08)}}h1{{font-size:20px;color:{color}}}</style></head>
<body><div><h1>{title}</h1><p>{text}</p></div></body></html>"""


def callback_page(ok, text):
    title, color = ("Přihlášení dokončeno", "#059669") if ok else ("Přihlášení se nezdařilo", "#c62828")
    html = CALLBACK_PAGE.format(title=title, color=color, text=escape(text))
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/oauth/callback")
def oauth_callback():
    state = request.args.get("state")
    entry = google_oauth.get_pending(state)
    if not entry or entry["status"] != "pending":
        return callback_page(False, "Neplatný nebo vypršelý požadavek. Zkuste se přihlásit znovu z aplikace.")
    if request.args.get("error") or not request.args.get("code"):
        google_oauth.finish(state, error="Přihlášení bylo zrušeno.")
        return callback_page(False, "Přihlášení bylo zrušeno. Toto okno můžete zavřít.")
    try:
        tok = google_oauth.exchange(request.args["code"], entry)
        email_addr = tok["email"]
        if entry["target_id"]:
            existing = store.get(entry["target_id"])
            if existing and existing["email"].lower() != email_addr.lower():
                raise google_oauth.OAuthError(
                    f"Přihlásili jste se jako {email_addr}, ale upravovaný účet je {existing['email']}.")
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
        syncer.trigger()
        google_oauth.finish(state, account_id=new_id)
        return callback_page(True, f"Účet {email_addr} je připojen. Toto okno můžete zavřít a vrátit se do Maileru.")
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


@app.get("/api/accounts/<acc_id>/messages")
def messages(acc_id):
    acc = account_or_404(acc_id)
    page = max(0, request.args.get("page", 0, type=int))
    folder, query = folder_arg(), request.args.get("q") or None
    if query:  # hledání v textu zpráv umí jen server
        with pool.get(acc) as M:
            res = mail.list_messages(M, folder, page, PAGE_SIZE, query)
        for m in res["messages"]:
            m["account_id"] = acc_id
        return jsonify(res)
    syncer.ensure_synced(acc, folder)
    offset, limit = list_window(PAGE_SIZE)
    version = database.version  # verze dat, ze které seznam vzniká (UI podle ní pozná, že je zastaralý)
    return jsonify(**database.list_messages([acc_id], folder, page, limit, offset=offset), version=version)


@app.get("/api/unified")
def unified():
    """Sloučená doručená pošta ze všech účtů – z lokální databáze."""
    page = max(0, request.args.get("page", 0, type=int))
    query = request.args.get("q") or None
    view = request.args.get("filter", "all")
    if view not in db.VIEW_FILTERS:
        abort(400, "Neznámý pohled.")
    accounts = store.all()
    if not accounts:
        return jsonify(messages=[], has_more=False, errors=[])
    errors = []

    if query:  # hledání v textu – paralelně na serverech
        def work(acc):
            with pool.get(acc) as M:
                return mail.list_messages(M, "INBOX", page, UNIFIED_PAGE_SIZE, query)
        result, has_more = [], False
        with ThreadPoolExecutor(max_workers=8) as ex:
            for acc, fut in [(a, ex.submit(work, a)) for a in accounts]:
                try:
                    res = fut.result()
                except Exception as e:
                    errors.append(f"{acc['name']}: {mail.friendly_error(e)}")
                    continue
                for m in res["messages"]:
                    m["account_id"] = acc["id"]
                result.extend(res["messages"])
                has_more = has_more or res["has_more"]
        if view != "all":  # výsledky hledání ze serveru roztřídíme podle pravidel
            senders = database.notify_senders()
            is_notif = lambda m: any(a["addr"].lower() in senders for a in m["from"])
            result = [m for m in result if is_notif(m) == (view == "notifications")]
        result.sort(key=lambda m: m["date"] or 0, reverse=True)
        return jsonify(messages=result, has_more=has_more, errors=errors)

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
            errors.append(f"{acc['name']}: {syncer.errors[acc['id']]} (zobrazuji uložená data)")
    offset, limit = list_window(UNIFIED_PAGE_SIZE)
    version = database.version
    res = database.list_messages([a["id"] for a in accounts], "INBOX", page, limit, view, offset=offset)
    return jsonify(messages=res["messages"], has_more=res["has_more"], errors=errors, version=version)


def _mark_seen_in_background(acc, folder, uid):
    syncer.enqueue(acc, folder, uid, "Označení zprávy jako přečtené",
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
                raw, flags = mail.fetch_raw_mark_seen(M, folder, uid)
        except mail.MessageGone as e:  # zastaralá cache – zprávu odebrat, ať zmizí i ze seznamu
            database.remove_message(acc_id, folder, uid)
            database.bump()
            return jsonify(error=str(e), gone=True), 404
        database.put_body(acc_id, folder, uid, raw)
    else:
        flags = ["\\Seen"] + (["\\Flagged"] if row and row["flagged"] else [])
        if row and not row["seen"]:
            _mark_seen_in_background(acc, folder, uid)
    database.set_flag(acc_id, folder, uid, "seen", True)
    data = mail.parse_message(raw, uid, folder, flags)
    data["snoozed_until"] = row["snoozed_until"] if row else None
    senders = database.notify_senders()
    data["notification"] = any(a["addr"].lower() in senders for a in data["from"])
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
    syncer.enqueue(acc, folder, uid, "Smazání zprávy", lambda M: mail.delete_message(M, folder, uid), remove=True)
    database.remove_message(acc_id, folder, uid)
    database.bump()
    return jsonify(ok=True)


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/archive")
def message_archive(acc_id, uid):
    acc = account_or_404(acc_id)
    folder = folder_arg(request.get_json(force=True))
    syncer.enqueue(acc, folder, uid, "Archivace zprávy", lambda M: mail.archive_message(M, folder, uid), remove=True)
    database.remove_message(acc_id, folder, uid)
    database.bump()
    return jsonify(ok=True)


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
        abort(400, "Chybí čas odložení.")
    if until <= time.time():
        abort(400, "Čas odložení musí být v budoucnosti.")
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
    syncer.enqueue(acc, folder, uid, "Odložení zprávy", lambda M: mail.set_flag(M, folder, uid, "\\Seen", True))
    return jsonify(ok=True, until=until)


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/unsnooze")
def message_unsnooze(acc_id, uid):
    account_or_404(acc_id)
    database.remove_snooze(acc_id, folder_arg(request.get_json(force=True)), uid)
    return jsonify(ok=True)


@app.post("/api/accounts/<acc_id>/messages/<int:uid>/notification")
def message_notification(acc_id, uid):
    """Označí (value=true) / odznačí odesílatele zprávy jako zdroj notifikací – platí pro všechny jeho zprávy."""
    acc = account_or_404(acc_id)
    data = request.get_json(force=True)
    folder = folder_arg(data)
    row = database.message(acc_id, folder, uid)
    if row:
        sender = row["from"][0]["addr"] if row["from"] else ""
    else:
        with pool.get(acc) as M:
            mail._select(M, folder, readonly=True)
            recs = mail.fetch_header_records(M, [uid])
        sender = recs[0]["from"][0]["addr"] if recs and recs[0]["from"] else ""
    if not sender:
        raise mail.MailError("Zpráva nemá adresu odesílatele.")
    value = bool(data.get("value", True))
    if value:
        database.add_notify_rule(sender)
    else:
        database.remove_notify_sender(sender)
    database.bump()
    return jsonify(ok=True, sender=sender.lower(), notification=value)


@app.get("/api/settings")
def settings_get():
    return jsonify(database.settings())


@app.put("/api/settings")
def settings_put():
    result = database.update_settings(request.get_json(force=True) or {})
    syncer.trigger()  # případný úklid podle nového nastavení hned
    return jsonify(result)


@app.post("/api/notifications/mark-read")
def notifications_mark_read():
    return jsonify(ok=True, count=syncer.mark_notifications_read())


@app.get("/api/notification-rules")
def notification_rules():
    return jsonify(database.notify_rules())


@app.delete("/api/notification-rules/<int:rule_id>")
def notification_rule_delete(rule_id):
    database.delete_notify_rule(rule_id)
    database.bump()
    return jsonify(ok=True)


@app.get("/api/spam-rules")
def spam_rules():
    return jsonify(database.spam_rules())


@app.delete("/api/spam-rules/<int:rule_id>")
def spam_rule_delete(rule_id):
    database.delete_spam_rule(rule_id)
    return jsonify(ok=True)


@app.get("/api/sync/status")
def sync_status():
    """Levný dotaz pro UI: změnila se data? (verze), nepřečtené v doručené poště, chyby synchronizace."""
    return jsonify(version=database.version, unread=database.unread_counts(), views=database.view_unread(),
                   errors=syncer.errors, notices=syncer.notices)


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
            raise mail.MailError("Zpráva nebyla nalezena (možná byla smazána).")
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
        mail.spam_message(M, folder, uid)
        if job_id:
            threading.Thread(target=_spam_sender_job, args=(job_id, sender, ts), daemon=True).start()

    def failed(msg):
        if job_id:
            with jobs_lock:
                jobs[job_id].update(status="done", errors=[msg])

    syncer.enqueue(acc, folder, uid, "Přesun do spamu", op, remove=True, on_error=failed)
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
            abort(404, "Úloha neexistuje.")
        result = dict(job)
        if job["status"] == "done":
            del jobs[job_id]
    return jsonify(result)


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
        related = database.related(acc_id, sync.context_folders(database.folders(acc_id)), me, counterpart,
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
            raise mail.MailError("Zpráva nebyla nalezena.")
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
            raise mail.MailError(f"Poškozená příloha: {a.get('filename')}")
        attachments.append((a.get("filename") or "priloha", a.get("content_type") or "application/octet-stream", content))

    fwd = data.get("forward")  # přílohy z přeposílané zprávy
    if fwd and fwd.get("indexes"):
        src = account_or_404(fwd.get("account_id"))
        with pool.get(src) as M:
            attachments += mail.get_attachments(M, fwd["folder"], int(fwd["uid"]), [int(i) for i in fwd["indexes"]])

    msg = mail.build_message(acc, to, cc, str(data.get("subject") or ""), str(data.get("body") or ""),
                             data.get("in_reply_to") or None, data.get("references") or None, attachments)
    warning = mail.send_message(acc, msg, recipients, pool)
    return jsonify(ok=True, warning=warning)


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}/"
    print(f"Mailer běží na {url}  (ukončení: Ctrl+C)")
    if not os.environ.get("MAILER_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    start_background()
    app.run(host=HOST, port=PORT, threaded=True)
