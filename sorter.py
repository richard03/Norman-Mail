"""Řazení podle podobnosti: vícetřídní naivní Bayes nad hlavičkami zpráv.

Třídy (cíle): "inbox" (zpráva zůstává v Důležitých / Vzít na vědomí), "unimportant", "notifications"
a "folder:<id>" pro každou složku aplikace. Spam řeší samostatný filtr (bayes.py).

Učí se jen z toho, kam zprávy dal uživatel nebo jeho pravidla: kategorie zpráv v doručené poště (ruční
umístění i explicitní pravidla), přečtené zprávy, které zůstaly v doručené poště bez kategorie, a obsah složek
aplikace. Zprávy, které zařadil sám tento filtr (tabulka sorter_auto), se do učení nepočítají, dokud je
uživatel ručně nepotvrdí – jinak by se filtr utvrzoval ve vlastních chybách. Model se nepamatuje zvlášť:
počítá se z aktuálního stavu databáze (nejvýš jednou za REBUILD_AFTER, nebo po změně dat).

Slova (tokeny) jsou jen z hlaviček, které už aplikace má: odesílatel, jeho doména a jméno, slova předmětu,
příjemci a příloha. Nic se kvůli tomu nestahuje ze serveru.
"""

import json
import math
import re
import threading
import time

import db as db_module

MIN_CLASS = 15            # třída s menším počtem naučených zpráv se nenavrhuje
MIN_INBOX = 30            # bez dost zpráv, které zůstávají v doručené poště, filtr nic nedělá
PER_CLASS_LIMIT = 2000    # z každé třídy stačí nejnovější zprávy
AUTO_THRESHOLD = 0.97     # jistota pro automatické zařazení (kategorie, složky se zapnutým řazením)
SUGGEST_THRESHOLD = 0.85  # jistota pro návrh u zprávy
REBUILD_AFTER = 600       # model se přepočítá nejvýš jednou za 10 minut (a jen když se data změnila)
INBOX = "inbox"

WORD_RE = re.compile(r"[^\W_][\w'’-]*", re.UNICODE)
DIGITS_RE = re.compile(r"\d")


def _words(text):
    for w in WORD_RE.findall((text or "").lower()):
        if len(w) >= 2:
            yield DIGITS_RE.sub("0", w)[:30]


def header_tokens(senders, sender_names, to, cc, subject, attachments) -> set:
    """Slova zprávy z hlaviček. senders/to/cc: adresy (malými písmeny), sender_names: jména odesílatelů."""
    out = set()
    for a in senders:
        out.add("from:" + a)
        if "@" in a:
            dom = a.rsplit("@", 1)[1]
            out.add("fromdom:" + dom)
            parts = dom.split(".")
            if len(parts) > 2:
                out.add("fromdom:" + ".".join(parts[-2:]))
            out.add("fromuser:" + a.split("@", 1)[0].split("+", 1)[0])
    for name in sender_names:
        out |= {"fromname:" + w for w in _words(name)}
    out |= {"subj:" + w for w in _words(subject)}
    out |= {"to:" + a for a in to}
    out |= {"cc:" + a for a in cc}
    n = len(to) + len(cc)
    out.add("rcpts:" + ("1" if n <= 1 else "few" if n <= 5 else "many"))
    if attachments:
        out.add("att:1")
    return out


def rec_tokens(rec) -> set:
    """Slova ze záznamu zprávy (z databáze i z hlaviček stažených ze serveru)."""
    addrs = lambda lst: [(a.get("addr") or "").lower() for a in lst or [] if a.get("addr")]
    return header_tokens(addrs(rec.get("from")), [a.get("name") or "" for a in rec.get("from") or []],
                         addrs(rec.get("to")), addrs(rec.get("cc")), rec.get("base_subject") or rec.get("subject"),
                         rec.get("has_attachments"))


def _row_tokens(r):
    split = lambda s: [a for a in (s or "").strip(",").split(",") if a]
    names = [a.get("name") or "" for a in json.loads(r["from_json"] or "[]")]
    return header_tokens(split(r["from_addrs"]), names, split(r["to_addrs"]), split(r["cc_addrs"]),
                         r["base_subject"], r["has_attachments"])


class Sorter:
    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._built_at = 0.0
        self._built_version = None
        self.docs = {}      # třída -> počet naučených zpráv
        self.counts = {}    # třída -> {slovo: počet zpráv třídy se slovem}
        self.known = set()  # slova, která se vyskytla aspoň v jedné třídě

    # ---- model
    def training_rows(self):
        """(třída, řádek zprávy) – zprávy, o kterých rozhodl uživatel nebo jeho pravidla."""
        auto = {r[0] for r in self.db._q(f"SELECT key FROM sorter_auto WHERE key NOT IN "
                                         f"(SELECT key FROM message_marks)")}
        out = []
        rows = self.db._q(f"""SELECT m.*, {db_module.MSG_KEY} AS mkey, ({db_module.CATEGORY_EXPLICIT}) AS cat
                              FROM messages m WHERE m.folder='INBOX' AND NOT {db_module.IS_TODO}
                              ORDER BY m.date DESC LIMIT ?""", (PER_CLASS_LIMIT * 6,))
        per = {}
        for r in rows:
            if r["mkey"] in auto:
                continue
            cat = r["cat"]
            if cat in db_module.CATEGORIES:
                cls = cat
            elif cat in ("important", "fyi") or r["seen"]:  # ručně do doručené, nebo přečtená a ponechaná
                cls = INBOX
            else:
                continue
            if per.get(cls, 0) < PER_CLASS_LIMIT:
                per[cls] = per.get(cls, 0) + 1
                out.append((cls, r))
        for f in self.db.user_folders():
            if not f["account_id"]:
                continue
            for r in self.db._q(f"""SELECT m.*, {db_module.MSG_KEY} AS mkey FROM messages m
                                    WHERE m.account_id=? AND m.folder=? ORDER BY m.date DESC LIMIT ?""",
                                (f["account_id"], f["imap_name"], PER_CLASS_LIMIT)):
                if r["mkey"] not in auto:
                    out.append((f"folder:{f['id']}", r))
        return out

    def rebuild(self):
        docs, counts = {}, {}
        for cls, r in self.training_rows():
            docs[cls] = docs.get(cls, 0) + 1
            c = counts.setdefault(cls, {})
            for t in _row_tokens(r):
                c[t] = c.get(t, 0) + 1
        with self._lock:
            self.docs, self.counts = docs, counts
            self.known = set().union(*counts.values()) if counts else set()
            self._built_at, self._built_version = time.time(), self.db.version

    def _fresh(self):
        stale = time.time() - self._built_at > REBUILD_AFTER and self._built_version != self.db.version
        if not self._built_at or stale:
            self.rebuild()

    def ready(self) -> bool:
        self._fresh()
        return self.docs.get(INBOX, 0) >= MIN_INBOX and any(
            n >= MIN_CLASS for c, n in self.docs.items() if c != INBOX)

    def status(self) -> dict:
        self._fresh()
        return {"ready": self.ready(), "classes": dict(self.docs), "min_class": MIN_CLASS, "min_inbox": MIN_INBOX}

    # ---- posuzování
    def distribution(self, tokens) -> dict:
        """Pravděpodobnost každé třídy (jen třídy s dost zprávami). Prázdné, když filtr není připravený."""
        self._fresh()
        with self._lock:
            docs = {c: n for c, n in self.docs.items() if n >= (MIN_INBOX if c == INBOX else MIN_CLASS)}
            if INBOX not in docs or len(docs) < 2:
                return {}
            total = sum(docs.values())
            toks = [t for t in tokens if t in self.known]
            logp = {}
            for c, n in docs.items():
                cnt = self.counts.get(c, {})
                lp = math.log((n + 1) / (total + len(docs)))
                for t in toks:
                    lp += math.log((cnt.get(t, 0) + 1) / (n + 2))
                logp[c] = lp
        top = max(logp.values())
        exp = {c: math.exp(v - top) for c, v in logp.items()}
        s = sum(exp.values())
        return {c: v / s for c, v in exp.items()}

    def predict(self, rec):
        """(třída, jistota) nejpravděpodobnějšího cíle, pokud to není doručená pošta; jinak None."""
        dist = self.distribution(rec_tokens(rec))
        if not dist:
            return None
        cls = max(dist, key=dist.get)
        return None if cls == INBOX else (cls, dist[cls])

    # ---- zprávy zařazené filtrem (do učení se nepočítají)
    def remember(self, rec, target, score):
        self.db._q("INSERT OR REPLACE INTO sorter_auto VALUES(?, ?, ?, ?)",
                   (db_module.message_key(rec), target, score, time.time()))

    def prune(self, older_than=90 * 86400):
        self.db._q("DELETE FROM sorter_auto WHERE created<?", (time.time() - older_than,))

    def auto_of(self, rec):
        rows = self.db._q("SELECT target, score FROM sorter_auto WHERE key=?", (db_module.message_key(rec),))
        return {"target": rows[0][0], "score": rows[0][1]} if rows else None
