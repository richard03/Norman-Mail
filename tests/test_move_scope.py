"""Ruční přesun (/api/messages/move): rozsah jen zprávy / pravidlo / zrušit pravidla, zámek zprávy."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import db  # noqa: E402
from test_todo import rec  # noqa: E402

import app as app_module  # noqa: E402

ACC = {"id": "a", "name": "Účet", "email": "me@example.com"}


class MoveScopeTest(unittest.TestCase):
    def setUp(self):
        self.d = db.Database(Path(tempfile.mkdtemp()) / "test.db")
        self.d.set_my_addresses("a", {"me@example.com"})
        self.d.insert_messages("a", "INBOX", [rec(1), rec(2, subject="Faktura")])
        self.patches = [
            mock.patch.object(app_module, "database", self.d),
            mock.patch.object(app_module.store, "get", lambda acc_id: ACC if acc_id == "a" else None),
            mock.patch.object(app_module.store, "list_public", lambda: [ACC]),
            mock.patch.object(app_module.syncer, "enqueue", lambda *a, **k: None),
            mock.patch.object(app_module.syncer, "trigger", lambda: None),
            mock.patch.object(app_module.syncer, "bayes_learn_async", lambda *a, **k: None),
        ]
        for p in self.patches:
            p.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def post(self, url, body):
        return self.client.post(url, json=body, headers={"X-Mailer": "1"}, base_url="http://127.0.0.1")

    def move(self, uids, target, **extra):
        items = [{"account_id": "a", "folder": "INBOX", "uid": u} for u in uids]
        r = self.post("/api/messages/move", {"target": target, "items": items, **extra})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def preview(self, uids, target):
        items = [{"account_id": "a", "folder": "INBOX", "uid": u} for u in uids]
        return self.post("/api/messages/move/preview", {"target": target, "items": items}).get_json()

    def uids(self, view):
        return sorted(m["uid"] for m in self.d.list_messages(["a"], "INBOX", 0, 50, view)["messages"])

    def test_only_message_keeps_rule_and_locks(self):
        self.d.add_category_rule("notifications", sender="eva@example.com")
        self.move([1], {"type": "inbox", "view": "unimportant"})
        self.assertEqual(self.uids("unimportant"), [1])
        self.assertEqual(self.uids("notifications"), [2])
        self.assertEqual(len(self.d.category_rules("notifications")), 1)

    def test_sender_rule_replaces_other_category(self):
        self.d.add_category_rule("notifications", sender="eva@example.com")
        p = self.preview([1], {"type": "inbox", "view": "unimportant"})
        self.assertEqual(p["senders"], ["eva@example.com"])
        self.assertEqual([r["target"] for r in p["rules"]], ["notifications"])
        self.assertIn("custom", p["scopes"])
        self.move([1], {"type": "inbox", "view": "unimportant"}, scope="sender")
        self.assertEqual(self.d.category_rules("notifications"), [])
        self.assertEqual(self.uids("unimportant"), [1, 2])

    def test_important_does_not_delete_rules_unless_release(self):
        self.d.add_category_rule("notifications", sender="eva@example.com")
        p = self.preview([1], {"type": "inbox", "view": "important"})
        self.assertEqual(p["scopes"], ["message", "release"])
        self.move([1], {"type": "inbox", "view": "important"})
        self.assertEqual(self.uids("important"), [1])
        self.assertEqual(self.uids("notifications"), [2])
        self.move([2], {"type": "inbox", "view": "fyi"}, scope="release")
        self.assertEqual(self.d.category_rules("notifications"), [])
        self.assertEqual(self.uids("fyi"), [2])

    def test_custom_rule(self):
        self.move([2], {"type": "inbox", "view": "notifications"}, scope="custom",
                  rule={"sender": "", "recipient": "", "subject": "faktura"})
        self.assertEqual([r["subject"] for r in self.d.category_rules("notifications")], ["faktura"])

    def test_invalid_scope(self):
        items = [{"account_id": "a", "folder": "INBOX", "uid": 1}]
        r = self.post("/api/messages/move", {"target": {"type": "inbox", "view": "important"},
                                             "items": items, "scope": "sender"})
        self.assertEqual(r.status_code, 400)

    def test_spam_message_scope_has_no_rule(self):
        self.move([1], {"type": "spam"})
        self.assertEqual(self.d.spam_senders(), set())
        self.d.insert_messages("a", "INBOX", [rec(3)])
        self.move([3], {"type": "spam"}, scope="sender")
        self.assertEqual(self.d.spam_senders(), {"eva@example.com"})


if __name__ == "__main__":
    unittest.main()
