"""Lokální databáze metadat zpráv (SQLite): cache hlaviček a těl, stav synchronizace, odložené zprávy."""

import json
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
"""

# Zpráva je notifikace, pokud její odesílatel odpovídá některému pravidlu (přesná shoda adresy).
IS_NOTIFICATION = ("EXISTS (SELECT 1 FROM notify_rules r WHERE r.kind='sender' "
                   "AND instr(m.from_addrs, ',' || r.value || ',') > 0)")
# Zpráva je přímo pro mě, pokud je některá z mých adres (daného účtu) v poli Komu.
IS_DIRECT = ("EXISTS (SELECT 1 FROM my_addresses a WHERE a.account_id=m.account_id "
             "AND instr(m.to_addrs, ',' || a.email || ',') > 0)")
VIEW_FILTERS = {
    "important": f"NOT {IS_NOTIFICATION} AND {IS_DIRECT}",
    "fyi": f"NOT {IS_NOTIFICATION} AND NOT {IS_DIRECT}",   # Vzít na vědomí: jsem v kopii / skryté kopii
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
DEFAULT_SETTINGS = {
    "notify_mark_read_on_leave": False,       # po opuštění Notifikací označit všechny za přečtené
    "notify_delete_read_after_week": True,    # mazat přečtené notifikace starší než týden
    "notify_delete_unread_after_week": False,  # mazat nepřečtené notifikace starší než týden
    "bayes_enabled": True,                    # bayesovský filtr přesouvá rozpoznaný spam
    "bayes_sensitivity": "medium",            # high / medium / low
}
SETTING_CHOICES = {"bayes_sensitivity": ("high", "medium", "low")}


def rule_matches(rule, rec) -> bool:
    """Odpovídá zpráva (záznam z hlaviček) pravidlu složky? Stejná logika jako RULE_MATCH v SQL."""
    v = rule["value"]
    if rule["kind"] == "sender":
        return any(a["addr"].lower() == v for a in rec["from"])
    if rule["kind"] == "recipient":
        return any(a["addr"].lower() == v for a in rec["to"] + rec["cc"])
    return v in (rec.get("base_subject") or "")


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
        self._tx(lambda c: c.executemany(
            "INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows))

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
            "notification": bool(r["is_notification"]) if "is_notification" in r.keys() else False,
        }

    _SELECT = f"""SELECT m.*, s.until AS snoozed_until, {IS_NOTIFICATION} AS is_notification FROM messages m
                 LEFT JOIN snoozes s ON s.account_id=m.account_id AND s.folder=m.folder AND s.uid=m.uid"""

    def message(self, acc_id, folder, uid):
        rows = self._q(self._SELECT + " WHERE m.account_id=? AND m.folder=? AND m.uid=?", (acc_id, folder, uid))
        return self._to_dict(rows[0]) if rows else None

    def list_messages(self, acc_ids, folder, page, page_size, view="all", offset=None, search=None) -> dict:
        """Stránka seznamu. offset (místo page) = počet už zobrazených zpráv – pak stránkování
        nic nepřeskočí, ani když mezitím nějaké zprávy zmizely."""
        if offset is None:
            offset = page * page_size
        if isinstance(folder, dict) and not folder:
            return {"messages": [], "total": 0, "has_more": False}
        where, params = self._list_where(acc_ids, folder, view)
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

    def view_of(self, acc_id, folder, uid):
        """Pohled sloučené doručené pošty (important / fyi / notifications), do kterého zpráva patří."""
        for view in ("notifications", "important", "fyi"):
            if self._q(f"SELECT 1 FROM messages m WHERE m.account_id=? AND m.folder=? AND m.uid=? AND {VIEW_FILTERS[view]}",
                       (acc_id, folder, uid)):
                return view
        return "all"

    def view_unread(self, folder="INBOX") -> dict:
        """Počty nepřečtených pro pohledy Důležité / Notifikace / Všechny doručené."""
        return {view: self._q(f"SELECT COALESCE(SUM(1 - seen), 0) FROM messages m WHERE m.folder=? AND {cond}",
                              (folder,))[0][0] for view, cond in VIEW_FILTERS.items()}

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

    def spam_folders(self) -> dict:
        """{account_id: název složky Spam}"""
        return self.role_folders("junk")

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

    def delete_spam_rule(self, rule_id):
        self._q("DELETE FROM spam_rules WHERE id=?", (rule_id,))

    def safe_senders(self, senders) -> set:
        """Odesílatelé, které bayesovský filtr nikdy nepřesune: vlastní adresy, zdroje notifikací
        a lidé, kterým jsem někdy psal (jsou v Komu/Kopie odeslané pošty)."""
        senders = {s.lower() for s in senders if s}
        own = {r[0].lower() for r in self._q("SELECT email FROM my_addresses")}
        safe = senders & (own | self.notify_senders())
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

    # ------------------------------------------------------------------ hromadné operace s notifikacemi

    def notification_uids(self, seen=None, older_than=None) -> dict:
        """{account_id: [uid, ...]} notifikací v doručené poště. Odložené zprávy se vynechávají."""
        sql = f"""SELECT m.account_id, m.uid FROM messages m
                  LEFT JOIN snoozes s ON s.account_id=m.account_id AND s.folder=m.folder AND s.uid=m.uid
                  WHERE m.folder='INBOX' AND {IS_NOTIFICATION} AND s.uid IS NULL"""
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

    # ------------------------------------------------------------------ pravidla pro notifikace

    def add_notify_rule(self, sender):
        sender = sender.lower()
        self._q("INSERT OR IGNORE INTO notify_rules(kind, value, created) VALUES('sender', ?, ?)", (sender, time.time()))
        return self._q("SELECT id FROM notify_rules WHERE kind='sender' AND value=?", (sender,))[0][0]

    def remove_notify_sender(self, sender):
        self._q("DELETE FROM notify_rules WHERE kind='sender' AND value=?", (sender.lower(),))

    def delete_notify_rule(self, rule_id):
        self._q("DELETE FROM notify_rules WHERE id=?", (rule_id,))

    def notify_senders(self) -> set:
        return {r[0] for r in self._q("SELECT value FROM notify_rules WHERE kind='sender'")}

    def notify_rules(self):
        """Pravidla s počtem zpráv, které jim v doručené poště odpovídají."""
        return [dict(r) for r in self._q("""
            SELECT r.*, (SELECT COUNT(*) FROM messages m WHERE m.folder='INBOX'
                         AND instr(m.from_addrs, ',' || r.value || ',') > 0) AS messages
            FROM notify_rules r ORDER BY r.created DESC""")]

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
