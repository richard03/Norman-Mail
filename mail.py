"""IMAP/SMTP logika emailového klienta (pouze standardní knihovna)."""

import base64
import email
import imaplib
import re
import smtplib
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import timezone
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parsedate_to_datetime
from html.parser import HTMLParser

import google_oauth

TIMEOUT = 30


class MailError(Exception):
    """Chyba s hláškou vhodnou pro uživatele."""


class MessageGone(MailError):
    """Zpráva na serveru už není (smazaná/přesunutá) – lokální cache je zastaralá."""


# --------------------------------------------------------------------------
# Pomocné funkce
# --------------------------------------------------------------------------

def q(name: str) -> str:
    """Uvozovky kolem názvu složky pro IMAP příkazy."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return re.sub(r'\\(.)', r'\1', s[1:-1])
    return s


def decode_utf7(s: str) -> str:
    """Dekóduje modifikované UTF-7 (RFC 3501) používané v názvech IMAP složek."""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "&":
            j = s.find("-", i)
            if j == -1:
                out.append(s[i:])
                break
            if j == i + 1:
                out.append("&")
            else:
                b64 = s[i + 1:j].replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except Exception:
                    out.append(s[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def dec(value) -> str:
    """Dekóduje hlavičku s encoded-words (=?utf-8?...?=)."""
    if value is None:
        return ""
    value = str(value)
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def get_header(msg, name: str) -> str:
    try:
        v = msg.get(name)
    except Exception:  # poškozená hlavička – vezmeme surovou hodnotu
        v = next((val for k, val in msg.raw_items() if k.lower() == name.lower()), None)
    return "" if v is None else dec(v)


def parse_addrs(value: str) -> list:
    try:
        pairs = getaddresses([value]) if value else []
    except Exception:
        return [{"name": value, "addr": ""}]
    return [{"name": dec(n), "addr": a} for n, a in pairs if a or n]


def parse_date(value: str):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


class _HtmlToText(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(re.sub(r"[ \t\r\n]+", " ", data))


def html_to_text(html: str) -> str:
    p = _HtmlToText()
    try:
        p.feed(html)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", "", html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def friendly_error(e: Exception) -> str:
    if isinstance(e, (MailError, google_oauth.OAuthError)):
        return str(e)
    if isinstance(e, smtplib.SMTPAuthenticationError) and b"application-specific password" in (e.smtp_error or b"").lower():
        return friendly_error(imaplib.IMAP4.error("Application-specific password required"))
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "Přihlášení k SMTP serveru selhalo – zkontrolujte jméno a heslo (u Gmailu/iCloudu použijte heslo aplikace)."
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "Server odmítl adresáty: " + ", ".join(e.recipients.keys())
    if isinstance(e, smtplib.SMTPException):
        return f"Chyba SMTP: {e}"
    if isinstance(e, imaplib.IMAP4.error):
        msg = str(e)
        m = re.fullmatch(r"b'(.*)'", msg)  # imaplib vrací text odpovědi jako repr bajtů
        if m:
            msg = m.group(1)
        if "application-specific password" in msg.lower():
            return ("Gmail nepřijímá běžné heslo. Použijte tlačítko „Přihlásit přes Google“ v dialogu účtu, "
                    "nebo místo hesla zadejte „heslo aplikace“ (myaccount.google.com → Zabezpečení → Hesla aplikací).")
        if "AUTHENTICATIONFAILED" in msg.upper() or "LOGIN" in msg.upper() or "credentials" in msg.lower():
            return "Přihlášení k IMAP serveru selhalo – zkontrolujte jméno a heslo (u Gmailu/iCloudu použijte heslo aplikace)."
        return f"Chyba IMAP: {msg}"
    if isinstance(e, socket.gaierror):
        return "Server nebyl nalezen – zkontrolujte adresu serveru."
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "Vypršel časový limit spojení se serverem."
    if isinstance(e, ssl.SSLError):
        return f"Chyba zabezpečeného spojení (zkontrolujte port a typ zabezpečení): {e}"
    if isinstance(e, ConnectionRefusedError):
        return "Server odmítl spojení – zkontrolujte port."
    if isinstance(e, OSError):
        return f"Chyba sítě: {e}"
    return f"Neočekávaná chyba: {e}"


# --------------------------------------------------------------------------
# Připojení
# --------------------------------------------------------------------------

def imap_connect(acc: dict) -> imaplib.IMAP4:
    ctx = ssl.create_default_context()
    if acc["imap_security"] == "ssl":
        M = imaplib.IMAP4_SSL(acc["imap_host"], int(acc["imap_port"]), ssl_context=ctx, timeout=TIMEOUT)
    else:
        M = imaplib.IMAP4(acc["imap_host"], int(acc["imap_port"]), timeout=TIMEOUT)
        M.starttls(ssl_context=ctx)
    try:
        if acc.get("auth") == "oauth2":
            auth_string, sent = google_oauth.xoauth2_string(acc).encode(), []

            def respond(_challenge):  # při chybě server pošle výzvu – odpovíme prázdně
                if sent:
                    return b""
                sent.append(True)
                return auth_string

            try:
                M.authenticate("XOAUTH2", respond)
            except imaplib.IMAP4.error as e:
                raise MailError(f"Google odmítl přihlášení k IMAP ({e}). Zkuste se v nastavení účtu přihlásit znovu.")
        else:
            M._encoding = "utf-8"  # hesla s diakritikou
            M.login(acc["username"], acc["password"])
        typ, data = M.capability()
        M.caps = set(data[0].decode().upper().split()) if typ == "OK" and data and data[0] else set()
    except Exception:
        try:
            M.shutdown()
        except Exception:
            pass
        raise
    return M


def smtp_connect(acc: dict) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    if acc["smtp_security"] == "ssl":
        s = smtplib.SMTP_SSL(acc["smtp_host"], int(acc["smtp_port"]), timeout=TIMEOUT, context=ctx)
    else:
        s = smtplib.SMTP(acc["smtp_host"], int(acc["smtp_port"]), timeout=TIMEOUT)
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
    try:
        if acc.get("auth") == "oauth2":
            auth_string = google_oauth.xoauth2_string(acc)
            s.ehlo_or_helo_if_needed()
            s.auth("XOAUTH2", lambda challenge=None: auth_string if challenge is None else "")
        else:
            s.login(acc["username"], acc["password"])
    except Exception:
        s.close()
        raise
    return s


class ImapPool:
    """Jedno udržované IMAP spojení na účet (chráněné zámkem)."""

    def __init__(self):
        self._entries = {}
        self._lock = threading.Lock()

    def _entry(self, acc_id):
        with self._lock:
            return self._entries.setdefault(acc_id, {"lock": threading.Lock(), "conn": None, "used": 0.0})

    @staticmethod
    def _close(entry):
        M, entry["conn"] = entry["conn"], None
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass

    @contextmanager
    def get(self, acc: dict):
        entry = self._entry(acc["id"])
        with entry["lock"]:
            if entry["conn"] is not None and time.time() - entry["used"] > 60:
                try:
                    entry["conn"].noop()
                except Exception:
                    entry["conn"] = None
            if entry["conn"] is None:
                entry["conn"] = imap_connect(acc)
            try:
                yield entry["conn"]
            except imaplib.IMAP4.abort:
                entry["conn"] = None
                raise
            except imaplib.IMAP4.error:
                raise  # odpověď NO/BAD – spojení je v pořádku
            except BaseException:
                self._close(entry)
                raise
            finally:
                entry["used"] = time.time()

    def drop(self, acc_id):
        entry = self._entry(acc_id)
        with entry["lock"]:
            self._close(entry)


def test_account(acc: dict):
    M = imap_connect(acc)
    try:
        M.logout()
    except Exception:
        pass
    s = smtp_connect(acc)
    try:
        s.quit()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Složky
# --------------------------------------------------------------------------

LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s*(?P<name>.*)$', re.I)
ROLE_FLAGS = {"\\sent": "sent", "\\trash": "trash", "\\drafts": "drafts", "\\junk": "junk",
              "\\archive": "archive", "\\all": "all", "\\flagged": "flagged"}
ROLE_NAMES = {
    "sent": {"sent", "sent items", "sent messages", "sent mail", "odeslané", "odeslana posta", "odeslaná pošta", "odeslane"},
    "trash": {"trash", "deleted", "deleted items", "deleted messages", "koš", "kos", "bin"},
    "drafts": {"drafts", "draft", "koncepty", "rozepsané", "rozepsane"},
    "junk": {"spam", "junk", "junk e-mail", "junk email", "nevyžádaná pošta", "hromadné"},
    "archive": {"archive", "archiv"},
}
ROLE_LABELS = {"inbox": "Doručená pošta", "sent": "Odeslaná pošta", "drafts": "Koncepty", "junk": "Spam",
               "trash": "Koš", "archive": "Archiv", "all": "Všechny zprávy", "flagged": "S hvězdičkou"}
ROLE_ORDER = ["inbox", "drafts", "sent", "flagged", "archive", "all", "junk", "trash"]


def list_folders(M, with_counts=True) -> list:
    typ, data = M.list()
    if typ != "OK":
        raise MailError("Nepodařilo se načíst seznam složek.")
    folders, used_roles = [], set()
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):  # název složky poslán jako literál
            line, literal = item[0].decode("utf-8", "replace"), item[1].decode("utf-8", "replace")
        else:
            line, literal = item.decode("utf-8", "replace"), None
        m = LIST_RE.match(line)
        if not m:
            continue
        flags = m["flags"].lower().split()
        if "\\noselect" in flags or "\\nonexistent" in flags:
            continue
        name = literal if literal is not None else _unquote(m["name"])
        delim = None if m["delim"].upper() == "NIL" else _unquote(m["delim"])
        segments = name.split(delim) if delim else [name]
        last = decode_utf7(segments[-1])

        role = None
        if name.upper() == "INBOX":
            role, name = "inbox", "INBOX"
        else:
            role = next((ROLE_FLAGS[f] for f in flags if f in ROLE_FLAGS), None)
            if role is None:
                role = next((r for r, names in ROLE_NAMES.items() if last.lower() in names), None)
        if role in used_roles:  # stejnou roli zobrazíme jen jednou
            role = None
        if role:
            used_roles.add(role)
        folders.append({
            "name": name,
            "path": decode_utf7(name),
            "display": ROLE_LABELS.get(role, last),
            "depth": 0 if role else len(segments) - 1,
            "role": role,
            "unseen": 0,
        })

    folders.sort(key=lambda f: (ROLE_ORDER.index(f["role"]) if f["role"] else len(ROLE_ORDER), f["path"].lower()))

    if with_counts:
        for f in folders:
            if f["role"] not in (None, "inbox"):
                continue
            try:
                typ, data = M.status(q(f["name"]), "(UNSEEN)")
                m = re.search(rb"UNSEEN (\d+)", data[0] if data and data[0] else b"")
                f["unseen"] = int(m.group(1)) if m else 0
            except imaplib.IMAP4.error:
                pass
    return folders


def find_role(M, role):
    return next((f["name"] for f in list_folders(M, with_counts=False) if f["role"] == role), None)


# --------------------------------------------------------------------------
# Zprávy
# --------------------------------------------------------------------------

UID_RE = re.compile(rb"UID (\d+)")
FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
NEW_ITEM_RE = re.compile(rb"^\d+ \(")


def _parse_fetch(data) -> list:
    """Převede odpověď FETCH na seznam [meta, literál]."""
    items = []
    for part in data or []:
        if isinstance(part, tuple):
            items.append([part[0], part[1]])
        elif isinstance(part, bytes):
            if items and not NEW_ITEM_RE.match(part):
                items[-1][0] += b" " + part  # např. FLAGS za literálem
            else:
                items.append([part, b""])
    return items


def _flags(meta: bytes) -> list:
    m = FLAGS_RE.search(meta)
    return m.group(1).decode().split() if m else []


def _select(M, folder, readonly):
    typ, data = M.select(q(folder), readonly=readonly)
    if typ != "OK":
        raise MailError(f"Složku „{decode_utf7(folder)}“ nelze otevřít.")


def _search(M, query):
    if not query:
        typ, data = M.uid("SEARCH", "ALL")
    elif query.isascii():
        typ, data = M.uid("SEARCH", "TEXT", q(query))
    else:
        M.literal = query.encode("utf-8")
        typ, data = M.uid("SEARCH", "CHARSET", "UTF-8", "TEXT")
    if typ != "OK":
        raise MailError("Vyhledávání selhalo.")
    return data[0].split() if data and data[0] else []


def list_messages(M, folder, page=0, page_size=50, query=None) -> dict:
    _select(M, folder, readonly=True)
    uids = sorted(_search(M, query), key=int, reverse=True)
    total = len(uids)
    chunk = uids[page * page_size:(page + 1) * page_size]
    messages = []
    if chunk:
        messages = [{k: v for k, v in r.items() if k in LIST_FIELDS} for r in fetch_header_records(M, chunk)]
        for m in messages:
            m["folder"] = folder
        messages.sort(key=lambda x: x["uid"], reverse=True)
    return {"total": total, "messages": messages, "has_more": (page + 1) * page_size < total}


def _fetch_raw(M, uid):
    typ, data = M.uid("FETCH", str(uid), "(UID FLAGS BODY.PEEK[])")
    for meta, lit in _parse_fetch(data):
        m = UID_RE.search(meta)
        if lit and m and int(m.group(1)) == uid:
            return lit, _flags(meta)
    raise MessageGone("Zpráva už na serveru není (byla smazána nebo přesunuta).")


def _safe_body(msg, prefs):
    try:
        return msg.get_body(preferencelist=prefs)
    except Exception:
        return None


def _part_text(part) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, "replace")
        except LookupError:
            return payload.decode("utf-8", "replace")


def _leaves(msg):
    return [p for p in msg.walk() if not p.is_multipart()]


def _filename(part):
    try:
        return part.get_filename()
    except Exception:
        return None


def fetch_raw_mark_seen(M, folder, uid):
    """Stáhne celou zprávu a označí ji jako přečtenou. Vrací (raw, flags)."""
    _select(M, folder, readonly=False)
    raw, flags = _fetch_raw(M, uid)
    if "\\Seen" not in flags:
        M.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
        flags.append("\\Seen")
    return raw, flags


def get_message(M, folder, uid) -> dict:
    raw, flags = fetch_raw_mark_seen(M, folder, uid)
    return parse_message(raw, uid, folder, flags)


def parse_message(raw: bytes, uid, folder, flags) -> dict:
    msg = email.message_from_bytes(raw, policy=policy.default)

    html_part = _safe_body(msg, ("html",))
    text_part = _safe_body(msg, ("plain",))
    html = _part_text(html_part) if html_part is not None else None
    text = _part_text(text_part) if text_part is not None else None

    attachments = []
    for idx, part in enumerate(_leaves(msg)):
        if part is html_part or part is text_part:
            continue
        ctype = part.get_content_type()
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        if html and cid and part.get_content_maintype() == "image" and f"cid:{cid}" in html:
            data = part.get_payload(decode=True) or b""
            html = html.replace(f"cid:{cid}", f"data:{ctype};base64,{base64.b64encode(data).decode()}")
            continue
        filename = _filename(part)
        if filename or part.get_content_disposition() == "attachment" or part.get_content_maintype() != "text":
            payload = part.get_payload(decode=True) or b""
            attachments.append({"index": idx, "filename": dec(filename) if filename else f"priloha-{idx}",
                                "content_type": ctype, "size": len(payload)})

    if html:
        html = re.sub(r"<meta[^>]+http-equiv\s*=\s*[\"']?refresh[^>]*>", "", html, flags=re.I)
    if text is None and html:
        text = html_to_text(html)

    return {
        "uid": uid,
        "folder": folder,
        "subject": get_header(msg, "Subject") or "(bez předmětu)",
        "from": parse_addrs(get_header(msg, "From")),
        "to": parse_addrs(get_header(msg, "To")),
        "cc": parse_addrs(get_header(msg, "Cc")),
        "reply_to": parse_addrs(get_header(msg, "Reply-To")),
        "date": parse_date(get_header(msg, "Date")),
        "message_id": get_header(msg, "Message-ID").strip(),
        "references": " ".join(get_header(msg, "References").split()),
        "html": html,
        "text": text or "",
        "attachments": attachments,
        "seen": "\\Seen" in flags,
        "flagged": "\\Flagged" in flags,
    }


def get_attachments(M, folder, uid, indexes) -> list:
    """Vrátí [(filename, content_type, data)] pro dané indexy částí."""
    _select(M, folder, readonly=True)
    raw, _ = _fetch_raw(M, uid)
    return attachments_from_raw(raw, indexes)


def attachments_from_raw(raw: bytes, indexes) -> list:
    leaves = _leaves(email.message_from_bytes(raw, policy=policy.default))
    out = []
    for i in indexes:
        if not 0 <= i < len(leaves):
            raise MailError("Příloha nebyla nalezena.")
        part = leaves[i]
        name = _filename(part)
        out.append((dec(name) if name else f"priloha-{i}", part.get_content_type(), part.get_payload(decode=True) or b""))
    return out


ALLOWED_FLAGS = {"\\Seen", "\\Flagged"}


def set_flag(M, folder, uid, flag, value):
    if flag not in ALLOWED_FLAGS:
        raise MailError("Nepodporovaný příznak.")
    _select(M, folder, readonly=False)
    typ, _ = M.uid("STORE", str(uid), "+FLAGS" if value else "-FLAGS", f"({flag})")
    if typ != "OK":
        raise MailError("Změna příznaku selhala.")


# --------------------------------------------------------------------------
# Synchronizace do lokální databáze
# --------------------------------------------------------------------------

LIST_FIELDS = {"uid", "subject", "from", "to", "date", "seen", "flagged", "has_attachments", "size"}
HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID CONTENT-TYPE"


def header_record(uid, flags, size, h) -> dict:
    """Metadata zprávy z hlaviček (compat32) – pro seznam i databázi."""
    subject = get_header(h, "Subject")
    return {
        "uid": uid,
        "message_id": get_header(h, "Message-ID").strip(),
        "subject": subject or "(bez předmětu)",
        "base_subject": base_subject(subject).lower(),
        "from": parse_addrs(str(h.get("From") or "")),
        "to": parse_addrs(str(h.get("To") or "")),
        "cc": parse_addrs(str(h.get("Cc") or "")),
        "date": parse_date(get_header(h, "Date")),
        "seen": "\\Seen" in flags,
        "flagged": "\\Flagged" in flags,
        "deleted": "\\Deleted" in flags,
        "has_attachments": "multipart/mixed" in get_header(h, "Content-Type").lower(),
        "size": size,
    }


def fetch_header_records(M, uids) -> list:
    """Metadata pro dané UID ve vybrané složce."""
    if not uids:
        return []
    uidset = ",".join(u.decode() if isinstance(u, bytes) else str(u) for u in uids)
    typ, data = M.uid("FETCH", uidset, f"(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
    out = []
    for meta, lit in _parse_fetch(data):
        m = UID_RE.search(meta)
        if not m or not lit:
            continue
        size = SIZE_RE.search(meta)
        h = email.message_from_bytes(lit, policy=policy.compat32)
        out.append(header_record(int(m.group(1)), _flags(meta), int(size.group(1)) if size else 0, h))
    return out


def folder_status(M, folder) -> dict:
    typ, data = M.status(q(folder), "(UIDVALIDITY MESSAGES)")
    if typ != "OK" or not data or not data[-1]:
        raise MailError(f"Stav složky „{decode_utf7(folder)}“ nelze zjistit.")
    line = data[-1] if isinstance(data[-1], bytes) else data[-1][0]

    def get(key):
        m = re.search(key + rb" (\d+)", line)
        return int(m.group(1)) if m else None
    return {"uidvalidity": get(rb"UIDVALIDITY"), "messages": get(rb"MESSAGES")}


def all_uids(M) -> set:
    typ, data = M.uid("SEARCH", "ALL")
    if typ != "OK":
        raise MailError("Seznam zpráv nelze načíst.")
    return {int(u) for u in data[0].split()} if data and data[0] else set()


def fetch_flags(M, uidset) -> dict:
    """{uid: (seen, flagged, deleted)} pro zadanou množinu (např. "1:*")."""
    typ, data = M.uid("FETCH", uidset, "(UID FLAGS)")
    out = {}
    for meta, _ in _parse_fetch(data):
        m = UID_RE.search(meta)
        if m:
            flags = _flags(meta)
            out[int(m.group(1))] = ("\\Seen" in flags, "\\Flagged" in flags, "\\Deleted" in flags)
    return out


def mark_unseen_by_id(M, folder, uid, message_id):
    """Označí zprávu jako nepřečtenou (probuzení odložené zprávy). Pokud se UID mezitím změnilo,
    dohledá zprávu podle Message-ID. Vrací aktuální UID, nebo None, pokud zpráva ve složce už není."""
    _select(M, folder, readonly=False)
    recs = fetch_header_records(M, [uid])
    if not recs or (message_id and recs[0]["message_id"] != message_id):
        uid = None
        if message_id and message_id.isascii():
            typ, data = M.uid("SEARCH", "HEADER", "Message-ID", q(message_id))
            found = data[0].split() if typ == "OK" and data and data[0] else []
            uid = int(found[-1]) if found else None
    if uid is None:
        return None
    M.uid("STORE", str(uid), "-FLAGS", "(\\Seen)")
    return uid


def _expunge_uids(M, uidset):
    M.uid("STORE", uidset, "+FLAGS", "(\\Deleted)")
    if "UIDPLUS" in M.caps:
        M.uid("EXPUNGE", uidset)
    else:
        M.expunge()


def _move_uids(M, uids, dest):
    """Přesune zprávy (ve vybrané složce otevřené pro zápis) do složky dest."""
    uids = [str(u) for u in uids]
    for i in range(0, len(uids), 500):
        uidset = ",".join(uids[i:i + 500])
        if "MOVE" in M.caps:
            typ, _ = M.uid("MOVE", uidset, q(dest))
            if typ == "OK":
                continue
        typ, _ = M.uid("COPY", uidset, q(dest))
        if typ != "OK":
            raise MailError(f"Přesun do složky „{decode_utf7(dest)}“ selhal.")
        _expunge_uids(M, uidset)


def _ensure_folder(M, folders, roles, fallback):
    """Název první složky s danou rolí; když žádná není, vytvoří složku fallback."""
    for role in roles:
        name = next((f["name"] for f in folders if f["role"] == role), None)
        if name:
            return name
    if not any(f["name"] == fallback for f in folders):
        typ, _ = M.create(q(fallback))
        if typ != "OK":
            raise MailError(f"Složku „{fallback}“ se nepodařilo vytvořit.")
        try:
            M.subscribe(q(fallback))
        except imaplib.IMAP4.error:
            pass
    return fallback


def delete_message(M, folder, uid):
    """Přesune zprávu do koše; pokud už v koši je (nebo koš neexistuje), smaže ji natrvalo."""
    trash = find_role(M, "trash")
    _select(M, folder, readonly=False)
    if trash and trash != folder:
        _move_uids(M, [uid], trash)
    else:
        _expunge_uids(M, str(uid))


def mark_seen_uids(M, folder, uids):
    _select(M, folder, readonly=False)
    uids = [str(u) for u in uids]
    for i in range(0, len(uids), 500):
        M.uid("STORE", ",".join(uids[i:i + 500]), "+FLAGS", "(\\Seen)")


def trash_uids(M, folder, uids):
    """Hromadné smazání (přesun do koše; bez koše natrvalo)."""
    if not uids:
        return
    trash = find_role(M, "trash")
    _select(M, folder, readonly=False)
    if trash and trash != folder:
        _move_uids(M, uids, trash)
    else:
        uids = [str(u) for u in uids]
        for i in range(0, len(uids), 500):
            _expunge_uids(M, ",".join(uids[i:i + 500]))


def archive_message(M, folder, uid):
    folders = list_folders(M, with_counts=False)
    # Gmail nemá složku Archiv – archivace = přesun do „Všechny zprávy“ (odebere štítek Doručené).
    dest = _ensure_folder(M, folders, ("archive", "all"), "Archive")
    if dest == folder:
        raise MailError("Zpráva už je v archivu.")
    _select(M, folder, readonly=False)
    _move_uids(M, [uid], dest)


def _fetch_headers(M, uids, fields="FROM TO CC SUBJECT DATE MESSAGE-ID"):
    """Vrátí [(uid, flags, hlavičky)] pro dané UID ve vybrané složce."""
    if not uids:
        return []
    typ, data = M.uid("FETCH", ",".join(str(u) for u in uids), f"(UID FLAGS BODY.PEEK[HEADER.FIELDS ({fields})])")
    out = []
    for meta, lit in _parse_fetch(data):
        m = UID_RE.search(meta)
        if m and lit:
            out.append((int(m.group(1)), _flags(meta), email.message_from_bytes(lit, policy=policy.compat32)))
    return out


def addr_set(h, name) -> set:
    return {a["addr"].lower() for a in parse_addrs(str(h.get(name) or "")) if a["addr"]}


def spam_message(M, folder, uid):
    """Přesune zprávu do spamu. Vrací (adresa odesílatele, datum) pro hromadné zpracování."""
    folders = list_folders(M, with_counts=False)
    dest = _ensure_folder(M, folders, ("junk",), "Spam")
    if dest == folder:
        raise MailError("Zpráva už je ve spamu.")
    _select(M, folder, readonly=False)
    headers = _fetch_headers(M, [uid], "FROM DATE")
    if not headers:
        raise MessageGone("Zpráva už na serveru není (byla smazána nebo přesunuta).")
    _, _, h = headers[0]
    sender = next(iter(addr_set(h, "From")), None)
    _move_uids(M, [uid], dest)
    return sender, parse_date(get_header(h, "Date"))


def move_to_spam(M, folder, uids) -> int:
    """Přesune dané zprávy do složky Spam (vytvoří ji, pokud chybí)."""
    folders = list_folders(M, with_counts=False)
    junk = _ensure_folder(M, folders, ("junk",), "Spam")
    if not uids or junk == folder:
        return 0
    _select(M, folder, readonly=False)
    _move_uids(M, uids, junk)
    return len(uids)


def move_sender_to_spam(M, sender, before_ts) -> int:
    """Přesune do spamu starší zprávy od odesílatele (Doručená pošta + archiv). Vrací počet."""
    if not sender.isascii():
        return 0
    folders = list_folders(M, with_counts=False)
    sources = [f["name"] for f in folders if f["role"] in ("inbox", "archive", "all")]
    junk = None
    moved = 0
    for folder in sources:
        _select(M, folder, readonly=False)
        typ, data = M.uid("SEARCH", "FROM", q(sender))
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        matches = []
        for uid, flags, h in _fetch_headers(M, [u.decode() for u in uids], "FROM DATE"):
            ts = parse_date(get_header(h, "Date"))
            # IMAP hledá podřetězec – ověříme přesnou shodu adresy; „starší“ = nejvýš stejné datum
            if sender in addr_set(h, "From") and "\\Deleted" not in flags and (ts is None or before_ts is None or ts <= before_ts):
                matches.append(uid)
        if matches:
            junk = junk or _ensure_folder(M, folders, ("junk",), "Spam")
            _select(M, folder, readonly=False)
            _move_uids(M, matches, junk)
            moved += len(matches)
    return moved


# --------------------------------------------------------------------------
# Související zprávy (kontext)
# --------------------------------------------------------------------------

SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:\[[^\]]{0,40}\]\s*)*"
    r"(?:re|fw|fwd|odp|př|aw|wg|tr|sv|vs|rv|antw|ref|réf)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*",
    re.I)


def base_subject(subject: str) -> str:
    """Předmět bez Re:/Fw:/Odp: apod. (i opakovaných), normalizovaný pro porovnání."""
    s = (subject or "").strip()
    while True:
        stripped = SUBJECT_PREFIX_RE.sub("", s, count=1)
        if stripped == s:
            break
        s = stripped
    return " ".join(s.split())


def find_related(M, me, counterpart, subject, exclude_mid, is_stale=lambda: False, limit=100):
    """Zprávy související s aktuální: od odesílatele, ode mě pro něj, s ním v kopii, nebo se stejným předmětem."""
    me, counterpart = me.lower(), (counterpart or "").lower()
    base = base_subject(subject)
    base_cmp = base.lower() if len(base) >= 3 else ""
    folders = list_folders(M, with_counts=False)
    if any(f["role"] == "all" for f in folders):  # Gmail: štítky jsou jen pohledy na „Všechny zprávy“
        folders = [f for f in folders if f["role"] in ("inbox", "sent", "drafts", "junk", "all")]
    else:
        folders = [f for f in folders if f["role"] != "trash"]
    folders.sort(key=lambda f: f["role"] == "all")  # „Všechny zprávy“ až nakonec (duplicity)

    found, seen_mids = [], {exclude_mid} if exclude_mid else set()
    for f in folders:
        if is_stale():
            return None
        try:
            _select(M, f["name"], readonly=True)
        except MailError:
            continue
        uids = set()
        if counterpart and counterpart.isascii() and me.isascii():
            cp, my = q(counterpart), q(me)
            typ, data = M.uid("SEARCH", f"OR OR FROM {cp} CC {cp} (FROM {my} TO {cp})")
            if typ == "OK" and data and data[0]:
                uids.update(data[0].split())
        if base_cmp:
            try:
                if base.isascii():
                    typ, data = M.uid("SEARCH", "SUBJECT", q(base))
                else:
                    M.literal = base.encode("utf-8")
                    typ, data = M.uid("SEARCH", "CHARSET", "UTF-8", "SUBJECT")
                if typ == "OK" and data and data[0]:
                    uids.update(data[0].split())
            except imaplib.IMAP4.error:  # server nepodporuje UTF-8 hledání
                M.literal = None
        uids = sorted({int(u) for u in uids}, reverse=True)[:limit]
        for uid, flags, h in _fetch_headers(M, uids):
            if "\\Deleted" in flags:
                continue
            frm, to, cc = addr_set(h, "From"), addr_set(h, "To"), addr_set(h, "Cc")
            subj = get_header(h, "Subject")
            reasons = []
            if counterpart and counterpart in frm:
                reasons.append("sender")
            if counterpart and me in frm and counterpart in to:
                reasons.append("mine")
            if counterpart and counterpart in cc:
                reasons.append("cc")
            if base_cmp and base_subject(subj).lower() == base_cmp:
                reasons.append("subject")
            if not reasons:
                continue
            mid = get_header(h, "Message-ID").strip()
            if mid and mid in seen_mids:
                continue
            seen_mids.add(mid)
            found.append({
                "uid": uid, "folder": f["name"], "folder_display": f["display"], "folder_role": f["role"],
                "subject": subj or "(bez předmětu)",
                "from": parse_addrs(str(h.get("From") or "")), "to": parse_addrs(str(h.get("To") or "")),
                "date": parse_date(get_header(h, "Date")), "seen": "\\Seen" in flags, "reasons": reasons,
            })
    found.sort(key=lambda m: m["date"] or 0, reverse=True)
    return found[:limit]


# --------------------------------------------------------------------------
# Odesílání
# --------------------------------------------------------------------------

# Tyto servery ukládají odeslanou poštu do složky Odeslané samy.
AUTO_SENT_HOSTS = ("gmail.com", "googlemail.com", "office365.com", "outlook.com")


ADDR_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_recipients(**fields) -> list:
    """Adresy ze zadaných polí (Komu, Kopie, Skrytá). Každé pole se parsuje zvlášť –
    getaddresses() v Pythonu 3.13 vrátí pro celý vstup prázdný výsledek, pokud je některé pole prázdné."""
    labels = {"to": "Komu", "cc": "Kopie", "bcc": "Skrytá"}
    out = []
    for key, value in fields.items():
        value = (value or "").strip().strip(",;").replace(";", ",")
        if not value:
            continue
        addrs = [a for _, a in getaddresses([value])]
        if not addrs or not all(ADDR_RE.match(a) for a in addrs):
            raise MailError(f"Neplatná adresa v poli {labels.get(key, key)}: „{value}“. "
                            "Více adres oddělte čárkou, např. jan@example.cz, petr@example.cz")
        out += addrs
    return list(dict.fromkeys(out))  # bez duplicit, v původním pořadí


def build_message(acc, to, cc, subject, body, in_reply_to=None, references=None, attachments=()):
    msg = EmailMessage()
    msg["From"] = formataddr((acc.get("display_name") or "", acc["email"]))
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=acc["email"].rsplit("@", 1)[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body, cte="quoted-printable")
    for filename, ctype, data in attachments:
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
    # Deklarace jazyka – bez ní klient jazyk jen odhaduje z textu (a u krátkých zpráv se plete).
    # Až po set_content/add_attachment, které hlavičky Content-* mažou či přesouvají.
    msg["Content-Language"] = "cs"
    return msg


def send_message(acc, msg, recipients, pool: ImapPool):
    if not recipients:
        raise MailError("Zadejte alespoň jednoho adresáta.")
    s = smtp_connect(acc)
    try:
        s.send_message(msg, to_addrs=recipients)
    finally:
        try:
            s.quit()
        except Exception:
            pass

    if acc["smtp_host"].lower().endswith(AUTO_SENT_HOSTS):
        return None
    try:  # uložení kopie do Odeslaných; chyba zde nesmí zrušit úspěšné odeslání
        with pool.get(acc) as M:
            sent = find_role(M, "sent")
            if sent:
                M.append(q(sent), "(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        return None
    except Exception as e:
        return "Zpráva byla odeslána, ale nepodařilo se uložit kopii do Odeslaných: " + friendly_error(e)
