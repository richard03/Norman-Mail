"""Filtr podobnosti: učí se z ručního umístění, pravidel a složek, ne ze svých vlastních rozhodnutí."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import db  # noqa: E402
import sorter  # noqa: E402


def rec(uid, sender, subject, seen=True, to="me@example.com"):
    return {"uid": uid, "message_id": f"<{uid}@x>", "subject": subject, "base_subject": subject.lower(),
            "from": [{"name": "", "addr": sender}], "to": [{"name": "", "addr": to}], "cc": [],
            "date": time.time() - uid, "seen": seen, "flagged": False, "has_attachments": False, "size": 100 + uid}


class SorterTest(unittest.TestCase):
    def setUp(self):
        self.d = db.Database(Path(tempfile.mkdtemp()) / "test.db")
        self.d.set_my_addresses("a", {"me@example.com"})
        people = [rec(i, f"person{i}@friends.cz", f"Schůzka {i}") for i in range(1, 41)]
        news = [rec(100 + i, "news@shop.cz", f"Akce týdne {i}") for i in range(20)]
        self.d.insert_messages("a", "INBOX", people + news)
        for i in range(20):  # uživatel je ručně dal do Notifikací
            self.d.mark_message(self.d.message("a", "INBOX", 100 + i), "notifications")
        self.fid = self.d.create_user_folder("Faktury", "a", "Faktury", False, False)
        self.d.insert_messages("a", "Faktury", [rec(200 + i, "billing@energy.cz", f"Faktura {i}") for i in range(20)])
        self.s = sorter.Sorter(self.d)

    def test_predicts_category_and_folder(self):
        self.assertTrue(self.s.ready())
        cls, p = self.s.predict(rec(300, "news@shop.cz", "Nová kolekce"))
        self.assertEqual(cls, "notifications")
        self.assertGreater(p, sorter.AUTO_THRESHOLD)
        cls, p = self.s.predict(rec(301, "billing@energy.cz", "Vyúčtování"))
        self.assertEqual(cls, f"folder:{self.fid}")
        self.assertIsNone(self.s.predict(rec(302, "person7@friends.cz", "Oběd")))  # zůstává v doručené

    def test_not_ready_without_enough_data(self):
        d = db.Database(Path(tempfile.mkdtemp()) / "test.db")
        d.insert_messages("a", "INBOX", [rec(1, "x@y.cz", "Ahoj")])
        self.assertFalse(sorter.Sorter(d).ready())
        self.assertIsNone(sorter.Sorter(d).predict(rec(2, "x@y.cz", "Ahoj")))

    def test_auto_category_view_and_no_self_training(self):
        new = rec(400, "promo@shop.cz", "Sleva", seen=False)
        self.d.insert_messages("a", "INBOX", [new])
        self.s.remember(new, "notifications", 0.99)
        self.assertEqual(self.d.view_of("a", "INBOX", 400), "notifications")
        p = self.d.placement(self.d.message("a", "INBOX", 400), "a", "INBOX")
        self.assertEqual((p["layer"], p["target"]), ("bayes", "notifications"))
        keys = {r["mkey"] for _, r in self.s.training_rows()}
        self.assertNotIn(db.message_key(new), keys)  # vlastní rozhodnutí se neučí
        self.d.mark_message(self.d.message("a", "INBOX", 400), "important")  # ruční oprava má přednost
        self.assertEqual(self.d.view_of("a", "INBOX", 400), "important")
        self.assertIn(db.message_key(new), {r["mkey"] for _, r in self.s.training_rows()})

    def test_rule_beats_auto_and_disabling_setting_clears(self):
        new = rec(401, "promo@shop.cz", "Sleva", seen=False)
        self.d.insert_messages("a", "INBOX", [new])
        self.s.remember(new, "notifications", 0.99)
        self.d.add_category_rule("unimportant", sender="promo@shop.cz")
        self.assertEqual(self.d.view_of("a", "INBOX", 401), "unimportant")
        self.d.delete_rules([r for r in self.d.matching_rules(new) if r["type"] == "category"])
        self.assertEqual(self.d.view_of("a", "INBOX", 401), "notifications")
        self.d.update_settings({"sorter_notifications": False})
        self.assertEqual(self.d.view_of("a", "INBOX", 401), "important")


    def test_sync_files_new_messages(self):
        import sync
        m = sync.SyncManager.__new__(sync.SyncManager)
        m.db, m.sorter, moved = self.d, self.s, []
        m.move_messages = lambda acc, folder, uids, target: moved.extend(uids) or list(uids)
        m._error = lambda acc, e: self.fail(e)
        recs = [rec(500, "news@shop.cz", "Novinky", seen=False), rec(501, "billing@energy.cz", "Faktura", seen=False),
                rec(502, "person3@friends.cz", "Ahoj", seen=False)]
        left = m._apply_sorter({"id": "a", "name": "A"}, recs)
        self.assertEqual([r["uid"] for r in left], [500, 501, 502])  # složka bez „řadit automaticky“ jen navrhuje
        self.assertEqual(self.s.auto_of(recs[0])["target"], "notifications")
        self.assertIsNone(self.s.auto_of(recs[2]))
        self.d.update_user_folder(self.fid, "Faktury", "a", "Faktury", False, False, True)
        left = m._apply_sorter({"id": "a", "name": "A"}, [rec(503, "billing@energy.cz", "Faktura 2", seen=False)])
        self.assertEqual((left, moved), ([], [503]))
        self.d.update_settings({"sorter_notifications": False})
        m._apply_sorter({"id": "a", "name": "A"}, [rec(504, "news@shop.cz", "Novinky 2", seen=False)])
        self.assertIsNone(self.s.auto_of(rec(504, "news@shop.cz", "Novinky 2")))


if __name__ == "__main__":
    unittest.main()
