"""Lokální databáze metadat zpráv (SQLite): cache hlaviček a těl, stav synchronizace, odložené zprávy."""

import json
import re
import sqlite3
import threading
import time

from store import DATA_DIR

DB_FILE = DATA_DIR / "mailer.db"
SCHEMA_VERSION = 1
MAX_CACHED_BODY = 25 * 1024 * 1024  # větší zprávy se do cache neukládají

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS folders(
    account_id TEXT, name TEXT, display TEXT, role TEXT, depth INTEGER,
    uidvalidity INTEGER, complete INTEGER DEFAULT 0, last_sync REAL,
    PRIMARY KEY(account_id, name));
CREATE TABLE IF NOT EXISTS messages(
    account_id TEXT, folder TEXT, uid INTEGER,
    message_id TEXT, subject TEXT, base_subject TEXT,
    from_json TEXT, to_json TEXT, cc_json TEXT,
    from_addrs TEXT, to_addrs TEXT, cc_addrs TEXT,   -- ",a@b.cz,c@d.cz," pro rychlé hledání
    date REAL, seen INTEGER, flagged INTEGER, has_attachments INTEGER, size INTEGER,
    PRIMARY KEY(account_id, folder, uid));
CREATE INDEX IF NOT EXISTS ix_messages_date ON messages(account_id, folder, date);
CREATE INDEX IF NOT EXISTS ix_messages_subject ON messages(account_id, base_subject);
CREATE TABLE IF NOT EXISTS bodies(
    account_id TEXT, folder TEXT, uid INTEGER, raw BLOB,
    PRIMARY KEY(account_id, folder, uid));
CREATE TABLE IF NOT EXISTS snoozes(
    account_id TEXT, folder TEXT, uid INTEGER, message_id TEXT, until REAL,
    PRIMARY KEY(account_id, folder, uid));
CREATE TABLE IF NOT EXISTS spam_rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, value TEXT NOT NULL,
    created REAL, hits INTEGER DEFAULT 0, UNIQUE(kind, value));
CREATE TABLE IF NOT EXISTS user_folders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, imap_name TEXT NOT NULL,
    after_read INTEGER DEFAULT 0, mark_read INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS user_folder_rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT, folder_id INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
    created REAL, hits INTEGER DEFAULT 0, after_read INTEGER DEFAULT 0, UNIQUE(kind, value));
CREATE TABLE IF NOT EXISTS my_addresses(
    account_id TEXT, email TEXT, PRIMARY KEY(account_id, email));
CREATE TABLE IF NOT EXISTS notify_rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, value TEXT NOT NULL,
    created REAL, UNIQUE(kind, value));
CREATE TABLE IF NOT EXISTS unimportant_rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, value TEXT NOT NULL,
    created REAL, UNIQUE(kind, value));
CREATE TABLE IF NOT EXISTS message_marks(
    key TEXT PRIMARY KEY, category TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS category_rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '', recipient TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
    created REAL, UNIQUE(category, sender, recipient, subject));
CREATE TABLE IF NOT EXISTS contacts(
    email TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    sent INTEGER NOT NULL DEFAULT 0,       -- kolikrát jsem této adrese psal
    received INTEGER NOT NULL DEFAULT 0,   -- kolikrát se adresa objevila ve zprávě, kterou jsem dostal
    last REAL NOT NULL DEFAULT 0,          -- datum poslední takové zprávy
    rank_sent REAL NOT NULL DEFAULT 0,     -- pořadí v našeptávání: součet vah zpráv, které jsem adrese psal,
    rank_received REAL NOT NULL DEFAULT 0);  -- a které jsem od ní dostal (viz _contact_weight, contacts)
"""

# Kategorie doručené pošty, které určuje uživatel: pravidla nebo označení jedné zprávy.
# Pravidlo má až tři podmínky (odesílatel, příjemce v Komu / Kopii, část předmětu); zpráva musí splnit všechny
# vyplněné. (Dřívější tabulky notify_rules / unimportant_rules se jednou převedly do category_rules.)
CATEGORIES = ("notifications", "unimportant")
CATEGORY_RULE_FIELDS = ("sender", "recipient", "subject")
CATEGORY_RULE_MATCH = ("(r.sender='' OR instr(m.from_addrs, ',' || r.sender || ',') > 0)"
                       " AND (r.recipient='' OR instr(m.to_addrs, ',' || r.recipient || ',') > 0"
                       "      OR instr(m.cc_addrs, ',' || r.recipient || ',') > 0)"
                       " AND (r.subject='' OR instr(m.base_subject, r.subject) > 0)")
# Klíč zprávy pro označení „jen tento email“ – Message-ID (přežije přesun), jinak umístění zprávy.
MSG_KEY = "COALESCE(NULLIF(m.message_id, ''), m.account_id || '|' || m.folder || '|' || m.uid)"


def message_key(rec) -> str:
    return rec.get("message_id") or f"{rec['account_id']}|{rec['folder']}|{rec['uid']}"


def _category_rule(cat):
    return f"EXISTS (SELECT 1 FROM category_rules r WHERE r.category='{cat}' AND {CATEGORY_RULE_MATCH})"


def category_rule_matches(rule, rec) -> bool:
    """Odpovídá zpráva pravidlu Nedůležitých / Notifikací? Stejná logika jako CATEGORY_RULE_MATCH v SQL."""
    if rule["sender"] and not any((a.get("addr") or "").lower() == rule["sender"] for a in rec["from"]):
        return False
    if rule["recipient"] and not any((a.get("addr") or "").lower() == rule["recipient"]
                                     for a in rec.get("to", []) + rec.get("cc", [])):
        return False
    return not rule["subject"] or rule["subject"] in (rec.get("base_subject") or "")


# Kategorie zprávy (jediný výraz, vyhodnotí se jednou): označení jedné zprávy má přednost před pravidly;
# když zprávě odpovídají pravidla obou kategorií, vyhrávají Notifikace. NULL = žádná kategorie.
CATEGORY = (f"COALESCE((SELECT k.category FROM message_marks k WHERE k.key={MSG_KEY}), "
            f"CASE WHEN {_category_rule('notifications')} THEN 'notifications' "
            f"WHEN {_category_rule('unimportant')} THEN 'unimportant' END)")
IS_NOTIFICATION = f"({CATEGORY}) IS 'notifications'"
IS_UNIMPORTANT = f"({CATEGORY}) IS 'unimportant'"
CATEGORY_FILTERS = {"notifications": IS_NOTIFICATION, "unimportant": IS_UNIMPORTANT}
# Zpráva je přímo pro mě, pokud je některá z mých adres (daného účtu) v poli Komu.
IS_DIRECT = ("EXISTS (SELECT 1 FROM my_addresses a WHERE a.account_id=m.account_id "
             "AND instr(m.to_addrs, ',' || a.email || ',') > 0)")
VIEW_FILTERS = {
    "important": f"({CATEGORY}) IS NULL AND {IS_DIRECT}",
    "fyi": f"({CATEGORY}) IS NULL AND NOT {IS_DIRECT}",  # Vzít na vědomí: jsem v kopii / skryté kopii
    "unimportant": IS_UNIMPORTANT,
    "notifications": IS_NOTIFICATION,
    "all": "1",
}

# Typy pravidel složek a jejich podmínka v SQL (r = pravidlo, m = zpráva)
RULE_KINDS = ("sender", "recipient", "subject")
RULE_MATCH = ("((r.kind='sender' AND instr(m.from_addrs, ',' || r.value || ',') > 0)"
              " OR (r.kind='recipient' AND (instr(m.to_addrs, ',' || r.value || ',') > 0"
              "                             OR instr(m.cc_addrs, ',' || r.value || ',') > 0))"
              " OR (r.kind='subject' AND instr(m.base_subject, r.value) > 0))")

WEEK = 7 * 24 * 3600
MONTH = 30 * 24 * 3600
DEFAULT_SETTINGS = {
    "notify_mark_read_on_leave": False,         # po opuštění Notifikací označit všechny za přečtené
    "notify_delete_read_after_week": False,     # mazat přečtené notifikace starší než týden
    "notify_delete_read_after_month": False,    # … starší než měsíc
    "notify_delete_unread_after_week": False,   # mazat nepřečtené notifikace starší než týden
    "notify_delete_unread_after_month": False,  # … starší než měsíc
    "unimp_mark_read_on_leave": False,          # totéž pro Nedůležité
    "unimp_delete_read_after_week": False,
    "unimp_delete_read_after_month": False,
    "unimp_delete_unread_after_week": False,
    "unimp_delete_unread_after_month": False,
    "spam_delete_after_week": False,            # mazat zprávy ve spamu starší než týden
    "spam_delete_after_month": False,           # … starší než měsíc
    "bayes_enabled": True,                    # bayesovský filtr přesouvá rozpoznaný spam
    "bayes_sensitivity": "medium",            # high / medium / low
}
SETTING_CHOICES = {"bayes_sensitivity": ("high", "medium", "low")}


def delete_age(settings, prefix, seen=None):
    """Stáří (s), od kterého se zprávy podle nastavení mažou; None = nemažou se.
    prefix: notify / unimp / spam; seen: přečtené / nepřečtené (None = bez rozlišení, Spam)."""
    part = f"{prefix}_delete" + ("" if seen is None else "_read" if seen else "_unread")
    if settings.get(f"{part}_after_week"):
        return WEEK
    if settings.get(f"{part}_after_month"):
        return MONTH
    return None


def rule_matches(rule, rec) -> bool:
    """Odpovídá zpráva (záznam z hlaviček) pravidlu složky? Stejná logika jako RULE_MATCH v SQL."""
    v = rule["value"]
    if rule["kind"] == "sender":
        return any(a["addr"].lower() == v for a in rec["from"])
    if rule["kind"] == "recipient":
        return any(a["addr"].lower() == v for a in rec["to"] + rec["cc"])
    return v in (rec.get("base_subject") or "")


# Adresy, které se do našeptávání nehodí (automatické odesílatele)
NO_REPLY_RE = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|bounces?)([+._-]|@)",
                         re.IGNORECASE)
MAX_CONTACTS = 5000
# Pořadí v našeptávání: každá zpráva přispěje váhou, která s jejím stářím exponenciálně klesá (poločas
# RANK_HALF_LIFE) – nahoře jsou adresy, které se používají často a v poslední době, staré klesají.
# Váha se ukládá vztažená k pevnému datu RANK_EPOCH: všechny kontakty stárnou stejně rychle, takže pořadí
# podle uloženého součtu je stejné jako podle aktuálního skóre a nic se nemusí přepočítávat.
RANK_HALF_LIFE = 90 * 86400
RANK_EPOCH = 1577836800      # 1. 1. 2020
RANK_SENT_WEIGHT = 5         # zpráva, kterou jsem adrese napsal, váží víc než zpráva, kterou jsem od ní dostal
RANK_STRANGER = 0.1          # pošta od adres, kterým jsem nikdy nepsal (newslettery, automaty), váží jen zlomek


def _contact_weight(date) -> float:
    date = min(date or 0, time.time()) or RANK_EPOCH - 20 * 365 * 86400  # bez data = hodně stará
    return 2.0 ** ((date - RANK_EPOCH) / RANK_HALF_LIFE)


def _addrs(lst) -> str:
    return "," + ",".join(a["addr"].lower() for a in lst if a.get("addr")) + ","


class Database:
    def __init__(self, path=DB_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.execute("INSERT OR IGNORE INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
        # migrace: uživatelské složky míří na konkrétní účet (starší složky bez účtu je třeba dokonfigurovat)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(user_folders)")}
        if "account_id" not in cols:
            self._conn.execute("ALTER TABLE user_folders ADD COLUMN account_id TEXT")
        # migrace: „ihned / až po přečtení“ má každé pravidlo zvlášť (dřív celá složka) – převzít nastavení složky
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(user_folder_rules)")}
        if "after_read" not in cols:
            self._conn.execute("ALTER TABLE user_folder_rules ADD COLUMN after_read INTEGER DEFAULT 0")
            self._conn.execute("UPDATE user_folder_rules SET after_read=COALESCE("
                               "(SELECT f.after_read FROM user_folders f WHERE f.id=user_folder_rules.folder_id), 0)")
        # migrace: pravidla Notifikací / Nedůležitých s více podmínkami (dřív jen odesílatel NEBO předmět)
        if not self._conn.execute("SELECT 1 FROM meta WHERE key='category_rules_migrated'").fetchone():
            for cat, table in (("notifications", "notify_rules"), ("unimportant", "unimportant_rules")):
                self._conn.execute(f"""INSERT OR IGNORE INTO category_rules(category, sender, subject, created)
                    SELECT ?, CASE kind WHEN 'sender' THEN value ELSE '' END,
                              CASE kind WHEN 'subject' THEN value ELSE '' END, created FROM {table}""", (cat,))
            self._conn.execute("INSERT INTO meta VALUES('category_rules_migrated', '1')")
        # migrace: pořadí kontaktů podle četnosti i stáří zpráv – kontakty se znovu naplní ze zpráv
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(contacts)")}
        if "rank_sent" not in cols:
            self._conn.execute("ALTER TABLE contacts ADD COLUMN rank_sent REAL NOT NULL DEFAULT 0")
            self._conn.execute("ALTER TABLE contacts ADD COLUMN rank_received REAL NOT NULL DEFAULT 0")
            self._conn.execute("DELETE FROM contacts")
            self._conn.execute("DELETE FROM meta WHERE key='contacts_built'")
        self.version = 0  # zvyšuje se při každé změně viditelné v UI

    def bump(self):
        with self._lock:
            self.version += 1

    def _q(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _tx(self, fn):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                result = fn(self._conn)
                self._conn.execute("COMMIT")
                return result
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ složky

    def upsert_folders(self, acc_id, folders):
        names = [f["name"] for f in folders]

        def run(c):
            for f in folders:
                c.execute("""INSERT INTO folders(account_id, name, display, role, depth) VALUES(?,?,?,?,?)
                             ON CONFLICT(account_id, name) DO UPDATE SET display=excluded.display,
                             role=excluded.role, depth=excluded.depth""",
                          (acc_id, f["name"], f["display"], f["role"], f["depth"]))
            gone = [r[0] for r in c.execute("SELECT name FROM folders WHERE account_id=?", (acc_id,))
                    if r[0] not in names]
            for name in gone:
                self._clear_folder(c, acc_id, name)
                c.execute("DELETE FROM folders WHERE account_id=? AND name=?", (acc_id, name))
        self._tx(run)

    def folders(self, acc_id):
        return [dict(r) for r in self._q("SELECT * FROM folders WHERE account_id=?", (acc_id,))]

    def folder(self, acc_id, name):
        rows = self._q("SELECT * FROM folders WHERE account_id=? AND name=?", (acc_id, name))
        return dict(rows[0]) if rows else None

    def set_folder_state(self, acc_id, name, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO folders(account_id, name) VALUES(?,?)", (acc_id, name))
            self._conn.execute(f"UPDATE folders SET {cols} WHERE account_id=? AND name=?",
                               (*fields.values(), acc_id, name))

    @staticmethod
    def _clear_folder(c, acc_id, name):
        for table in ("messages", "bodies", "snoozes"):
            c.execute(f"DELETE FROM {table} WHERE account_id=? AND folder=?", (acc_id, name))

    def clear_folder(self, acc_id, name):
        self._tx(lambda c: self._clear_folder(c, acc_id, name))

    def delete_account(self, acc_id):
        def run(c):
            for table in ("folders", "messages", "bodies", "snoozes", "my_addresses"):
                c.execute(f"DELETE FROM {table} WHERE account_id=?", (acc_id,))
        self._tx(run)
        self.bump()

    # ------------------------------------------------------------------ zprávy

    def cached_flags(self, acc_id, folder) -> dict:
        return {r["uid"]: (bool(r["seen"]), bool(r["flagged"])) for r in
                self._q("SELECT uid, seen, flagged FROM messages WHERE account_id=? AND folder=?", (acc_id, folder))}

    def insert_messages(self, acc_id, folder, records):
        rows = [(acc_id, folder, r["uid"], r["message_id"], r["subject"], r["base_subject"],
                 json.dumps(r["from"], ensure_ascii=False), json.dumps(r["to"], ensure_ascii=False),
                 json.dumps(r["cc"], ensure_ascii=False), _addrs(r["from"]), _addrs(r["to"]), _addrs(r["cc"]),
                 r["date"], int(r["seen"]), int(r["flagged"]), int(r["has_attachments"]), r["size"])
                for r in records if not r.get("deleted")]
        def run(c):
            new = self._new_records(c, acc_id, folder, records)
            c.executemany("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            if new and self._contacts_built(c):
                self._learn_contacts(c, acc_id, folder, new)
        self._tx(run)

    @staticmethod
    def _new_records(c, acc_id, folder, records) -> list:
        """Záznamy, které v cache ještě nejsou (aby se kontakty nepočítaly dvakrát)."""
        have = {r[0] for r in c.execute("SELECT uid FROM messages WHERE account_id=? AND folder=?", (acc_id, folder))}
        return [r for r in records if not r.get("deleted") and r["uid"] not in have]

    def delete_uids(self, acc_id, folder, uids):
        uids = list(uids)

        def run(c):
            for table in ("messages", "bodies", "snoozes"):
                c.executemany(f"DELETE FROM {table} WHERE account_id=? AND folder=? AND uid=?",
                              [(acc_id, folder, u) for u in uids])
        self._tx(run)

    def update_flags(self, acc_id, folder, changes):
        """changes: [(uid, seen, flagged)]"""
        self._tx(lambda c: c.executemany(
            "UPDATE messages SET seen=?, flagged=? WHERE account_id=? AND folder=? AND uid=?",
            [(int(s), int(f), acc_id, folder, u) for u, s, f in changes]))

    def set_flag(self, acc_id, folder, uid, column, value):
        assert column in ("seen", "flagged")
        self._q(f"UPDATE messages SET {column}=? WHERE account_id=? AND folder=? AND uid=?",
                (int(value), acc_id, folder, uid))

    def remove_message(self, acc_id, folder, uid):
        self.delete_uids(acc_id, folder, [uid])

    @staticmethod
    def _to_dict(r) -> dict:
        return {
            "account_id": r["account_id"], "folder": r["folder"], "uid": r["uid"],
            "message_id": r["message_id"], "subject": r["subject"], "base_subject": r["base_subject"],
            "from": json.loads(r["from_json"]), "to": json.loads(r["to_json"]), "cc": json.loads(r["cc_json"]),
            "date": r["date"], "seen": bool(r["seen"]), "flagged": bool(r["flagged"]),
            "has_attachments": bool(r["has_attachments"]), "size": r["size"],
            "snoozed_until": r["snoozed_until"] if "snoozed_until" in r.keys() else None,
            "notification": "category" in r.keys() and r["category"] == "notifications",
            "unimportant": "category" in r.keys() and r["category"] == "unimportant",
        }

    _CATS = f"{CATEGORY} AS category"
    _SELECT = f"""SELECT m.*, s.until AS snoozed_until, {_CATS} FROM messages m
                 LEFT JOIN snoozes s ON s.account_id=m.account_id AND s.folder=m.folder AND s.uid=m.uid"""

    def message(self, acc_id, folder, uid):
        rows = self._q(self._SELECT + " WHERE m.account_id=? AND m.folder=? AND m.uid=?", (acc_id, folder, uid))
        return self._to_dict(rows[0]) if rows else None

    # ------------------------------------------------------------------ hledání ve veškeré poště

    # Kde zprávu v aplikaci uvidíte: složka aplikace, pohled doručené pošty, Spam, Smazané, jinak název složky.
    # Priorita pro odstranění duplicit (Gmail: zpráva je v doručené poště i ve „Všech zprávách“).
    _LABELED = f"""SELECT m.*, s.until AS snoozed_until, {_CATS},
            CASE WHEN u.name IS NOT NULL THEN u.name
                 WHEN m.folder='INBOX' THEN CASE {CATEGORY} WHEN 'notifications' THEN 'Notifikace'
                                                 WHEN 'unimportant' THEN 'Nedůležité'
                                                 ELSE CASE WHEN {IS_DIRECT} THEN 'Důležité' ELSE 'Vzít na vědomí' END END
                 WHEN f.role='trash' THEN 'Smazané' WHEN f.role='junk' THEN 'Spam'
                 ELSE COALESCE(f.display, m.folder) END AS folder_label,
            CASE WHEN m.folder='INBOX' OR u.name IS NOT NULL THEN 0 WHEN f.role='all' THEN 2 ELSE 1 END AS prio
        FROM messages m
        LEFT JOIN snoozes s ON s.account_id=m.account_id AND s.folder=m.folder AND s.uid=m.uid
        LEFT JOIN folders f ON f.account_id=m.account_id AND f.name=m.folder
        LEFT JOIN user_folders u ON u.account_id=m.account_id AND u.imap_name=m.folder"""

    def _labeled(self, rows):
        out = []
        for r in rows:
            d = self._to_dict(r)
            d["folder_label"], d["prio"] = r["folder_label"], r["prio"]
            out.append(d)
        return out

    @staticmethod
    def dedupe(messages):
        """Stejná zpráva ve více složkách (podle Message-ID) jen jednou – tam, kde ji uživatel vidí."""
        best = {}
        for m in messages:
            k = (m["account_id"], m.get("message_id") or f"{m['folder']}|{m['uid']}")
            if k not in best or m.get("prio", 1) < best[k].get("prio", 1):
                best[k] = m
        return sorted(best.values(), key=lambda m: (m["date"] or 0, m["uid"]), reverse=True)

    def search_all(self, term, acc_ids, limit=300, sender=None, recipient=None, subject=None, attachments=None):
        """Hledání ve všech uložených zprávách všech účtů. term = kdekoli v předmětu, odesílateli
        nebo příjemcích; pokročilé hledání: sender / recipient (Komu, Kopie) / subject = část textu,
        attachments = True (má přílohu) / False (nemá) / None (nezáleží). Všechna zadaná kritéria
        musí platit zároveň. Vrací (zprávy, zda jich je víc)."""
        if not acc_ids:
            return [], False
        conds, params = [], []
        if term:
            conds.append("(m.subject LIKE ? OR m.from_json LIKE ? OR m.to_json LIKE ? OR m.cc_json LIKE ?)")
            params += [f"%{term}%"] * 4
        if sender:
            conds.append("m.from_json LIKE ?")
            params.append(f"%{sender}%")
        if recipient:
            conds.append("(m.to_json LIKE ? OR m.cc_json LIKE ?)")
            params += [f"%{recipient}%"] * 2
        if subject:
            conds.append("m.subject LIKE ?")
            params.append(f"%{subject}%")
        if not conds:  # samotné „má / nemá přílohu“ by vrátilo půl schránky – hledá se jen s textem
            return [], False
        if attachments is not None:
            conds.append("m.has_attachments=?")
            params.append(int(attachments))
        rows = self._q(f"""{self._LABELED}
            WHERE m.account_id IN ({','.join('?' * len(acc_ids))}) AND {' AND '.join(conds)}
            ORDER BY m.date DESC, m.uid DESC LIMIT ?""", (*acc_ids, *params, limit * 2 + 1))
        found = self.dedupe(self._labeled(rows))
        return found[:limit], len(rows) > limit * 2 or len(found) > limit

    def messages_by_uids(self, acc_id, folder, uids) -> dict:
        """{uid: zpráva s folder_label} uložených zpráv ze seznamu UID (výsledky hledání na serveru)."""
        out = {}
        uids = list(uids)
        for i in range(0, len(uids), 500):
            chunk = uids[i:i + 500]
            rows = self._q(f"{self._LABELED} WHERE m.account_id=? AND m.folder=? AND m.uid IN ({','.join('?' * len(chunk))})",
                           (acc_id, folder, *chunk))
            out.update({d["uid"]: d for d in self._labeled(rows)})
        return out

    def folder_label(self, acc_id, folder) -> str:
        """Popisek složky pro zprávu, která ještě není v databázi."""
        u = self._q("SELECT name FROM user_folders WHERE account_id=? AND imap_name=?", (acc_id, folder))
        if u:
            return u[0][0]
        if folder == "INBOX":
            return "Doručená pošta"
        f = self.folder(acc_id, folder) or {}
        return {"trash": "Smazané", "junk": "Spam"}.get(f.get("role"), f.get("display") or folder)

    def list_messages(self, acc_ids, folder, page, page_size, view="all", offset=None, search=None,
                      unread=None) -> dict:
        """Stránka seznamu. offset (místo page) = počet už zobrazených zpráv – pak stránkování
        nic nepřeskočí, ani když mezitím nějaké zprávy zmizely. unread = (keep,) → jen nepřečtené;
        keep = (účet, složka, uid) zprávy, která v seznamu zůstane i přečtená (právě otevřená), nebo None."""
        if offset is None:
            offset = page * page_size
        if isinstance(folder, dict) and not folder:
            return {"messages": [], "total": 0, "has_more": False}
        where, params = self._list_where(acc_ids, folder, view)
        if unread is not None:
            keep = unread[0]
            if keep:
                where += " AND (m.seen=0 OR (m.account_id=? AND m.folder=? AND m.uid=?))"
                params += list(keep)
            else:
                where += " AND m.seen=0"
        if search:
            where += " AND (m.subject LIKE ? OR m.from_json LIKE ? OR m.to_json LIKE ?)"
            params += [f"%{search}%"] * 3
        total = self._q(f"SELECT COUNT(*) FROM messages m {where}", params)[0][0]
        rows = self._q(f"{self._SELECT} {where} ORDER BY m.date DESC, m.uid DESC LIMIT ? OFFSET ?",
                       (*params, page_size, offset))
        return {"messages": [self._to_dict(r) for r in rows], "total": total,
                "has_more": offset + page_size < total}

    @staticmethod
    def _list_where(acc_ids, folder, view="all"):
        """Podmínka seznamu: účty + složka, nebo {account_id: složka} (např. koše všech účtů)."""
        if isinstance(folder, dict):
            where = "WHERE (" + " OR ".join(["(m.account_id=? AND m.folder=?)"] * len(folder)) + ")"
            params = [x for pair in folder.items() for x in pair]
        else:
            where = f"WHERE m.account_id IN ({','.join('?' * len(acc_ids))}) AND m.folder=?"
            params = [*acc_ids, folder]
        return where + f" AND {VIEW_FILTERS[view]}", params

    def list_position(self, acc_ids, folder, view, acc_id, msg_folder, uid):
        """Pořadí zprávy (od 0) v seznamu se stejným řazením jako list_messages; None = v seznamu není."""
        if isinstance(folder, dict) and not folder:
            return None
        where, params = self._list_where(acc_ids, folder, view)
        rows = self._q(f"""SELECT pos FROM (
                SELECT m.account_id, m.folder, m.uid,
                       ROW_NUMBER() OVER (ORDER BY m.date DESC, m.uid DESC) - 1 AS pos
                FROM messages m {where})
            WHERE account_id=? AND folder=? AND uid=?""", (*params, acc_id, msg_folder, uid))
        return rows[0][0] if rows else None

    # pohled doručené pošty, do kterého zpráva patří
    _VIEW = f"COALESCE({CATEGORY}, CASE WHEN {IS_DIRECT} THEN 'important' ELSE 'fyi' END)"

    def view_of(self, acc_id, folder, uid):
        """Pohled sloučené doručené pošty (important / fyi / unimportant / notifications), kam zpráva patří."""
        rows = self._q(f"SELECT {self._VIEW} FROM messages m WHERE m.account_id=? AND m.folder=? AND m.uid=?",
                       (acc_id, folder, uid))
        return rows[0][0] if rows else "all"

    def view_unread(self, folder="INBOX") -> dict:
        """Počty nepřečtených pro pohledy doručené pošty (jedním průchodem)."""
        out = {view: 0 for view in VIEW_FILTERS}
        for view, n in self._q(f"SELECT {self._VIEW} AS v, COUNT(*) FROM messages m WHERE m.folder=? AND m.seen=0 "
                               "GROUP BY v", (folder,)):
            out[view] = n
        out["all"] = sum(n for v, n in out.items() if v != "all")
        return out

    # ------------------------------------------------------------------ koš („Smazané“)

    def role_folders(self, role) -> dict:
        """{account_id: název složky s danou rolí} (trash = koš, junk = spam)"""
        return {r[0]: r[1] for r in self._q("SELECT account_id, name FROM folders WHERE role=?", (role,))}

    def role_count(self, role, acc_ids, unread=False) -> int:
        """Počet (nepřečtených) zpráv ve složkách dané role u daných účtů."""
        acc_ids = list(acc_ids)
        if not acc_ids:
            return 0
        seen = "AND m.seen=0" if unread else ""
        return self._q(f"""SELECT COUNT(*) FROM messages m JOIN folders f
                           ON f.account_id=m.account_id AND f.name=m.folder AND f.role=?
                           WHERE m.account_id IN ({",".join("?" * len(acc_ids))}) {seen}""",
                       (role, *acc_ids))[0][0]

    def trash_folders(self) -> dict:
        """{account_id: název koše}"""
        return self.role_folders("trash")

    def trash_total(self, acc_ids) -> int:
        """Počet zpráv v koších daných účtů (číslo u složky Smazané)."""
        return self.role_count("trash", acc_ids)

    def sent_folders(self) -> dict:
        """{account_id: název složky Odeslané}"""
        return self.role_folders("sent")

    def spam_folders(self) -> dict:
        """{account_id: název složky Spam}"""
        return self.role_folders("junk")

    def role_uids_older(self, role, older_than) -> dict:
        """{account_id: (složka, [uid])} zpráv ve složkách dané role starších než older_than (unix ts)."""
        out = {}
        for acc_id, folder, uid in self._q("""SELECT m.account_id, m.folder, m.uid FROM messages m JOIN folders f
                ON f.account_id=m.account_id AND f.name=m.folder AND f.role=?
                WHERE m.date IS NOT NULL AND m.date < ?""", (role, older_than)):
            out.setdefault(acc_id, (folder, []))[1].append(uid)
        return out

    def unseen_uids(self, acc_id, folder) -> list:
        return [r[0] for r in self._q("SELECT uid FROM messages WHERE account_id=? AND folder=? AND seen=0",
                                      (acc_id, folder))]

    def inbox_from_senders(self, senders) -> list:
        """Zprávy v doručené poště od daných odesílatelů: [{account_id, uid, seen, date, snoozed}]."""
        out = []
        for s in senders:
            out += [dict(r) for r in self._q("""
                SELECT m.account_id, m.uid, m.seen, m.date, s.uid IS NOT NULL AS snoozed FROM messages m
                LEFT JOIN snoozes s ON s.account_id=m.account_id AND s.folder=m.folder AND s.uid=m.uid
                WHERE m.folder='INBOX' AND instr(m.from_addrs, ?) > 0""", (f",{s},",))]
        return out

    def unread_counts(self, folder="INBOX") -> dict:
        return {r[0]: r[1] for r in self._q(
            "SELECT account_id, SUM(1 - seen) FROM messages WHERE folder=? GROUP BY account_id", (folder,))}

    # ------------------------------------------------------------------ těla zpráv

    def get_body(self, acc_id, folder, uid):
        rows = self._q("SELECT raw FROM bodies WHERE account_id=? AND folder=? AND uid=?", (acc_id, folder, uid))
        return bytes(rows[0][0]) if rows else None

    def put_body(self, acc_id, folder, uid, raw):
        if len(raw) <= MAX_CACHED_BODY:
            self._q("INSERT OR REPLACE INTO bodies VALUES(?,?,?,?)", (acc_id, folder, uid, raw))

    # ------------------------------------------------------------------ odložení

    def add_snooze(self, acc_id, folder, uid, message_id, until):
        self._q("INSERT OR REPLACE INTO snoozes VALUES(?,?,?,?,?)", (acc_id, folder, uid, message_id, until))

    def remove_snooze(self, acc_id, folder, uid):
        self._q("DELETE FROM snoozes WHERE account_id=? AND folder=? AND uid=?", (acc_id, folder, uid))

    def due_snoozes(self, now=None):
        return [dict(r) for r in self._q("SELECT * FROM snoozes WHERE until<=? ORDER BY until",
                                         (now or time.time(),))]

    # ------------------------------------------------------------------ pravidla pro spam

    def add_spam_rule(self, sender):
        """Pravidlo „odesílatel je spam“ (platí pro všechny účty). Vrací id pravidla."""
        sender = sender.lower()
        self._q("INSERT OR IGNORE INTO spam_rules(kind, value, created) VALUES('sender', ?, ?)", (sender, time.time()))
        return self._q("SELECT id FROM spam_rules WHERE kind='sender' AND value=?", (sender,))[0][0]

    def spam_rules(self):
        return [dict(r) for r in self._q("SELECT * FROM spam_rules ORDER BY created DESC")]

    RULE_TABLES = ("spam_rules", "user_folder_rules")

    def update_rule(self, table, rule_id, kind, value, after_read=None) -> bool:
        """Úprava pravidla (typ, hodnota, u složek i kdy přesouvat). False = takové pravidlo už existuje."""
        assert table in self.RULE_TABLES
        sets, params = ["kind=?", "value=?"], [kind, value]
        if after_read is not None:
            sets.append("after_read=?")
            params.append(int(after_read))
        try:
            self._q(f"UPDATE {table} SET {', '.join(sets)} WHERE id=?", (*params, rule_id))
        except sqlite3.IntegrityError:
            return False
        return True

    def delete_spam_rule(self, rule_id):
        self._q("DELETE FROM spam_rules WHERE id=?", (rule_id,))

    def safe_senders(self, senders) -> set:
        """Odesílatelé, které bayesovský filtr nikdy nepřesune: vlastní adresy, zdroje notifikací
        a lidé, kterým jsem někdy psal (jsou v Komu/Kopie odeslané pošty)."""
        senders = {s.lower() for s in senders if s}
        own = {r[0].lower() for r in self._q("SELECT email FROM my_addresses")}
        safe = senders & (own | self.category_senders())
        for s in senders - safe:
            if self._q("""SELECT 1 FROM messages m JOIN folders f ON f.account_id=m.account_id AND f.name=m.folder
                          AND f.role='sent' WHERE instr(m.to_addrs, ?) > 0 OR instr(m.cc_addrs, ?) > 0 LIMIT 1""",
                       (f",{s},", f",{s},")):
                safe.add(s)
        return safe

    def remove_spam_senders(self, senders) -> int:
        """Smaže pravidla „odesílatel je spam“ (zpráva vrácená ze spamu). Vrací počet smazaných."""
        senders = sorted({s.lower() for s in senders if s})
        if not senders:
            return 0
        n = len(self.spam_senders() & set(senders))
        self._q(f"DELETE FROM spam_rules WHERE kind='sender' AND value IN ({','.join('?' * len(senders))})", senders)
        return n

    def spam_senders(self) -> set:
        return {r[0] for r in self._q("SELECT value FROM spam_rules WHERE kind='sender'")}

    def add_rule_hits(self, sender, count=1):
        self._q("UPDATE spam_rules SET hits=hits+? WHERE kind='sender' AND value=?", (count, sender.lower()))

    # ------------------------------------------------------------------ nastavení

    def settings(self) -> dict:
        stored = {r[0][len("setting:"):]: json.loads(r[1])
                  for r in self._q("SELECT key, value FROM meta WHERE key LIKE 'setting:%'")}
        return {k: stored.get(k, v) for k, v in DEFAULT_SETTINGS.items()}

    def update_settings(self, changes: dict) -> dict:
        for k, v in changes.items():
            if k in SETTING_CHOICES and v not in SETTING_CHOICES[k]:
                continue
            if k in DEFAULT_SETTINGS and isinstance(v, type(DEFAULT_SETTINGS[k])):
                self._q("INSERT OR REPLACE INTO meta VALUES(?, ?)", ("setting:" + k, json.dumps(v)))
        return self.settings()

    # ------------------------------------------------------------------ hromadné operace s Notifikacemi / Nedůležitými

    def category_uids(self, category, seen=None, older_than=None) -> dict:
        """{account_id: [uid, ...]} zpráv kategorie v doručené poště. Odložené zprávy se vynechávají."""
        sql = f"""SELECT m.account_id, m.uid FROM messages m
                  LEFT JOIN snoozes s ON s.account_id=m.account_id AND s.folder=m.folder AND s.uid=m.uid
                  WHERE m.folder='INBOX' AND {CATEGORY_FILTERS[category]} AND s.uid IS NULL"""
        params = []
        if seen is not None:
            sql += " AND m.seen=?"
            params.append(int(seen))
        if older_than is not None:
            sql += " AND m.date IS NOT NULL AND m.date < ?"
            params.append(older_than)
        out = {}
        for acc_id, uid in self._q(sql, params):
            out.setdefault(acc_id, []).append(uid)
        return out

    def unseen_in(self, folder="INBOX", view="all", acc_id=None) -> dict:
        """{account_id: [uid]} nepřečtených zpráv ve složce (u doručené pošty volitelně jen v pohledu)."""
        sql = f"SELECT m.account_id, m.uid FROM messages m WHERE m.folder=? AND m.seen=0 AND {VIEW_FILTERS[view]}"
        params = [folder]
        if acc_id:
            sql += " AND m.account_id=?"
            params.append(acc_id)
        out = {}
        for a, uid in self._q(sql, params):
            out.setdefault(a, []).append(uid)
        return out

    def set_seen_bulk(self, acc_id, folder, uids):
        self._tx(lambda c: c.executemany("UPDATE messages SET seen=1 WHERE account_id=? AND folder=? AND uid=?",
                                         [(acc_id, folder, u) for u in uids]))

    # ------------------------------------------------------------------ uživatelské složky

    def user_folders(self):
        """Složky s počtem pravidel a nepřečtených (v cílové složce cílového účtu)."""
        return [dict(r) for r in self._q("""
            SELECT f.*,
                   (SELECT COUNT(*) FROM user_folder_rules r WHERE r.folder_id=f.id) AS rules,
                   (SELECT COUNT(*) FROM messages m WHERE m.account_id=f.account_id AND m.folder=f.imap_name) AS total,
                   (SELECT COALESCE(SUM(1 - seen), 0) FROM messages m
                     WHERE m.account_id=f.account_id AND m.folder=f.imap_name) AS unread
            FROM user_folders f ORDER BY f.name COLLATE NOCASE""")]

    def user_folder(self, folder_id):
        rows = self._q("SELECT * FROM user_folders WHERE id=?", (folder_id,))
        return dict(rows[0]) if rows else None

    def user_folder_names(self, acc_id) -> set:
        """Názvy cílových složek na daném účtu (synchronizují se vždy)."""
        return {r[0] for r in self._q("SELECT imap_name FROM user_folders WHERE account_id=?", (acc_id,))}

    def create_user_folder(self, name, account_id, imap_name, after_read, mark_read):
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO user_folders(name, account_id, imap_name, after_read, mark_read, created)
                   VALUES(?,?,?,?,?,?)""",
                (name, account_id, imap_name, int(after_read), int(mark_read), time.time()))
            return cur.lastrowid

    def update_user_folder(self, folder_id, name, account_id, imap_name, after_read, mark_read):
        self._q("UPDATE user_folders SET name=?, account_id=?, imap_name=?, after_read=?, mark_read=? WHERE id=?",
                (name, account_id, imap_name, int(after_read), int(mark_read), folder_id))

    def delete_user_folder(self, folder_id):
        def run(c):
            c.execute("DELETE FROM user_folder_rules WHERE folder_id=?", (folder_id,))
            c.execute("DELETE FROM user_folders WHERE id=?", (folder_id,))
        self._tx(run)

    def folder_rules(self, folder_id):
        return [dict(r) for r in self._q(
            "SELECT * FROM user_folder_rules WHERE folder_id=? ORDER BY created DESC", (folder_id,))]

    def add_folder_rule(self, folder_id, kind, value, after_read=False):
        """Pravidlo složky (odesílatel / příjemce / slovo v předmětu), přesun ihned nebo až po přečtení.
        Stejné pravidlo může patřit jen do jedné složky (při přidání do jiné se přesune)."""
        assert kind in RULE_KINDS
        value = " ".join(value.lower().split())
        self._q("""INSERT INTO user_folder_rules(folder_id, kind, value, created, after_read) VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(kind, value) DO UPDATE SET folder_id=excluded.folder_id, created=excluded.created,
                                                          after_read=excluded.after_read""",
                (folder_id, kind, value, time.time(), int(after_read)))
        return self._q("SELECT id FROM user_folder_rules WHERE kind=? AND value=?", (kind, value))[0][0]

    def delete_folder_rule(self, rule_id):
        self._q("DELETE FROM user_folder_rules WHERE id=?", (rule_id,))

    def add_folder_rule_hits(self, rule_id, count):
        self._q("UPDATE user_folder_rules SET hits=hits+? WHERE id=?", (count, rule_id))

    def folder_rules_active(self) -> list:
        """Všechna pravidla složek, které mají cílový účet, i s nastavením složky."""
        return [dict(r) for r in self._q("""
            SELECT r.id AS rule_id, r.kind, r.value, r.after_read, f.id, f.name, f.account_id, f.imap_name, f.mark_read
            FROM user_folder_rules r JOIN user_folders f ON f.id=r.folder_id
            WHERE f.account_id IS NOT NULL""")]

    def folder_rule_matches(self, folder_id) -> dict:
        """{account_id: {uid: id pravidla}} zpráv v doručené poště, které pravidla složky přesouvají
        (pravidla „až po přečtení“ jen přečtené; odložené se vynechávají)."""
        sql = f"""SELECT m.account_id, m.uid, r.id FROM messages m
                  JOIN user_folder_rules r ON r.folder_id=? AND {RULE_MATCH} AND (r.after_read=0 OR m.seen=1)
                  LEFT JOIN snoozes s ON s.account_id=m.account_id AND s.folder=m.folder AND s.uid=m.uid
                  WHERE m.folder='INBOX' AND s.uid IS NULL"""
        out = {}
        for acc_id, uid, rule_id in self._q(sql, (folder_id,)):
            out.setdefault(acc_id, {}).setdefault(uid, rule_id)
        return out

    # ------------------------------------------------------------------ kontakty (našeptávání adres)

    @staticmethod
    def _contacts_built(c) -> bool:
        return c.execute("SELECT 1 FROM meta WHERE key='contacts_built'").fetchone() is not None

    @staticmethod
    def _learn_contacts(c, acc_id, folder, records, mine=None, by_me=False):
        """Přičte adresy ze zpráv do kontaktů. Zpráva ode mě (z mé adresy nebo z Odeslaných) = psal jsem
        jejím příjemcům; jinak = adresy z ní (odesílatel i ostatní příjemci) jsem dostal. Spam se přeskakuje."""
        role = (c.execute("SELECT role FROM folders WHERE account_id=? AND name=?", (acc_id, folder)).fetchone()
                or [None])[0]
        if role == "junk":
            return
        if mine is None:
            mine = {r[0] for r in c.execute("SELECT email FROM my_addresses")}
        for r in records:
            frm = [a for a in r.get("from") or [] if a.get("addr")]
            sent = by_me or role == "sent" or any(a["addr"].lower() in mine for a in frm)
            people = (r.get("to") or []) + (r.get("cc") or []) + ([] if sent else frm)
            col = "sent" if sent else "received"
            seen = set()
            for a in people:
                addr = (a.get("addr") or "").strip().lower()
                if "@" not in addr or addr in mine or addr in seen or len(addr) > 254:
                    continue
                seen.add(addr)
                name = (a.get("name") or "").strip().strip("\"'")
                if name.lower() == addr:
                    name = ""
                date = r.get("date") or 0
                c.execute(f"""INSERT INTO contacts(email, name, {col}, last, rank_{col}) VALUES(?, ?, 1, ?, ?)
                              ON CONFLICT(email) DO UPDATE SET {col}={col}+1,
                              rank_{col}=contacts.rank_{col}+excluded.rank_{col},
                              name=CASE WHEN excluded.name<>'' AND (contacts.name='' OR excluded.last>=contacts.last)
                                        THEN excluded.name ELSE contacts.name END,
                              last=MAX(contacts.last, excluded.last)""",
                          (addr, name[:200], date, _contact_weight(date)))

    def _build_contacts(self):
        """Jednorázové naplnění kontaktů ze zpráv, které už jsou v cache."""
        def run(c):
            if self._contacts_built(c):
                return
            mine = {r[0] for r in c.execute("SELECT email FROM my_addresses")}
            groups = {}
            for r in c.execute("SELECT account_id, folder, from_json, to_json, cc_json, date FROM messages"):
                groups.setdefault((r[0], r[1]), []).append(
                    {"from": json.loads(r[2] or "[]"), "to": json.loads(r[3] or "[]"),
                     "cc": json.loads(r[4] or "[]"), "date": r[5]})
            for (acc_id, folder), recs in groups.items():
                self._learn_contacts(c, acc_id, folder, recs, mine)
            c.execute("INSERT INTO meta VALUES('contacts_built', '1')")
        self._tx(run)

    def add_sent_contacts(self, addrs):
        """Adresáti právě odeslané zprávy: [(jméno, adresa)] – do našeptávání hned, ne až po synchronizaci."""
        self._build_contacts()
        rec = {"from": [], "to": [{"name": n, "addr": a} for n, a in addrs], "date": time.time()}
        self._tx(lambda c: self._learn_contacts(c, None, None, [rec], by_me=True))

    def contacts(self) -> list:
        """Kontakty pro našeptávání: nahoře adresy používané často a v poslední době (komu píšu váží víc).
        Bez automatických odesílatelů, spammerů a zdrojů Notifikací / Nedůležitých (pokud jsem jim nepsal)."""
        self._build_contacts()
        skip = self.spam_senders() | self.category_senders()
        rows = self._q("SELECT email, name, sent, received FROM contacts "
                       f"ORDER BY rank_sent * {RANK_SENT_WEIGHT} + rank_received * "
                       f"CASE WHEN sent > 0 THEN 1 ELSE {RANK_STRANGER} END DESC, last DESC")
        out = []
        for r in rows:
            if not r["sent"] and (r["email"] in skip or NO_REPLY_RE.match(r["email"])):
                continue
            out.append({"email": r["email"], "name": r["name"]})
            if len(out) >= MAX_CONTACTS:
                break
        return out

    # ------------------------------------------------------------------ moje adresy (pro „Vzít na vědomí“)

    def set_my_addresses(self, acc_id, emails):
        emails = {e.lower() for e in emails if e and "@" in e}

        def run(c):
            c.execute("DELETE FROM my_addresses WHERE account_id=?", (acc_id,))
            c.executemany("INSERT INTO my_addresses VALUES(?, ?)", [(acc_id, e) for e in emails])
        self._tx(run)

    def my_addresses(self) -> dict:
        out = {}
        for acc_id, email in self._q("SELECT account_id, email FROM my_addresses"):
            out.setdefault(acc_id, set()).add(email)
        return out

    def folder_unread(self) -> dict:
        return {r[0]: r[1] for r in self._q("""
            SELECT f.id, (SELECT COALESCE(SUM(1 - seen), 0) FROM messages m
                           WHERE m.account_id=f.account_id AND m.folder=f.imap_name)
            FROM user_folders f""")}

    # ------------------------------------------------------------------ Notifikace / Nedůležité: pravidla a označení

    def add_category_rule(self, category, sender="", recipient="", subject=""):
        """Pravidlo kategorie (vyplněné podmínky musí platit všechny). Stejné pravidlo může být v obou
        kategoriích – pak vyhrávají Notifikace (viz CATEGORY); po smazání jednoho z nich platí to druhé."""
        assert category in CATEGORIES and (sender or recipient or subject)
        self._q("""INSERT OR IGNORE INTO category_rules(category, sender, recipient, subject, created)
                   VALUES(?, ?, ?, ?, ?)""", (category, sender.lower(), recipient.lower(), subject.lower(), time.time()))

    def update_category_rule(self, category, rule_id, sender, recipient, subject) -> bool:
        """Úprava pravidla. False = stejné pravidlo už v kategorii existuje."""
        try:
            self._q("UPDATE category_rules SET sender=?, recipient=?, subject=? WHERE id=? AND category=?",
                    (sender, recipient, subject, rule_id, category))
        except sqlite3.IntegrityError:
            return False
        return True

    def remove_category_sender(self, category, sender):
        """Smaže pravidla „jen odesílatel“ (bez dalších podmínek)."""
        self._q("DELETE FROM category_rules WHERE category=? AND sender=? AND recipient='' AND subject=''",
                (category, sender.lower()))

    def delete_category_rule(self, category, rule_id):
        self._q("DELETE FROM category_rules WHERE id=? AND category=?", (rule_id, category))

    def category_senders(self, category=None) -> set:
        """Odesílatelé z pravidel „jen odesílatel“ (category=None: obou kategorií)."""
        sql = "SELECT sender FROM category_rules WHERE sender<>'' AND recipient='' AND subject=''"
        if category:
            return {r[0] for r in self._q(sql + " AND category=?", (category,))}
        return {r[0] for r in self._q(sql)}

    def category_rules(self, category):
        """Pravidla s počtem zpráv, které jim v doručené poště odpovídají (jeden průchod doručenou poštou;
        pravidla „jen odesílatel“ se hledají ve slovníku)."""
        rules = [dict(r, messages=0) for r in self._q(
            "SELECT * FROM category_rules WHERE category=? ORDER BY created DESC", (category,))]
        by_sender, other = {}, []
        for r in rules:
            if r["sender"] and not r["recipient"] and not r["subject"]:
                by_sender[r["sender"]] = r
            else:
                other.append(r)
        for frm, to, cc, subj in self._q(
                "SELECT from_addrs, to_addrs, cc_addrs, base_subject FROM messages WHERE folder='INBOX'"):
            for a in frm.strip(",").split(","):
                if a in by_sender:
                    by_sender[a]["messages"] += 1
            for r in other:
                if ((not r["sender"] or f",{r['sender']}," in frm)
                        and (not r["recipient"] or f",{r['recipient']}," in to or f",{r['recipient']}," in cc)
                        and (not r["subject"] or r["subject"] in (subj or ""))):
                    r["messages"] += 1
        return rules

    def mark_message(self, rec, category):
        """„Jen tento email“: zpráva patří do kategorie bez ohledu na pravidla (vždy jen do jedné)."""
        assert category in CATEGORIES
        self._q("INSERT OR REPLACE INTO message_marks VALUES(?, ?, ?)", (message_key(rec), category, time.time()))

    def unmark_message(self, rec, category=None):
        if category:
            self._q("DELETE FROM message_marks WHERE key=? AND category=?", (message_key(rec), category))
        else:
            self._q("DELETE FROM message_marks WHERE key=?", (message_key(rec),))

    def uncategorize(self, rec, category):
        """Zpráva přestane patřit do kategorie: zruší se její označení i pravidla, která jí odpovídají
        – ta platila i pro ostatní zprávy."""
        self.unmark_message(rec, category)
        ids = [r["id"] for r in self._q("SELECT * FROM category_rules WHERE category=?", (category,))
               if category_rule_matches(r, rec)]
        if ids:
            self._q(f"DELETE FROM category_rules WHERE id IN ({','.join('?' * len(ids))})", ids)

    def categorizer(self):
        """Funkce zpráva → "notifications" / "unimportant" / None (stejná logika jako IS_NOTIFICATION
        a IS_UNIMPORTANT v SQL) – pro zprávy, které nejsou v databázi."""
        marks = dict(self._q("SELECT key, category FROM message_marks"))
        rules = {cat: self._q("SELECT * FROM category_rules WHERE category=?", (cat,)) for cat in CATEGORIES}

        def category(rec):
            mark = marks.get(message_key(rec))
            if mark:
                return mark
            return next((cat for cat in CATEGORIES if any(category_rule_matches(r, rec) for r in rules[cat])), None)
        return category

    # ------------------------------------------------------------------ kontext

    def related(self, acc_id, folders, me, counterpart, base, exclude_mid, limit=100) -> list:
        """Související zprávy čistě z databáze (stejná pravidla jako mail.find_related)."""
        me, cp = me.lower(), (counterpart or "").lower()
        roles = {f["name"]: f for f in folders}
        marks = ",".join("?" * len(roles))
        conds, params = [], []
        if cp:
            conds += ["m.from_addrs LIKE ?", "m.cc_addrs LIKE ?", "(m.from_addrs LIKE ? AND m.to_addrs LIKE ?)"]
            params += [f"%,{cp},%", f"%,{cp},%", f"%,{me},%", f"%,{cp},%"]
        if len(base) >= 3:
            conds.append("m.base_subject = ?")
            params.append(base)
        if not conds or not roles:
            return []
        rows = self._q(f"""{self._SELECT} WHERE m.account_id=? AND m.folder IN ({marks}) AND ({' OR '.join(conds)})
                           ORDER BY m.date DESC LIMIT ?""", (acc_id, *roles, *params, limit * 3))
        found, by_mid = [], {}
        for r in rows:
            m = self._to_dict(r)
            reasons = []
            if cp and f",{cp}," in r["from_addrs"]:
                reasons.append("sender")
            if cp and f",{me}," in r["from_addrs"] and f",{cp}," in r["to_addrs"]:
                reasons.append("mine")
            if cp and f",{cp}," in r["cc_addrs"]:
                reasons.append("cc")
            if len(base) >= 3 and r["base_subject"] == base:
                reasons.append("subject")
            mid = m["message_id"]
            if not reasons or (mid and mid == exclude_mid):
                continue
            f = roles[m["folder"]]
            m.update(reasons=reasons, folder_display=f["display"], folder_role=f["role"])
            if mid and mid in by_mid:  # duplicita (Gmail) – preferujeme konkrétní složku před „Všechny zprávy“
                prev = by_mid[mid]
                if prev["folder_role"] == "all" and f["role"] != "all":
                    found[found.index(prev)] = m
                    by_mid[mid] = m
                continue
            if mid:
                by_mid[mid] = m
            found.append(m)
        return found[:limit]
