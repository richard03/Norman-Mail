"""Bayesovský filtr spamu.

Učí se z toho, co uživatel sám označí: tlačítko Spam / přetažení do Spamu = spam, „Není spam“ = dobrá zpráva.
Dobré zprávy přibývají i samy: zpráva, kterou filtr posoudil, nechal v doručené poště a uživatel ji
přečetl a do 3 dnů neoznačil jako spam, se naučí jako dobrá. Napoprvé se filtr naučí z existujících zpráv
(spam = zprávy ve spamu od odesílatelů z pravidel, tj. dřív ručně označené; dobré = přečtené v doručené poště).

Výpočet je Robinsonova/Fisherova metoda (jako SpamBayes): pravděpodobnost každého slova (tokenu) se
vyhladí podle počtu výskytů a nejvýraznější slova se zkombinují chí-kvadrát testem. Výsledek je 0–1.
"""
import email
import hashlib
import json
import logging
import math
import re
import threading
import time
from email import policy

import mail

log = logging.getLogger("mailer.bayes")

FETCH_BYTES = 32768          # z každé zprávy stačí začátek (hlavičky + text)
MIN_SPAM = 10                # dokud se filtr nenaučí aspoň tolik spamu a dobrých zpráv, nic nepřesouvá
MIN_HAM = 10
MAX_TOKENS = 150             # kolik nejvýraznějších slov se kombinuje
UNKNOWN_STRENGTH = 0.45      # síla předpokladu pro málo známá slova (SpamBayes s)
UNKNOWN_PROB = 0.5           # předpoklad pro neznámé slovo (SpamBayes x)
MIN_DIST = 0.1               # slova blízko 0,5 nic neříkají
HAM_AFTER = 3 * 86400        # přečtená zpráva ponechaná v doručené poště 3 dny = dobrá
FORGET_AFTER = 30 * 86400    # posudky nepřečtených zpráv se zapomenou
RECENT = 3 * 86400           # posuzují se jen čerstvé zprávy (ne historie při první synchronizaci)
BOOTSTRAP_LIMIT = 300        # kolik zpráv každého druhu se napoprvé naučí
THRESHOLDS = {"high": 0.80, "medium": 0.90, "low": 0.97}  # citlivost → hranice pro přesun do spamu

SCHEMA = """
CREATE TABLE IF NOT EXISTS bayes_tokens(token TEXT PRIMARY KEY, spam INTEGER DEFAULT 0, ham INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS bayes_trained(key TEXT PRIMARY KEY, is_spam INTEGER, tokens TEXT, created REAL);
CREATE TABLE IF NOT EXISTS bayes_messages(
    key TEXT PRIMARY KEY, account_id TEXT, message_id TEXT, tokens TEXT, score REAL, moved INTEGER, created REAL);
"""

WORD_RE = re.compile(r"[^\W_][\w'’$€%-]*", re.UNICODE)
URL_RE = re.compile(r"https?://([^/\s\"'<>?#:]+)", re.I)
HTML_ATTR_URL_RE = re.compile(r"""(?:href|src)\s*=\s*["']?https?://([^/\s"'<>?#:]+)""", re.I)
IMG_RE = re.compile(r"<img\b", re.I)


# --------------------------------------------------------------------------
# Slova (tokeny) zprávy
# --------------------------------------------------------------------------

def _domain_tokens(prefix, host):
    host = host.lower().strip(".")
    parts = host.split(".")
    out = {f"{prefix}:{host}"}
    if len(parts) > 2:
        out.add(f"{prefix}:{'.'.join(parts[-2:])}")
    return out


def _words(text, prefix=""):
    out = set()
    for w in WORD_RE.findall(text.lower()):
        w = w.strip("'’-")
        if len(w) < 3:
            continue
        if len(w) > 20:  # dlouhé „slovo“ = spíš kód nebo kódované smetí
            out.add(f"{prefix}skip:{w[0]}{len(w) // 10 * 10}")
            continue
        out.add(prefix + w)
    return out


def _bucket(n):
    return "0" if n == 0 else "1" if n == 1 else "2-4" if n < 5 else "5-9" if n < 10 else "10+"


def message_tokens(raw: bytes) -> set:
    """Množina slov zprávy (z hlaviček i textu). Stačí i useknutý začátek zprávy."""
    msg = email.message_from_bytes(raw, policy=policy.compat32)
    tokens = set()
    subject = mail.get_header(msg, "Subject")
    tokens |= _words(subject, "subj:")
    if subject and subject.upper() == subject and any(c.isalpha() for c in subject):
        tokens.add("subj:ALLCAPS")
    if "!" in subject:
        tokens.add("subj:exclaim")
    senders = mail.parse_addrs(mail.get_header(msg, "From"))
    from_domain = ""
    for a in senders[:1]:
        addr = (a.get("addr") or "").lower()
        if addr:
            tokens.add("from:" + addr)
            if "@" in addr:
                from_domain = addr.rsplit("@", 1)[1]
                tokens |= _domain_tokens("fromdom", from_domain)
        tokens |= _words(a.get("name") or "", "fromname:")
    reply_to = mail.parse_addrs(mail.get_header(msg, "Reply-To"))
    if reply_to and reply_to[0].get("addr") and from_domain:
        if not reply_to[0]["addr"].lower().endswith("@" + from_domain):
            tokens.add("replyto:otherdomain")
    recipients = len(mail.parse_addrs(mail.get_header(msg, "To"))) + len(mail.parse_addrs(mail.get_header(msg, "Cc")))
    tokens.add("rcpt:" + _bucket(recipients))
    for name in ("X-Mailer", "User-Agent"):
        v = mail.get_header(msg, name)
        if v:
            tokens.add("mailer:" + v.split()[0].lower()[:30])
    if msg.get("List-Unsubscribe"):
        tokens.add("hdr:list-unsubscribe")
    if msg.get("Precedence"):
        tokens.add("precedence:" + str(msg.get("Precedence")).strip().lower()[:20])
    tokens.add("ctype:" + msg.get_content_type())

    plain, html, images = [], [], 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            tokens.add("part:" + ctype)
            if part.get_filename():
                ext = part.get_filename().rsplit(".", 1)[-1].lower()[:8]
                tokens.add("attach:" + ext)
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "replace")
        except LookupError:
            text = payload.decode("utf-8", "replace")
        if ctype == "text/html":
            html.append(text)
        else:
            plain.append(text)
    for h in html:
        images += len(IMG_RE.findall(h))
        for host in HTML_ATTR_URL_RE.findall(h):
            tokens |= _domain_tokens("url", host)
    if html and not plain:
        tokens.add("body:htmlonly")
    tokens.add("img:" + _bucket(images))
    text = "\n".join(plain) if plain else "\n".join(mail.html_to_text(h) for h in html)
    for host in URL_RE.findall(text):
        tokens |= _domain_tokens("url", host)
    text = URL_RE.sub(" ", text)
    tokens |= _words(text[:20000])
    return tokens


def message_key(message_id, sender, date, subject):
    """Totožnost zprávy napříč složkami a účty (Message-ID, jinak otisk odesílatele, data a předmětu)."""
    mid = (message_id or "").strip()
    if mid:
        return mid
    return "h:" + hashlib.sha1(f"{sender}|{date}|{subject}".encode("utf-8", "replace")).hexdigest()


def record_key(rec):
    sender = rec["from"][0]["addr"].lower() if rec.get("from") else ""
    return message_key(rec.get("message_id"), sender, rec.get("date"), rec.get("subject"))


def fetch_tokens(M, uids) -> dict:
    """{uid: tokeny} – ze začátku zpráv ve vybrané složce (bez označení jako přečtené)."""
    out = {}
    uids = [str(u) for u in uids]
    for i in range(0, len(uids), 25):
        typ, data = M.uid("FETCH", ",".join(uids[i:i + 25]), f"(UID BODY.PEEK[]<0.{FETCH_BYTES}>)")
        if typ != "OK":
            continue
        for meta, lit in mail._parse_fetch(data):
            m = mail.UID_RE.search(meta)
            if m and lit:
                try:
                    out[int(m.group(1))] = message_tokens(lit)
                except Exception:
                    log.exception("Zprávu %s nejde rozložit na slova", m.group(1))
    return out


# --------------------------------------------------------------------------
# Výpočet
# --------------------------------------------------------------------------

def chi2q(x2, v):
    """P(chi² s v stupni volnosti >= x2), v sudé."""
    m = x2 / 2.0
    total = term = math.exp(-m)
    for i in range(1, v // 2):
        term *= m / i
        total += term
    return min(total, 1.0)


def combine(probs):
    if not probs:
        return 0.5
    n = len(probs)
    s = 1.0 - chi2q(-2.0 * sum(math.log(1.0 - p) for p in probs), 2 * n)
    h = 1.0 - chi2q(-2.0 * sum(math.log(p) for p in probs), 2 * n)
    return (s - h + 1.0) / 2.0


def token_prob(spam, ham, nspam, nham):
    """Vyhlazená pravděpodobnost, že zpráva s tímto slovem je spam."""
    sr, hr = spam / nspam, ham / nham
    p = sr / (sr + hr) if sr + hr else UNKNOWN_PROB
    n = spam + ham
    f = (UNKNOWN_STRENGTH * UNKNOWN_PROB + n * p) / (UNKNOWN_STRENGTH + n)
    return min(0.99, max(0.01, f))


# --------------------------------------------------------------------------
# Filtr (úložiště v databázi aplikace)
# --------------------------------------------------------------------------

class BayesFilter:
    def __init__(self, db):
        self.db = db
        with db._lock:
            db._conn.executescript(SCHEMA)
        self.bootstrap_state = {"status": "idle", "done": 0, "total": 0}
        self._bootstrap_lock = threading.Lock()

    # ---- stav
    def counts(self):
        rows = dict(self.db._q("SELECT is_spam, COUNT(*) FROM bayes_trained GROUP BY is_spam"))
        return rows.get(1, 0), rows.get(0, 0)

    def ready(self):
        ns, nh = self.counts()
        return ns >= MIN_SPAM and nh >= MIN_HAM

    def threshold(self):
        return THRESHOLDS.get(self.db.settings().get("bayes_sensitivity"), THRESHOLDS["medium"])

    def active(self):
        return bool(self.db.settings().get("bayes_enabled")) and self.ready()

    def status(self):
        ns, nh = self.counts()
        moved = self.db._q("SELECT COUNT(*) FROM bayes_messages WHERE moved=1 AND created>?",
                           (time.time() - 7 * 86400,))[0][0]
        return {"spam": ns, "ham": nh, "min_spam": MIN_SPAM, "min_ham": MIN_HAM, "ready": ns >= MIN_SPAM and nh >= MIN_HAM,
                "tokens": self.db._q("SELECT COUNT(*) FROM bayes_tokens")[0][0], "moved_week": moved,
                "bootstrap": dict(self.bootstrap_state)}

    # ---- učení
    def train(self, key, tokens, is_spam) -> bool:
        """Naučí zprávu jako spam / dobrou. Pokud už byla naučená opačně, posudek se přehodí."""
        is_spam = int(bool(is_spam))
        tokens = sorted(tokens)

        def tx(c):
            row = c.execute("SELECT is_spam, tokens FROM bayes_trained WHERE key=?", (key,)).fetchone()
            if row and row[0] == is_spam:
                return False
            if row:
                old = "spam" if row[0] else "ham"
                c.executemany(f"UPDATE bayes_tokens SET {old}=MAX({old}-1, 0) WHERE token=?",
                              [(t,) for t in json.loads(row[1])])
            col = (1, 0) if is_spam else (0, 1)
            c.executemany("""INSERT INTO bayes_tokens(token, spam, ham) VALUES(?, ?, ?)
                             ON CONFLICT(token) DO UPDATE SET spam=spam+excluded.spam, ham=ham+excluded.ham""",
                          [(t, *col) for t in tokens])
            c.execute("INSERT OR REPLACE INTO bayes_trained VALUES(?, ?, ?, ?)",
                      (key, is_spam, json.dumps(tokens, ensure_ascii=False), time.time()))
            c.execute("DELETE FROM bayes_messages WHERE key=?", (key,))
            return True
        return self.db._tx(tx)

    def reset(self):
        def tx(c):
            for t in ("bayes_tokens", "bayes_trained", "bayes_messages"):
                c.execute(f"DELETE FROM {t}")
            c.execute("DELETE FROM meta WHERE key='bayes_bootstrapped'")
        self.db._tx(tx)

    def stored_tokens(self, key):
        """Slova zprávy, kterou filtr už posuzoval (ať se nemusí stahovat znovu)."""
        rows = self.db._q("SELECT tokens FROM bayes_messages WHERE key=?", (key,)) or \
            self.db._q("SELECT tokens FROM bayes_trained WHERE key=?", (key,))
        return set(json.loads(rows[0][0])) if rows else None

    def learn(self, M, acc_id, folder, uids, is_spam) -> int:
        """Naučí zprávy (podle UID ve složce) – slova z dřívějšího posudku, jinak ze serveru. Vrací počet."""
        keys, missing = {}, []
        for uid in uids:
            row = self.db.message(acc_id, folder, uid)
            key = record_key(row) if row else None
            tokens = self.stored_tokens(key) if key else None
            if tokens is not None:
                keys[uid] = (key, tokens)
            else:
                missing.append(uid)
        if missing:
            mail._select(M, folder, readonly=True)
            fetched = fetch_tokens(M, missing)
            recs = {r["uid"]: r for r in mail.fetch_header_records(M, missing)}
            for uid, tokens in fetched.items():
                if uid in recs:
                    keys[uid] = (record_key(recs[uid]), tokens)
        n = sum(self.train(key, tokens, is_spam) for key, tokens in keys.values())
        if n:
            log.info("Bayes: naučeno %d zpráv jako %s", n, "spam" if is_spam else "dobré")
        return n

    # ---- posuzování
    def score(self, tokens):
        ns, nh = self.counts()
        if ns < MIN_SPAM or nh < MIN_HAM or not tokens:
            return None
        counts = {}
        toks = list(tokens)
        for i in range(0, len(toks), 500):
            chunk = toks[i:i + 500]
            for t, s, h in self.db._q(f"SELECT token, spam, ham FROM bayes_tokens WHERE token IN "
                                      f"({','.join('?' * len(chunk))})", chunk):
                counts[t] = (s, h)
        probs = [token_prob(s, h, ns, nh) for s, h in counts.values() if s + h]
        probs = sorted((p for p in probs if abs(p - 0.5) >= MIN_DIST), key=lambda p: -abs(p - 0.5))[:MAX_TOKENS]
        return combine(probs)

    def explain(self, tokens, n=8):
        """Nejvýraznější slova pro i proti spamu (pro zobrazení u zprávy)."""
        ns, nh = self.counts()
        if not ns or not nh:
            return []
        toks = list(tokens)[:2000]
        rows = self.db._q(f"SELECT token, spam, ham FROM bayes_tokens WHERE token IN ({','.join('?' * len(toks))})", toks) if toks else []
        scored = sorted(((token_prob(s, h, ns, nh), t) for t, s, h in rows if s + h), key=lambda x: -abs(x[0] - 0.5))
        return [{"token": t, "p": round(p, 3)} for p, t in scored[:n]]

    def remember(self, key, acc_id, message_id, tokens, score, moved):
        self.db._q("INSERT OR REPLACE INTO bayes_messages VALUES(?, ?, ?, ?, ?, ?, ?)",
                   (key, acc_id, message_id or "", json.dumps(sorted(tokens), ensure_ascii=False),
                    score, int(moved), time.time()))

    def verdict(self, message_id_key):
        rows = self.db._q("SELECT score, moved, tokens FROM bayes_messages WHERE key=?", (message_id_key,))
        if not rows:
            return None
        return {"score": rows[0][0], "moved": bool(rows[0][1]), "tokens": json.loads(rows[0][2])}

    # ---- dobré zprávy přibývají samy
    def mature(self, now=None):
        """Přečtené zprávy, které po posouzení zůstaly 3 dny v doručené poště, se naučí jako dobré."""
        now = now or time.time()
        rows = self.db._q("SELECT key, account_id, message_id, tokens FROM bayes_messages WHERE moved=0 AND created<?",
                          (now - HAM_AFTER,))
        learned = 0
        for key, acc_id, mid, tokens in rows:
            seen = self.db._q("SELECT seen FROM messages WHERE account_id=? AND folder='INBOX' AND message_id=?",
                              (acc_id, mid)) if mid else []
            if seen and seen[0][0]:
                learned += self.train(key, json.loads(tokens), False)
            elif not seen:  # zpráva už v doručené poště není (smazaná, přesunutá) – nic se neučí
                self.db._q("DELETE FROM bayes_messages WHERE key=?", (key,))
        self.db._q("DELETE FROM bayes_messages WHERE created<?", (now - FORGET_AFTER,))
        return learned

    # ---- první naučení z existujících zpráv
    def needs_bootstrap(self):
        return not self.db._q("SELECT 1 FROM meta WHERE key='bayes_bootstrapped'")

    def bootstrap(self, store, pool):
        """Napoprvé: spam = zprávy ve spamu od odesílatelů z pravidel (dřív ručně označené),
        dobré = přečtené zprávy v doručené poště. Běží na pozadí."""
        if not self._bootstrap_lock.acquire(blocking=False):
            return
        try:
            spam_senders = self.db.spam_senders()
            junk = self.db.spam_folders()
            accounts = {a["id"]: a for a in store.all()}
            work = []  # (účet, složka, uid, je_spam)
            spam_rows = []
            for acc_id, folder in junk.items():
                if acc_id in accounts and spam_senders:
                    spam_rows += [(r[2], acc_id, folder, r[0]) for r in self.db._q(
                        "SELECT uid, from_addrs, date FROM messages WHERE account_id=? AND folder=?", (acc_id, folder))
                        if any(f",{s}," in (r[1] or "") for s in spam_senders)]
            spam_rows.sort(key=lambda x: -(x[0] or 0))
            work += [(a, f, u, True) for _, a, f, u in spam_rows[:BOOTSTRAP_LIMIT]]
            marks = ",".join("?" * len(accounts)) or "''"
            ham_rows = self.db._q(f"""SELECT account_id, uid, from_addrs FROM messages WHERE folder='INBOX' AND seen=1
                                      AND account_id IN ({marks}) ORDER BY date DESC LIMIT ?""",
                                  (*accounts, BOOTSTRAP_LIMIT * 2))
            ham = [(a, "INBOX", u, False) for a, u, fa in ham_rows
                   if not any(f",{s}," in (fa or "") for s in spam_senders)][:BOOTSTRAP_LIMIT]
            work += ham
            self.bootstrap_state = {"status": "running", "done": 0, "total": len(work)}
            groups = {}
            for acc_id, folder, uid, is_spam in work:
                groups.setdefault((acc_id, folder, is_spam), []).append(uid)
            for (acc_id, folder, is_spam), uids in groups.items():
                for i in range(0, len(uids), 25):
                    chunk = uids[i:i + 25]
                    try:
                        with pool.get(accounts[acc_id]) as M:
                            self.learn(M, acc_id, folder, chunk, is_spam)
                    except Exception as e:
                        log.warning("Bayes: první učení (%s): %s", acc_id, e)
                    self.bootstrap_state["done"] += len(chunk)
            self.db._q("INSERT OR REPLACE INTO meta VALUES('bayes_bootstrapped', ?)", (str(time.time()),))
            ns, nh = self.counts()
            log.info("Bayes: první učení hotovo – %d spam, %d dobrých", ns, nh)
        finally:
            self.bootstrap_state["status"] = "done"
            self._bootstrap_lock.release()
