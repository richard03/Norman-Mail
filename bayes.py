"""Bayesovský filtr: jeden model pro spam, doručenou poštu, Nedůležité, Notifikace a složky aplikace.

Třídy (cíle): "spam", "inbox" (zpráva zůstává v Důležitých / Vzít na vědomí), "unimportant", "notifications"
a "folder:<id>" pro každou složku aplikace.

Učí se jen z rozhodnutí uživatele a jeho pravidel, nikdy ze svých vlastních:
- ruční akce: Spam, Není spam, ruční přesun do kategorie / pohledu / složky (jen zpráva i s pravidlem);
- potvrzené zprávy: posouzená zpráva, která po 3 dnech leží tam, kam ji dal uživatel nebo jeho pravidlo
  (přečtená v doručené poště = inbox, s kategorií z ručního umístění / pravidla = ta kategorie, ve složce
  aplikace = ta složka). Zprávy, které zařadil sám filtr (tabulka bf_auto), se nepočítají, dokud je uživatel
  ručně nepotvrdí;
- napoprvé z existujících zpráv (spam od odesílatelů z pravidel, přečtená doručená pošta, kategorie, složky).

Slova jsou z hlaviček i textu (prvních 32 kB zprávy). U nových zpráv v doručené poště se stáhnou jednou
a slouží učení i posuzování.

Výpočet: každá třída se posoudí Robinsonovou/Fisherovou metodou „tato třída proti ostatním“ (jako SpamBayes):
pravděpodobnost slov se vyhladí podle počtu výskytů a nejvýraznější slova se zkombinují chí-kvadrát testem.
Výsledek 0–1; u nejistých zpráv vychází kolem 0,5 a filtr nic nedělá. Když je jistých víc tříd, vyhrává
důležitější cíl: doručená pošta > složka > Nedůležité > Notifikace > Spam.
"""
import email
import json
import logging
import math
import re
import threading
import time
from email import policy

import db as db_module
import mail

log = logging.getLogger("mailer.bayes")

FETCH_BYTES = 32768          # z každé zprávy stačí začátek (hlavičky + text)
MIN_CLASS = 10               # třída s menším počtem naučených zpráv se neposuzuje (doručená pošta taky)
MAX_TOKENS = 150             # kolik nejvýraznějších slov se kombinuje
UNKNOWN_STRENGTH = 0.45      # síla předpokladu pro málo známá slova (SpamBayes s)
UNKNOWN_PROB = 0.5           # předpoklad pro neznámé slovo (SpamBayes x)
MIN_DIST = 0.1               # slova blízko 0,5 nic neříkají
CONFIRM_AFTER = 3 * 86400    # posouzená zpráva, která 3 dny leží na svém místě, se naučí jako potvrzená
FORGET_AFTER = 30 * 86400    # slova posouzených zpráv se zapomenou
RECENT = 3 * 86400           # posuzují se jen čerstvé zprávy (ne historie při první synchronizaci)
BOOTSTRAP_LIMIT = 300        # kolik zpráv každé třídy se napoprvé naučí
THRESHOLDS = {"high": 0.80, "medium": 0.90, "low": 0.97}  # citlivost → hranice pro přesun do spamu
AUTO_THRESHOLD = 0.95        # jistota pro automatické zařazení do kategorie / složky
SUGGEST_THRESHOLD = 0.80     # jistota pro návrh u zprávy
SPAM, INBOX = "spam", "inbox"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bf_tokens(token TEXT, cls TEXT, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(token, cls));
CREATE TABLE IF NOT EXISTS bf_trained(key TEXT PRIMARY KEY, cls TEXT NOT NULL, tokens TEXT, created REAL);
CREATE INDEX IF NOT EXISTS ix_bf_trained_cls ON bf_trained(cls);
CREATE TABLE IF NOT EXISTS bf_seen(key TEXT PRIMARY KEY, account_id TEXT, tokens TEXT, created REAL);
"""


def priority(cls):
    """Pořadí důležitosti cílů (menší = důležitější) pro spor uvnitř filtru."""
    if cls == INBOX:
        return 0
    if cls.startswith("folder:"):
        return 1
    return {"unimportant": 2, "notifications": 3, SPAM: 4}.get(cls, 5)


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


def token_prob(n_in, n_out, total_in, total_out):
    """Vyhlazená pravděpodobnost, že zpráva s tímto slovem patří do třídy (proti ostatním)."""
    ir, orr = n_in / total_in, n_out / total_out
    p = ir / (ir + orr) if ir + orr else UNKNOWN_PROB
    n = n_in + n_out
    f = (UNKNOWN_STRENGTH * UNKNOWN_PROB + n * p) / (UNKNOWN_STRENGTH + n)
    return min(0.99, max(0.01, f))


def raw_tokens(raw: bytes):
    """Slova z (začátku) uložené zprávy; None, když ji nejde rozložit."""
    try:
        return message_tokens(raw[:FETCH_BYTES])
    except Exception:
        log.exception("Zprávu nejde rozložit na slova")
        return None


# --------------------------------------------------------------------------
# Filtr (úložiště v databázi aplikace)
# --------------------------------------------------------------------------

class BayesFilter:
    def __init__(self, db):
        self.db = db
        with db._lock:
            db._conn.executescript(SCHEMA)
        self._migrate()
        self.bootstrap_state = {"status": "idle", "done": 0, "total": 0}
        self._bootstrap_lock = threading.Lock()

    def _migrate(self):
        """Dřívější filtr jen spam / ne-spam: naučené zprávy → třídy spam / inbox, přesuny do spamu → bf_auto."""
        c = self.db._conn
        with self.db._lock:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bayes_trained'").fetchone():
                return
            rows = c.execute("SELECT key, is_spam, tokens, created FROM bayes_trained").fetchall()
            counts = {}
            for key, is_spam, tokens, created in rows:
                cls = SPAM if is_spam else INBOX
                c.execute("INSERT OR IGNORE INTO bf_trained VALUES(?, ?, ?, ?)", (key, cls, tokens, created))
                for t in json.loads(tokens or "[]"):
                    counts[(t, cls)] = counts.get((t, cls), 0) + 1
            c.executemany("INSERT OR REPLACE INTO bf_tokens VALUES(?, ?, ?)",
                          [(t, cls, n) for (t, cls), n in counts.items()])
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bayes_messages'").fetchone():
                for key, acc_id, tokens, score, moved, created in c.execute(
                        "SELECT key, account_id, tokens, score, moved, created FROM bayes_messages").fetchall():
                    c.execute("INSERT OR IGNORE INTO bf_seen VALUES(?, ?, ?, ?)", (key, acc_id, tokens, created))
                    if moved:
                        c.execute("INSERT OR IGNORE INTO bf_auto VALUES(?, ?, ?, ?)", (key, SPAM, score, created))
            for t in ("bayes_tokens", "bayes_trained", "bayes_messages"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
        log.info("Bayes: převedeno %d naučených zpráv na třídy", len(rows))

    # ---- stav
    def counts(self) -> dict:
        """Počet naučených zpráv po třídách."""
        return dict(self.db._q("SELECT cls, COUNT(*) FROM bf_trained GROUP BY cls"))

    def eligible(self, counts=None) -> dict:
        """Třídy, které má smysl posuzovat (dost naučených zpráv); bez doručené pošty žádné."""
        counts = self.counts() if counts is None else counts
        out = {c: n for c, n in counts.items() if n >= MIN_CLASS}
        return out if INBOX in out and len(out) >= 2 else {}

    def ready(self):
        return bool(self.eligible())

    def threshold(self):
        return THRESHOLDS.get(self.db.settings().get("bayes_sensitivity"), THRESHOLDS["medium"])

    def active(self):
        return bool(self.db.settings().get("bayes_enabled")) and self.ready()

    def status(self):
        counts = self.counts()
        moved = dict(self.db._q("SELECT target, COUNT(*) FROM bf_auto WHERE created>? GROUP BY target",
                                (time.time() - 7 * 86400,)))
        return {"classes": counts, "eligible": sorted(self.eligible(counts)), "min_class": MIN_CLASS,
                "ready": bool(self.eligible(counts)), "moved_week": moved,
                "tokens": self.db._q("SELECT COUNT(DISTINCT token) FROM bf_tokens")[0][0],
                "bootstrap": dict(self.bootstrap_state)}

    # ---- učení
    def train(self, key, tokens, cls) -> bool:
        """Naučí zprávu jako třídu. Pokud už byla naučená jako jiná třída, posudek se přehodí."""
        tokens = sorted(tokens)

        def tx(c):
            row = c.execute("SELECT cls, tokens FROM bf_trained WHERE key=?", (key,)).fetchone()
            if row and row[0] == cls:
                return False
            if row:
                c.executemany("UPDATE bf_tokens SET n=MAX(n-1, 0) WHERE token=? AND cls=?",
                              [(t, row[0]) for t in json.loads(row[1] or "[]")])
            c.executemany("""INSERT INTO bf_tokens(token, cls, n) VALUES(?, ?, 1)
                             ON CONFLICT(token, cls) DO UPDATE SET n=n+1""", [(t, cls) for t in tokens])
            c.execute("INSERT OR REPLACE INTO bf_trained VALUES(?, ?, ?, ?)",
                      (key, cls, json.dumps(tokens, ensure_ascii=False), time.time()))
            return True
        return self.db._tx(tx)

    def reset(self):
        def tx(c):
            for t in ("bf_tokens", "bf_trained", "bf_seen"):
                c.execute(f"DELETE FROM {t}")
            c.execute("DELETE FROM meta WHERE key IN ('bayes_bootstrapped', 'bf_boot_folders')")
        self.db._tx(tx)

    def forget_class(self, cls):
        """Zapomene třídu (smazaná složka aplikace)."""
        def tx(c):
            c.execute("DELETE FROM bf_tokens WHERE cls=?", (cls,))
            c.execute("DELETE FROM bf_trained WHERE cls=?", (cls,))
            c.execute("DELETE FROM bf_auto WHERE target=?", (cls,))
        self.db._tx(tx)

    def see(self, key, acc_id, tokens):
        """Zapamatuje si slova nové zprávy (pro pozdější potvrzení, ruční učení a vysvětlení)."""
        self.db._q("INSERT OR REPLACE INTO bf_seen VALUES(?, ?, ?, ?)",
                   (key, acc_id, json.dumps(sorted(tokens), ensure_ascii=False), time.time()))

    def stored_tokens(self, key):
        """Slova zprávy, kterou filtr už viděl nebo se ji naučil (ať se nemusí stahovat znovu)."""
        rows = self.db._q("SELECT tokens FROM bf_seen WHERE key=?", (key,)) or \
            self.db._q("SELECT tokens FROM bf_trained WHERE key=?", (key,))
        return set(json.loads(rows[0][0])) if rows else None

    def tokens_for(self, row):
        """Slova zprávy z databáze bez stahování: dřív viděná, nebo z uloženého těla. Jinak None."""
        key = db_module.message_key(row)
        tokens = self.stored_tokens(key)
        if tokens is None:
            raw = self.db.get_body(row["account_id"], row["folder"], row["uid"])
            tokens = raw_tokens(raw) if raw else None
        return tokens

    def learn(self, M, acc_id, folder, uids, cls) -> int:
        """Naučí zprávy (podle UID ve složce) jako třídu – slova bez stahování, když to jde. Vrací počet."""
        keys, missing = {}, []
        for uid in uids:
            row = self.db.message(acc_id, folder, uid)
            tokens = self.tokens_for(row) if row else None
            if tokens is not None:
                keys[uid] = (db_module.message_key(row), tokens)
            else:
                missing.append(uid)
        if missing and M is not None:
            mail._select(M, folder, readonly=True)
            fetched = fetch_tokens(M, missing)
            recs = {r["uid"]: r for r in mail.fetch_header_records(M, missing)}
            for uid, tokens in fetched.items():
                if uid in recs:
                    keys[uid] = (db_module.message_key(recs[uid]), tokens)
        n = sum(self.train(key, tokens, cls) for key, tokens in keys.values())
        if n:
            log.info("Bayes: naučeno %d zpráv jako %s", n, cls)
        return n

    # ---- posuzování
    def scores(self, tokens, counts=None) -> dict:
        """Jistota každé posuzované třídy (0–1, „tato třída proti ostatním“). Prázdné = filtr není připravený."""
        classes = self.eligible(counts)
        if not classes or not tokens:
            return {}
        per = {}  # slovo -> {třída: počet}
        toks = list(tokens)
        for i in range(0, len(toks), 500):
            chunk = toks[i:i + 500]
            for t, cls, n in self.db._q(f"SELECT token, cls, n FROM bf_tokens WHERE n>0 AND token IN "
                                        f"({','.join('?' * len(chunk))})", chunk):
                if cls in classes:
                    per.setdefault(t, {})[cls] = n
        total = sum(classes.values())
        out = {}
        for cls, n_cls in classes.items():
            probs = []
            for c in per.values():
                n_in = c.get(cls, 0)
                n_out = sum(c.values()) - n_in
                if n_in + n_out:
                    probs.append(token_prob(n_in, n_out, n_cls, total - n_cls))
            probs = sorted((p for p in probs if abs(p - 0.5) >= MIN_DIST),
                           key=lambda p: -abs(p - 0.5))[:MAX_TOKENS]
            out[cls] = combine(probs)
        return out

    def classify(self, tokens, spam_allowed=True):
        """(vítězná třída nebo None, jistoty tříd). Třída vyhraje, když je si jí filtr jistý (spam podle
        citlivosti, ostatní AUTO_THRESHOLD); při více jistých třídách důležitější cíl. Jistá doručená pošta
        = nic nepřesouvat."""
        scores = self.scores(tokens)
        if not scores or scores.get(INBOX, 0) >= AUTO_THRESHOLD:
            return None, scores
        spam_thr = self.threshold()
        cands = [c for c, s in scores.items() if c != INBOX
                 and s >= (spam_thr if c == SPAM else AUTO_THRESHOLD) and (c != SPAM or spam_allowed)]
        if not cands:
            return None, scores
        return min(cands, key=lambda c: (priority(c), -scores[c])), scores

    def suggest(self, tokens):
        """(třída, jistota) pro návrh u zprávy v doručené poště – kategorie nebo složka, ne spam."""
        scores = self.scores(tokens)
        if not scores or scores.get(INBOX, 0) >= AUTO_THRESHOLD:
            return None
        best = max((c for c in scores if c not in (INBOX, SPAM)), key=lambda c: scores[c], default=None)
        return (best, scores[best]) if best and scores[best] >= SUGGEST_THRESHOLD else None

    def explain(self, tokens, cls, n=8):
        """Nejvýraznější slova pro i proti třídě (pro zobrazení u zprávy)."""
        counts = self.counts()
        n_cls, total = counts.get(cls, 0), sum(counts.values())
        if not n_cls or not total - n_cls or not tokens:
            return []
        toks = list(tokens)[:2000]
        per = {}
        for t, c, k in self.db._q(f"SELECT token, cls, n FROM bf_tokens WHERE token IN ({','.join('?' * len(toks))})",
                                  toks):
            per.setdefault(t, {})[c] = k
        scored = []
        for t, c in per.items():
            n_in, n_out = c.get(cls, 0), sum(c.values()) - c.get(cls, 0)
            if n_in + n_out:
                scored.append((token_prob(n_in, n_out, n_cls, total - n_cls), t))
        scored.sort(key=lambda x: -abs(x[0] - 0.5))
        return [{"token": t, "p": round(p, 3)} for p, t in scored[:n]]

    # ---- zprávy zařazené filtrem (do učení se nepočítají, dokud je uživatel nepotvrdí)
    def remember_auto(self, key, target, score):
        self.db._q("INSERT OR REPLACE INTO bf_auto VALUES(?, ?, ?, ?)", (key, target, score, time.time()))

    # ---- potvrzené zprávy
    def _confirmed_class(self, key, auto):
        """Kam zprávu dal uživatel / jeho pravidlo (třída), nebo None (neví se, nebo rozhodl sám filtr)."""
        if key.startswith("h:"):
            rows = self.db._q(f"""SELECT m.*, ({db_module.CATEGORY_EXPLICIT}) AS cat, {db_module.IS_TODO} AS todo,
                                  {db_module.IS_LOCKED} AS locked FROM messages m WHERE {db_module.MSG_KEY}=?""",
                              (key,))
        else:
            rows = self.db._q(f"""SELECT m.*, ({db_module.CATEGORY_EXPLICIT}) AS cat, {db_module.IS_TODO} AS todo,
                                  {db_module.IS_LOCKED} AS locked FROM messages m WHERE m.message_id=?""", (key,))
        if not rows:
            return None, True  # zpráva už není (smazaná, archivovaná) – zapomenout
        folders = {(f["account_id"], f["imap_name"]): f["id"] for f in self.db.user_folders() if f["account_id"]}
        for r in rows:
            if (r["account_id"], r["folder"]) in folders:
                cls = f"folder:{folders[(r['account_id'], r['folder'])]}"
                return (None if auto == cls and not r["locked"] else cls), True
        for r in rows:
            if r["folder"] != "INBOX" or r["todo"]:
                continue
            if r["cat"] in db_module.CATEGORIES:
                return r["cat"], True
            if r["cat"] in ("important", "fyi"):
                return INBOX, True
            if auto in db_module.CATEGORIES:
                return None, True  # kategorii dal filtr a uživatel ji nepotvrdil
            if r["seen"]:
                return INBOX, True
            return None, False  # nepřečtená – počkat
        return None, True

    def confirm(self, now=None):
        """Posouzené zprávy, které po CONFIRM_AFTER leží tam, kam je dal uživatel nebo jeho pravidlo, se naučí."""
        now = now or time.time()
        rows = self.db._q("""SELECT s.key, s.tokens, a.target FROM bf_seen s LEFT JOIN bf_auto a ON a.key=s.key
                             WHERE s.created<? AND s.key NOT IN (SELECT key FROM bf_trained)""",
                          (now - CONFIRM_AFTER,))
        learned = 0
        for key, tokens, auto in rows:
            cls, done = self._confirmed_class(key, auto)
            if cls:
                learned += self.train(key, json.loads(tokens), cls)
            if done and not cls:
                self.db._q("DELETE FROM bf_seen WHERE key=? AND key NOT IN (SELECT key FROM bf_auto)", (key,))
        self.db._q("DELETE FROM bf_seen WHERE created<?", (now - FORGET_AFTER,))
        # zařazení do Nedůležitých / Notifikací se nezapomíná (zpráva by se jinak sama vrátila do Důležitých)
        self.db._q("DELETE FROM bf_auto WHERE created<? AND target NOT IN ('unimportant', 'notifications')",
                   (now - 3 * FORGET_AFTER,))
        return learned

    # ---- první naučení z existujících zpráv
    def needs_bootstrap(self):
        if not self.db._q("SELECT 1 FROM meta WHERE key='bayes_bootstrapped'"):
            return True
        done = set(self.db.meta_get("bf_boot_folders") or [])
        return any(f["id"] not in done for f in self.db.user_folders() if f["account_id"])

    def bootstrap_work(self, accounts):
        """(třída, účet, složka, uid) – zprávy k naučení, nejvýš BOOTSTRAP_LIMIT na třídu (bez už naučených)."""
        trained = {r[0] for r in self.db._q("SELECT key FROM bf_trained")}
        counts = self.counts()
        acc_ids = list(accounts)
        marks = ",".join("?" * len(acc_ids)) or "''"
        work = []

        def take(cls, rows):
            room = BOOTSTRAP_LIMIT - counts.get(cls, 0)
            for r in rows:
                if room <= 0:
                    break
                if r["mkey"] not in trained:
                    work.append((cls, r["account_id"], r["folder"], r["uid"]))
                    trained.add(r["mkey"])
                    room -= 1
        sel = f"SELECT m.account_id, m.folder, m.uid, m.from_addrs, {db_module.MSG_KEY} AS mkey"
        spam_senders = self.db.spam_senders()
        for acc_id, folder in self.db.spam_folders().items():
            if acc_id in accounts and spam_senders:
                take(SPAM, [r for r in self.db._q(f"{sel} FROM messages m WHERE m.account_id=? AND m.folder=? "
                                                   "ORDER BY m.date DESC LIMIT 2000", (acc_id, folder))
                            if any(f",{s}," in (r["from_addrs"] or "") for s in spam_senders)])
        inbox = self.db._q(f"""{sel}, ({db_module.CATEGORY_EXPLICIT}) AS cat, m.seen FROM messages m
                               WHERE m.folder='INBOX' AND m.account_id IN ({marks}) AND NOT {db_module.IS_TODO}
                               ORDER BY m.date DESC LIMIT 5000""", acc_ids)
        for cat in db_module.CATEGORIES:
            take(cat, [r for r in inbox if r["cat"] == cat])
        take(INBOX, [r for r in inbox if r["cat"] in ("important", "fyi")
                     or (r["cat"] is None and r["seen"]
                         and not any(f",{s}," in (r["from_addrs"] or "") for s in spam_senders))])
        for f in self.db.user_folders():
            if f["account_id"] in accounts:
                take(f"folder:{f['id']}", self.db._q(f"{sel} FROM messages m WHERE m.account_id=? AND m.folder=? "
                                                      "ORDER BY m.date DESC LIMIT ?",
                                                      (f["account_id"], f["imap_name"], BOOTSTRAP_LIMIT)))
        return work

    def bootstrap(self, store, pool):
        """Napoprvé (a pro nové složky): naučí se z existujících zpráv. Běží na pozadí."""
        if not self._bootstrap_lock.acquire(blocking=False):
            return
        try:
            accounts = {a["id"]: a for a in store.all()}
            folder_ids = [f["id"] for f in self.db.user_folders() if f["account_id"] in accounts]
            work = self.bootstrap_work(accounts)
            self.bootstrap_state = {"status": "running", "done": 0, "total": len(work)}
            groups = {}
            for cls, acc_id, folder, uid in work:
                groups.setdefault((acc_id, folder, cls), []).append(uid)
            for (acc_id, folder, cls), uids in groups.items():
                for i in range(0, len(uids), 25):
                    chunk = uids[i:i + 25]
                    try:
                        local = [u for u in chunk if (r := self.db.message(acc_id, folder, u))
                                 and self.tokens_for(r) is not None]
                        self.learn(None, acc_id, folder, local, cls)  # bez stahování
                        rest = [u for u in chunk if u not in local]
                        if rest:
                            with pool.get(accounts[acc_id]) as M:
                                self.learn(M, acc_id, folder, rest, cls)
                    except Exception as e:
                        log.warning("Bayes: první učení (%s): %s", acc_id, e)
                    self.bootstrap_state["done"] += len(chunk)
            self.db._q("INSERT OR REPLACE INTO meta VALUES('bayes_bootstrapped', ?)", (str(time.time()),))
            self.db.meta_set("bf_boot_folders", sorted(set(self.db.meta_get("bf_boot_folders") or []) | set(folder_ids)))
            log.info("Bayes: učení z existujících zpráv hotovo – %s", self.counts())
        finally:
            self.bootstrap_state["status"] = "done"
            self._bootstrap_lock.release()
