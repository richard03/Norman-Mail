"""Složka Archiv: archivní složky všech účtů; u Gmailu „Všechny zprávy“ bez zpráv, které jsou i jinde."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import db  # noqa: E402
from test_todo import rec  # noqa: E402


def folder(name, role):
    return {"name": name, "display": name, "role": role, "depth": 0}


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.d = db.Database(Path(tempfile.mkdtemp()) / "test.db")
        # a: běžný server se složkou Archive; g: Gmail se „Všemi zprávami“
        self.d.upsert_folders("a", [folder("INBOX", "inbox"), folder("Archive", "archive")])
        self.d.upsert_folders("g", [folder("INBOX", "inbox"), folder("[Gmail]/Sent Mail", "sent"),
                                    folder("[Gmail]/All Mail", "all")])
        self.d.insert_messages("a", "Archive", [rec(1)])
        self.d.insert_messages("g", "INBOX", [rec(10, message_id="<in@x>")])
        self.d.insert_messages("g", "[Gmail]/Sent Mail", [rec(11, message_id="<sent@x>")])
        self.d.insert_messages("g", "[Gmail]/All Mail", [rec(20, message_id="<in@x>"), rec(21, message_id="<sent@x>"),
                                                         rec(22, message_id="<archived@x>")])

    def listed(self):
        res = self.d.list_messages([], self.d.archive_folders(), 0, 50, archived=True)
        return sorted((m["account_id"], m["uid"]) for m in res["messages"])

    def test_archive_folders(self):
        self.assertEqual(self.d.archive_folders(), {"a": "Archive", "g": "[Gmail]/All Mail"})

    def test_gmail_all_mail_without_inbox_and_sent(self):
        self.assertEqual(self.listed(), [("a", 1), ("g", 22)])

    def test_returned_to_inbox_leaves_archive(self):
        self.d.insert_messages("g", "INBOX", [rec(12, message_id="<archived@x>")])
        self.assertEqual(self.listed(), [("a", 1)])

    def test_gmail_app_folder_not_in_archive(self):
        """Složka aplikace je u Gmailu jen štítek – zpráva v ní je i ve „Všech zprávách“, v Archivu ale ne."""
        self.d.upsert_folders("g", [folder("INBOX", "inbox"), folder("[Gmail]/Sent Mail", "sent"),
                                    folder("[Gmail]/All Mail", "all"), folder("Faktury", None)])
        self.d.create_user_folder("Faktury", "g", "Faktury", False, False)
        self.d.insert_messages("g", "Faktury", [rec(30, message_id="<archived@x>")])
        self.assertEqual(self.listed(), [("a", 1)])

    def test_position(self):
        pos = self.d.list_position([], self.d.archive_folders(), "all", "g", "[Gmail]/All Mail", 22, archived=True)
        self.assertIsNotNone(pos)
        self.assertIsNone(self.d.list_position([], self.d.archive_folders(), "all", "g", "[Gmail]/All Mail", 20,
                                               archived=True))


if __name__ == "__main__":
    unittest.main()
