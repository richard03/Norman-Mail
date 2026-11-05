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
CREATE TABLE IF NOT EXISTS notify_rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, value TEXT NOT NULL,
    created REAL, UNIQUE(kind, value));
"""

# Zpráva je notifikace, pokud její odesílatel odpovídá některému pravidlu (přesná shoda adresy).
IS_NOTIFICATION = ("EXISTS (SELECT 1 FROM notify_rules r WHERE r.kind='sender' "
                   "AND instr(m.from_addrs, ',' || r.value || ',') > 0)")
VIEW_FILTERS = {"important": f"NOT {IS_NOTIFICATION}", "notifications": IS_NOTIFICATION, "all": "1"}

WEEK = 7 * 24 * 3600
DEFAULT_SETTINGS = {
    "notify_mark_read_on_leave": False,       # po opuštění Notifikací označit všechny za přečtené
    "notify_delete_read_after_week": True,    # mazat přečtené notifikace starší než týden
    "notify_delete_unread_after_week": False,  # mazat nepřečtené notifikace starší než týden
}


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
            for table in ("folders", "messages", "bodies", "snoozes"):
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

    def list_messages(self, acc_ids, folder, page, page_size, view="all") -> dict:
        marks = ",".join("?" * len(acc_ids))
        where = f"WHERE m.account_id IN ({marks}) AND m.folder=? AND {VIEW_FILTERS[view]}"
        total = self._q(f"SELECT COUNT(*) FROM messages m {where}", (*acc_ids, folder))[0][0]
        rows = self._q(f"{self._SELECT} {where} ORDER BY m.date DESC, m.uid DESC LIMIT ? OFFSET ?",
                       (*acc_ids, folder, page_size, page * page_size))
        return {"messages": [self._to_dict(r) for r in rows], "total": total,
                "has_more": (page + 1) * page_size < total}

    def view_unread(self, folder="INBOX") -> dict:
        """Počty nepřečtených pro pohledy Důležité / Notifikace / Všechny doručené."""
        return {view: self._q(f"SELECT COALESCE(SUM(1 - seen), 0) FROM messages m WHERE m.folder=? AND {cond}",
                              (folder,))[0][0] for view, cond in VIEW_FILTERS.items()}

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

    def set_seen_bulk(self, acc_id, folder, uids):
        self._tx(lambda c: c.executemany("UPDATE messages SET seen=1 WHERE account_id=? AND folder=? AND uid=?",
                                         [(acc_id, folder, u) for u in uids]))

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
