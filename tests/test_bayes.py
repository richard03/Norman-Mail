"""Bayesovský filtr: jeden model pro spam, doručenou poštu, kategorie a složky."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import bayes  # noqa: E402
import db  # noqa: E402


def rec(uid, sender, subject, seen=False, age=0):
    return {"uid": uid, "message_id": f"<{uid}@x>", "subject": subject, "base_subject": subject.lower(),
            "from": [{"name": "", "addr": sender}], "to": [{"name": "", "addr": "me@example.com"}], "cc": [],
            "date": time.time() - age, "seen": seen, "flagged": False, "has_attachments": False, "size": 100 + uid}


# typická slova tříd (hlavičky i text)
WORDS = {
    "spam": ["viagra", "winner", "casino", "url:cheap.biz", "free", "money"],
    "inbox": ["meeting", "tomorrow", "thanks", "project", "from:boss@work.cz", "regards"],
    "notifications": ["order", "shipped", "tracking", "from:noreply@shop.cz", "hdr:list-unsubscribe", "delivery"],
    "folder:1": ["invoice", "amount", "due", "from:billing@energy.cz", "attach:pdf", "payment"],
}


def tokens(cls, i):
    return set(WORDS[cls]) | {f"noise{i}", f"noise{i % 7}"}


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.d = db.Database(Path(tempfile.mkdtemp()) / "test.db")
        self.d.set_my_addresses("a", {"me@example.com"})
        self.f = bayes.BayesFilter(self.d)

    def teach(self):
        for cls in WORDS:
            for i in range(12):
                self.f.train(f"{cls}-{i}", tokens(cls, i), cls)

    def test_not_ready_without_inbox(self):
        for i in range(12):
            self.f.train(f"s{i}", tokens("spam", i), "spam")
        self.assertFalse(self.f.ready())
        self.assertEqual(self.f.classify(tokens("spam", 99)), (None, {}))

    def test_classifies_each_class(self):
        self.teach()
        self.assertTrue(self.f.ready())
        for cls in ("spam", "notifications", "folder:1"):
            got, scores = self.f.classify(tokens(cls, 99))
            self.assertEqual(got, cls, scores)
        self.assertIsNone(self.f.classify(tokens("inbox", 99))[0])  # jistá doručená pošta = nic nepřesouvat
        self.assertIsNone(self.f.classify({"something", "unknown"})[0])  # nejistá zpráva = nic
        self.assertIsNone(self.f.classify(tokens("spam", 99), spam_allowed=False)[0])
        self.assertEqual(self.f.suggest(tokens("folder:1", 99))[0], "folder:1")
        self.assertIsNone(self.f.suggest(tokens("spam", 99)))  # spam se nenavrhuje
        expl = self.f.explain(tokens("spam", 99), "spam")
        self.assertIn("viagra", [e["token"] for e in expl])

    def test_retrain_switches_class(self):
        self.f.train("k", {"a", "b"}, "spam")
        self.assertFalse(self.f.train("k", {"a", "b"}, "spam"))
        self.assertTrue(self.f.train("k", {"a", "b"}, "inbox"))
        self.assertEqual(self.f.counts(), {"inbox": 1})
        self.assertEqual(dict(self.d._q("SELECT cls, n FROM bf_tokens WHERE token='a' AND n>0")), {"inbox": 1})

    def test_priority(self):
        order = sorted(["spam", "notifications", "unimportant", "folder:3", "inbox"], key=bayes.priority)
        self.assertEqual(order, ["inbox", "folder:3", "unimportant", "notifications", "spam"])

    def test_confirm_learns_only_user_decisions(self):
        old = time.time() - bayes.CONFIRM_AFTER - 10
        fid = self.d.create_user_folder("Faktury", "a", "Faktury", False, False)
        self.d.insert_messages("a", "INBOX", [rec(1, "boss@work.cz", "Projekt", seen=True),
                                              rec(2, "noreply@shop.cz", "Objednávka", seen=True),
                                              rec(3, "promo@shop.cz", "Sleva", seen=True),
                                              rec(4, "x@y.cz", "Nepřečtená")])
        self.d.insert_messages("a", "Faktury", [rec(5, "billing@energy.cz", "Faktura")])
        self.d.add_category_rule("notifications", sender="noreply@shop.cz")
        self.f.remember_auto("<3@x>", "unimportant", 0.99)  # rozhodl filtr, uživatel nepotvrdil
        for uid in range(1, 6):
            self.f.see(f"<{uid}@x>", "a", {f"w{uid}"})
        self.d._q("UPDATE bf_seen SET created=?", (old,))
        self.f.confirm()
        trained = dict(self.d._q("SELECT key, cls FROM bf_trained"))
        self.assertEqual(trained, {"<1@x>": "inbox", "<2@x>": "notifications", "<5@x>": f"folder:{fid}"})
        self.assertLessEqual({"<3@x>", "<4@x>"}, {r[0] for r in self.d._q("SELECT key FROM bf_seen")})  # čekají
        # uživatel rozhodnutí filtru potvrdil ručně → naučí se
        self.d.mark_message(self.d.message("a", "INBOX", 3), "unimportant")
        self.f.confirm()
        self.assertEqual(dict(self.d._q("SELECT key, cls FROM bf_trained WHERE key='<3@x>'")),
                         {"<3@x>": "unimportant"})

    def test_auto_placement_view_and_label(self):
        self.d.insert_messages("a", "INBOX", [rec(1, "promo@shop.cz", "Sleva")])
        self.f.remember_auto("<1@x>", "notifications", 0.98)
        self.assertEqual(self.d.view_of("a", "INBOX", 1), "notifications")
        p = self.d.placement(self.d.message("a", "INBOX", 1), "a", "INBOX")
        self.assertEqual((p["layer"], p["cls"]), ("bayes", "notifications"))
        self.d.update_settings({"bayes_auto_notifications": False})  # vypnuto → zpráva zpátky
        self.assertEqual(self.d.view_of("a", "INBOX", 1), "important")

    def test_migration_from_spam_only_filter(self):
        path = Path(tempfile.mkdtemp()) / "old.db"
        d = db.Database(path)
        d._conn.executescript("""
            CREATE TABLE bayes_tokens(token TEXT PRIMARY KEY, spam INTEGER, ham INTEGER);
            CREATE TABLE bayes_trained(key TEXT PRIMARY KEY, is_spam INTEGER, tokens TEXT, created REAL);
            CREATE TABLE bayes_messages(key TEXT PRIMARY KEY, account_id TEXT, message_id TEXT, tokens TEXT,
                                        score REAL, moved INTEGER, created REAL);""")
        d._conn.execute("INSERT INTO bayes_trained VALUES('s', 1, ?, 1)", (json.dumps(["casino", "free"]),))
        d._conn.execute("INSERT INTO bayes_trained VALUES('h', 0, ?, 1)", (json.dumps(["meeting"]),))
        d._conn.execute("INSERT INTO bayes_messages VALUES('m', 'a', 'm', ?, 0.99, 1, 1)", (json.dumps(["x"]),))
        f = bayes.BayesFilter(d)
        self.assertEqual(f.counts(), {"spam": 1, "inbox": 1})
        self.assertEqual(dict(d._q("SELECT token, n FROM bf_tokens WHERE cls='spam'")), {"casino": 1, "free": 1})
        self.assertEqual(d._q("SELECT target FROM bf_auto WHERE key='m'")[0][0], "spam")
        self.assertFalse(d._q("SELECT 1 FROM sqlite_master WHERE name='bayes_trained'"))

    def test_bootstrap_work_covers_classes(self):
        fid = self.d.create_user_folder("Faktury", "a", "Faktury", False, False)
        self.d.insert_messages("a", "INBOX", [rec(1, "boss@work.cz", "Projekt", seen=True),
                                              rec(2, "noreply@shop.cz", "Objednávka", seen=True)])
        self.d.insert_messages("a", "Faktury", [rec(3, "billing@energy.cz", "Faktura")])
        self.d.add_category_rule("notifications", sender="noreply@shop.cz")
        work = {(cls, uid) for cls, _, _, uid in self.f.bootstrap_work({"a": {}})}
        self.assertEqual(work, {("inbox", 1), ("notifications", 2), (f"folder:{fid}", 3)})

    def test_sync_applies_filter(self):
        import sync
        self.teach()
        fid = self.d.create_user_folder("Faktury", "a", "Faktury", False, False)
        self.d._q("UPDATE bf_tokens SET cls=? WHERE cls='folder:1'", (f"folder:{fid}",))
        self.d._q("UPDATE bf_trained SET cls=? WHERE cls='folder:1'", (f"folder:{fid}",))
        m = sync.SyncManager.__new__(sync.SyncManager)
        m.db, m.bayes, moved, spam = self.d, self.f, [], []
        m.move_messages = lambda acc, folder, uids, target, learn=None: moved.extend(uids) or list(uids)
        m._error = lambda acc, e: self.fail(e)
        m._trusted_authserv = lambda acc: None

        class Pool:
            def get(self, acc):
                import contextlib
                return contextlib.nullcontext(None)
        m.pool = Pool()
        import mail
        orig = mail.move_to_spam
        mail.move_to_spam = lambda M, folder, uids: spam.extend(uids)
        try:
            recs = [rec(10, "a@b.cz", "x"), rec(11, "noreply@shop.cz", "y"), rec(12, "billing@energy.cz", "z"),
                    rec(13, "boss@work.cz", "w")]
            toks = {10: tokens("spam", 50), 11: tokens("notifications", 51), 12: tokens("folder:1", 52),
                    13: tokens("inbox", 53)}
            left = m._apply_bayes({"id": "a", "name": "A"}, recs, toks)
            self.assertEqual(spam, [10])
            self.assertEqual([r["uid"] for r in left], [11, 12, 13])  # složka bez „řadit automaticky“ jen navrhuje
            self.assertEqual(self.d._q("SELECT target FROM bf_auto WHERE key='<11@x>'")[0][0], "notifications")
            self.d.update_user_folder(fid, "Faktury", "a", "Faktury", False, False, True)
            left = m._apply_bayes({"id": "a", "name": "A"}, [rec(14, "billing@energy.cz", "z")],
                                  {14: tokens("folder:1", 54)})
            self.assertEqual((left, moved), ([], [14]))
            self.d.update_settings({"bayes_enabled": False})
            left = m._apply_bayes({"id": "a", "name": "A"}, [rec(15, "a@b.cz", "x")], {15: tokens("spam", 55)})
            self.assertEqual(len(left), 1)
        finally:
            mail.move_to_spam = orig


if __name__ == "__main__":
    unittest.main()
