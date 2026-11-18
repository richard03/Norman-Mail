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
