"""Tlačítko Důležité: zpráva v kopii nebo z kategorie se ručně označí jako důležitá."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import db  # noqa: E402
import i18n  # noqa: E402
from test_todo import rec  # noqa: E402


class ImportantTest(unittest.TestCase):
    def setUp(self):
        self.d = db.Database(Path(tempfile.mkdtemp()) / "test.db")
        self.d.set_my_addresses("a", {"me@example.com"})
        self.d.insert_messages("a", "INBOX", [rec(1), rec(2, to="other@example.com")])
        self.msg = lambda uid: self.d.message("a", "INBOX", uid)

    def uids(self, view):
        return [m["uid"] for m in self.d.list_messages(["a"], "INBOX", 0, 50, view)["messages"]]

    def test_view_field(self):
        self.assertEqual(self.msg(1)["view"], "important")
        self.assertEqual(self.msg(2)["view"], "fyi")
        self.d.set_todo(self.msg(2), True)
        self.assertEqual(self.msg(2)["view"], "todo")

    def test_copy_marked_important(self):
        self.d.mark_message(self.msg(2), "important")
        self.assertEqual(self.uids("fyi"), [])
        self.assertEqual(sorted(self.uids("important")), [1, 2])
        self.assertEqual(self.d.view_of("a", "INBOX", 2), "important")
        self.assertEqual(self.msg(2)["view"], "important")
        self.assertFalse(self.msg(2)["notification"] or self.msg(2)["unimportant"])

    def test_category_mark_replaces_important(self):
        self.d.mark_message(self.msg(2), "important")
        self.d.mark_message(self.msg(2), "unimportant")
        self.assertEqual(self.uids("unimportant"), [2])
        self.assertEqual(self.uids("important"), [1])
        # a zpátky: Důležité
        self.d.uncategorize(self.msg(2), "unimportant")
        self.d.mark_message(self.msg(2), "important")
        self.assertEqual(sorted(self.uids("important")), [1, 2])
        self.assertEqual(self.uids("unimportant"), [])

    def test_important_mark_beats_rules(self):
        self.d.mark_message(self.msg(2), "important")
        self.d.add_category_rule("notifications", sender="eva@example.com")
        self.assertEqual(self.uids("notifications"), [1])  # pravidlo platí pro ostatní zprávy
        self.assertEqual(self.uids("important"), [2])
        self.assertEqual(self.d.categorizer()(self.msg(2)), "important")

    def test_search_label(self):
        self.d.mark_message(self.msg(2), "important")
        found, _ = self.d.search_all("ahoj", ["a"])
        self.assertEqual({m["uid"]: m["folder_label"] for m in found}[2], i18n.t("view.important"))


if __name__ == "__main__":
    unittest.main()
