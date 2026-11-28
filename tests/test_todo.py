"""ToDo: zpráva označená k vyřízení je jen v pohledu ToDo a po „Vyřešeno“ se vrátí do svého pohledu."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import db  # noqa: E402


def rec(uid, to="me@example.com", subject="Ahoj", seen=False, message_id=None):
    return {"uid": uid, "message_id": message_id or f"<{uid}@example.com>", "subject": subject,
            "base_subject": subject.lower(), "from": [{"name": "Eva", "addr": "eva@example.com"}],
            "to": [{"name": "", "addr": to}], "cc": [], "date": time.time() - uid, "seen": seen,
            "flagged": False, "has_attachments": False, "size": 100}


class TodoTest(unittest.TestCase):
    def setUp(self):
        self.d = db.Database(Path(tempfile.mkdtemp()) / "test.db")
        self.d.set_my_addresses("a", {"me@example.com"})
        self.d.insert_messages("a", "INBOX", [rec(1), rec(2, to="other@example.com"), rec(3, seen=True)])
        self.msg = lambda uid: self.d.message("a", "INBOX", uid)

    def uids(self, view):
        return [m["uid"] for m in self.d.list_messages(["a"], "INBOX", 0, 50, view)["messages"]]

    def test_todo_moves_and_returns(self):
        self.d.mark_message(self.msg(2), "notifications")
        self.assertEqual(self.uids("notifications"), [2])
        for uid in (1, 2, 3):
            self.d.set_todo(self.msg(uid), True)
        self.assertEqual(sorted(self.uids("todo")), [1, 2, 3])
        for view in ("important", "fyi", "notifications", "unimportant"):
            self.assertEqual(self.uids(view), [], view)
        self.assertEqual(self.d.view_of("a", "INBOX", 2), "todo")
        self.assertTrue(self.msg(1)["todo"])
        # číslo u ToDo = všechny zprávy (i přečtená č. 3), ne jen nepřečtené
        counts = self.d.view_unread()
        self.assertEqual((counts["todo"], counts["important"], counts["notifications"]), (3, 0, 0))
        # Vyřešeno: zpráva se vrátí tam, odkud přišla
        self.d.set_todo(self.msg(2), False)
        self.d.set_todo(self.msg(1), False)
        self.assertEqual(self.d.view_of("a", "INBOX", 2), "notifications")
        self.assertEqual(self.uids("notifications"), [2])
        self.assertEqual(self.uids("important"), [1])
        self.assertEqual(self.uids("todo"), [3])

    def test_todo_in_user_folder(self):
        """Zpráva ve vlastní složce: v ToDo se ukáže, ze složky nezmizí, po Vyřešeno z ToDo odejde."""
        self.d.create_user_folder("Projekty", "a", "Projekty", False, False)
        self.d.insert_messages("a", "Projekty", [rec(7)])
        self.d.insert_messages("a", "Ostatni", [rec(8)])
        self.d.set_todo(self.d.message("a", "Projekty", 7), True)
        self.d.set_todo(self.d.message("a", "Ostatni", 8), True)  # složka mimo aplikaci se do ToDo nepočítá
        self.assertEqual(self.uids("todo"), [7])
        self.assertEqual(self.d.view_unread()["todo"], 1)
        m = self.d.message("a", "Projekty", 7)
        self.assertEqual((m["todo"], m["view"]), (True, "todo"))
        self.assertEqual([x["uid"] for x in self.d.list_messages(["a"], "Projekty", 0, 50)["messages"]], [7])
        self.assertEqual(self.d.list_position(["a"], "INBOX", "todo", "a", "Projekty", 7), 0)
        self.d.set_todo(m, False)
        self.assertEqual(self.uids("todo"), [])
        self.assertIsNone(self.d.message("a", "Projekty", 7)["view"])

    def test_folder_rules_skip_todo(self):
        """Zprávu v ToDo pravidlo složky nepřesune; po Vyřešeno už ano."""
        fid = self.d.create_user_folder("Projekty", "a", "Projekty", False, False)
        self.d.add_folder_rule(fid, db.kind_conds("sender", "eva@example.com"))
        self.d.set_todo(self.msg(1), True)
        self.assertEqual(sorted(self.d.folder_rule_matches(fid)["a"]), [2, 3])
        self.assertEqual(self.d.todo_keys(), {"<1@example.com>"})
        self.d.set_todo(self.msg(1), False)
        self.assertEqual(sorted(self.d.folder_rule_matches(fid)["a"]), [1, 2, 3])

    def test_without_message_id_survives_move(self):
        """Zpráva bez Message-ID: ToDo i ruční kategorie drží otisk (odesílatel, datum, předmět), ne umístění."""
        self.d.create_user_folder("Projekty", "a", "Projekty", False, False)
        r = rec(5, subject="Bez ID")
        r["message_id"] = ""
        self.d.insert_messages("a", "INBOX", [r])
        self.d.set_todo(self.msg(5), True)
        self.d.mark_message(self.msg(5), "notifications")
        self.d.delete_uids("a", "INBOX", [5])
        self.d.insert_messages("a", "Projekty", [dict(r, uid=50)])
        m = self.d.message("a", "Projekty", 50)
        self.assertTrue(m["todo"])
        self.assertEqual(self.uids("todo"), [50])
        self.d.set_todo(m, False)
        self.d.delete_uids("a", "Projekty", [50])
        self.d.insert_messages("a", "INBOX", [dict(r, uid=51)])
        self.assertEqual(self.d.view_of("a", "INBOX", 51), "notifications")

    def test_migrates_location_keys(self):
        path = Path(tempfile.mkdtemp()) / "m.db"
        d = db.Database(path)
        r = rec(5)
        r["message_id"] = ""
        d.insert_messages("a", "INBOX", [r])
        d._q("DELETE FROM meta WHERE key='fingerprint_keys_migrated'")
        d._q("INSERT INTO todos VALUES('a|INBOX|5', 0)")
        d._q("INSERT INTO todos VALUES('a|INBOX|99', 0)")  # zpráva už v databázi není: zůstane
        d._conn.close()
        d = db.Database(path)
        keys = {k for (k,) in d._q("SELECT key FROM todos")}
        self.assertEqual(keys, {db.message_key(d.message("a", "INBOX", 5)), "a|INBOX|99"})
        self.assertTrue(d.message("a", "INBOX", 5)["todo"])

    def test_migrates_fingerprint_without_size(self):
        path = Path(tempfile.mkdtemp()) / "m.db"
        d = db.Database(path)
        r = rec(5)
        r["message_id"] = ""
        d.insert_messages("a", "INBOX", [r])
        old = db.message_key(d.message("a", "INBOX", 5)).rsplit("|", 1)[0]  # otisk bez velikosti
        d._q("DELETE FROM meta WHERE key='fingerprint_size_migrated'")
        d._q("INSERT INTO todos VALUES(?, 0)", (old,))
        d._conn.close()
        d = db.Database(path)
        self.assertTrue(d.message("a", "INBOX", 5)["todo"])
        self.assertEqual({k for (k,) in d._q("SELECT key FROM todos")}, {db.message_key(d.message("a", "INBOX", 5))})

    def test_same_headers_different_size_are_different(self):
        r1, r2 = rec(5), rec(6)
        for r in (r1, r2):
            r.update(message_id="", date=1_700_000_000.0)
        r2["size"] = 999
        self.d.insert_messages("a", "INBOX", [r1, r2])
        self.d.set_todo(self.msg(5), True)
        self.assertEqual(self.uids("todo"), [5])

    def test_todo_survives_move_by_message_id_and_skips_auto_delete(self):
        self.d.mark_message(self.msg(2), "notifications")
        self.d.set_todo(self.msg(2), True)
        self.assertEqual(self.d.category_uids("notifications"), {})  # automatické mazání se ToDo netýká
        # na serveru přesunuto (nové UID, stejné Message-ID): pořád v ToDo
        self.d.delete_uids("a", "INBOX", [2])
        self.d.insert_messages("a", "INBOX", [rec(20, to="other@example.com", message_id="<2@example.com>")])
        self.assertEqual(self.d.view_of("a", "INBOX", 20), "todo")


if __name__ == "__main__":
    unittest.main()
