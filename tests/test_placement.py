"""Ruční umístění zprávy má přednost před automatickým řazením a pravidla se při přesunu nemažou."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import db  # noqa: E402
from test_todo import rec  # noqa: E402


class PlacementTest(unittest.TestCase):
    def setUp(self):
        self.d = db.Database(Path(tempfile.mkdtemp()) / "test.db")
        self.d.set_my_addresses("a", {"me@example.com"})
        self.d.insert_messages("a", "INBOX", [rec(1), rec(2, to="other@example.com", subject="Faktura")])
        self.msg = lambda uid: self.d.message("a", "INBOX", uid)

    def uids(self, view):
        return sorted(m["uid"] for m in self.d.list_messages(["a"], "INBOX", 0, 50, view)["messages"])

    def test_manual_move_beats_other_category_rule(self):
        """Notifikace → Nedůležité: zpráva zůstane v Nedůležitých, i když jí odpovídá pravidlo Notifikací."""
        self.d.add_category_rule("notifications", sender="eva@example.com")
        self.assertEqual(self.uids("notifications"), [1, 2])
        self.d.mark_message(self.msg(1), "unimportant")
        self.assertEqual(self.uids("unimportant"), [1])
        self.assertEqual(self.uids("notifications"), [2])  # pravidlo dál platí pro ostatní zprávy

    def test_priority_unimportant_before_notifications(self):
        self.d.add_category_rule("notifications", sender="eva@example.com")
        self.d.add_category_rule("unimportant", subject="faktura")
        self.assertEqual(self.uids("unimportant"), [2])
        self.assertEqual(self.uids("notifications"), [1])
        self.assertEqual(self.d.categorizer()(self.msg(2)), "unimportant")

    def test_fyi_mark(self):
        self.d.add_category_rule("notifications", sender="eva@example.com")
        self.d.mark_message(self.msg(1), "fyi")  # přímo pro mě, ale ručně do Vzít na vědomí
        self.assertEqual(self.uids("fyi"), [1])
        self.assertEqual(self.uids("important"), [])
        self.assertEqual(self.msg(1)["view"], "fyi")
        self.assertEqual(self.d.view_of("a", "INBOX", 1), "fyi")

    def test_locked_message_skips_folder_rules(self):
        fid = self.d.create_user_folder("Faktury", "a", "Faktury", False, False)
        self.d.add_folder_rule(fid, "sender", "eva@example.com")
        self.assertEqual(sorted(self.d.folder_rule_matches(fid)["a"]), [1, 2])
        self.d.mark_message(self.msg(2), "fyi")  # vrácená ze složky do doručené pošty
        self.assertEqual(sorted(self.d.folder_rule_matches(fid)["a"]), [1])
        self.assertEqual(self.d.locked_keys([self.msg(1), self.msg(2)]), {db.message_key(self.msg(2))})

    def test_matching_rules_and_release(self):
        fid = self.d.create_user_folder("Faktury", "a", "Faktury", False, False)
        self.d.add_folder_rule(fid, "subject", "faktura")
        self.d.add_category_rule("notifications", sender="eva@example.com")
        self.d.add_spam_rule("eva@example.com")
        rules = self.d.matching_rules(self.msg(2))
        self.assertEqual(sorted(r["type"] for r in rules), ["category", "folder", "spam"])
        self.assertEqual([r["type"] for r in self.d.matching_rules(self.msg(1))], ["category", "spam"])
        self.d.delete_rules(rules)
        self.assertEqual(self.d.matching_rules(self.msg(2)), [])

    def test_replace_simple_rules(self):
        fid = self.d.create_user_folder("Eva", "a", "Eva", False, False)
        self.d.add_folder_rule(fid, "sender", "eva@example.com")
        self.d.add_spam_rule("eva@example.com")
        self.d.add_category_rule("notifications", sender="eva@example.com")
        self.d.add_category_rule("notifications", sender="eva@example.com", subject="faktura")  # složitější zůstane
        self.d.replace_simple_rules("sender", "Eva@example.com", ("category", "unimportant"))
        self.d.add_category_rule("unimportant", sender="eva@example.com")
        rules = self.d.matching_rules(self.msg(2))
        self.assertEqual(sorted((r["type"], r["target"], r["subject"]) for r in rules),
                         [("category", "notifications", "faktura"), ("category", "unimportant", "")])
        # pravidlo stejné kategorie / složky se nemaže
        self.d.replace_simple_rules("sender", "eva@example.com", ("category", "unimportant"))
        self.assertEqual(len(self.d.matching_rules(self.msg(2))), 2)

    def test_placement_explains_why(self):
        p = lambda uid: self.d.placement(self.msg(uid), "a", "INBOX")
        self.assertEqual((p(1)["layer"], p(1)["target"]), ("default", "important"))
        self.assertEqual((p(2)["layer"], p(2)["target"]), ("default", "fyi"))
        self.d.add_category_rule("notifications", sender="eva@example.com")
        self.assertEqual((p(1)["layer"], p(1)["target"]), ("rule", "notifications"))
        self.assertEqual(p(1)["rules"][0]["sender"], "eva@example.com")
        self.d.mark_message(self.msg(1), "important")
        self.assertEqual((p(1)["layer"], p(1)["target"]), ("manual", "important"))
        fid = self.d.create_user_folder("Faktury", "a", "Faktury", False, False)
        self.d.insert_messages("a", "Faktury", [rec(5, subject="Faktura")])
        in_folder = self.d.message("a", "Faktury", 5)
        self.assertEqual(self.d.placement(in_folder, "a", "Faktury")["layer"], "manual")
        self.d.add_folder_rule(fid, "subject", "faktura")
        self.assertEqual(self.d.placement(in_folder, "a", "Faktury")["layer"], "rule")

    def test_natural_view(self):
        self.assertEqual(self.d.natural_view(self.msg(1), "a"), "important")
        self.assertEqual(self.d.natural_view(self.msg(2), "a"), "fyi")
        self.d.add_category_rule("notifications", sender="eva@example.com")
        self.d.mark_message(self.msg(1), "important")
        self.assertEqual(self.d.natural_view(self.msg(1), "a"), "notifications")  # bez ruční značky


if __name__ == "__main__":
    unittest.main()
