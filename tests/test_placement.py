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

    def test_sender_domain_and_exact_subject(self):
        """Odesílatel jen jako doména (i subdomény); předmět celý, nebo jeho část. SQL i Python stejně."""
        def from_(uid, addr, subject):
            r = rec(uid, subject=subject)
            r["from"] = [{"name": "", "addr": addr}]
            return r
        self.d.insert_messages("a", "INBOX", [from_(3, "noreply@github.com", "Build failed"),
                                              from_(4, "x@notifications.github.com", "Faktura za březen"),
                                              from_(5, "x@notgithub.com", "Faktura")])
        self.d.add_category_rule("notifications", sender="github.com")
        self.assertEqual(self.uids("notifications"), [3, 4])
        self.d.add_category_rule("unimportant", subject="faktura", subject_exact=True)
        self.assertEqual(self.uids("unimportant"), [2, 5])  # „Faktura za březen“ ne
        self.assertEqual(self.d.category_rule_preview("unimportant", "", "", "faktura"), 1)  # část: i č. 4
        cat = self.d.categorizer(marks=False)
        self.d._q("DELETE FROM rule_placements")  # categorizer podle živých pravidel
        self.assertEqual([cat(self.msg(u)) for u in (3, 4, 5, 1)], ["notifications", "notifications", "unimportant", None])
        # totéž pravidlo znovu, ale „část předmětu“: převezme volbu
        self.d.add_category_rule("unimportant", subject="faktura")
        self.assertEqual([r["subject_exact"] for r in self.d.category_rules("unimportant")], [0])

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
        self.d.add_folder_rule(fid, db.kind_conds("sender", "eva@example.com"))
        self.assertEqual(sorted(self.d.folder_rule_matches(fid)["a"]), [1, 2])
        self.d.mark_message(self.msg(2), "fyi")  # vrácená ze složky do doručené pošty
        self.assertEqual(sorted(self.d.folder_rule_matches(fid)["a"]), [1])
        self.assertEqual(self.d.locked_keys([self.msg(1), self.msg(2)]), {db.message_key(self.msg(2))})

    def test_matching_rules_and_release(self):
        fid = self.d.create_user_folder("Faktury", "a", "Faktury", False, False)
        self.d.add_folder_rule(fid, db.kind_conds("subject", "faktura"))
        self.d.add_category_rule("notifications", sender="eva@example.com")
        self.d.add_spam_rule("eva@example.com")
        rules = self.d.matching_rules(self.msg(2))
        self.assertEqual(sorted(r["type"] for r in rules), ["category", "folder", "spam"])
        self.assertEqual([r["type"] for r in self.d.matching_rules(self.msg(1))], ["category", "spam"])
        self.d.delete_rules(rules)
        self.assertEqual(self.d.matching_rules(self.msg(2)), [])

    def test_replace_simple_rules(self):
        fid = self.d.create_user_folder("Eva", "a", "Eva", False, False)
        self.d.add_folder_rule(fid, db.kind_conds("sender", "eva@example.com"))
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
        self.d.add_folder_rule(fid, db.kind_conds("subject", "faktura"))
        self.assertEqual(self.d.placement(in_folder, "a", "Faktury")["layer"], "rule")

    def test_natural_view(self):
        self.assertEqual(self.d.natural_view(self.msg(1), "a"), "important")
        self.assertEqual(self.d.natural_view(self.msg(2), "a"), "fyi")
        self.d.add_category_rule("notifications", sender="eva@example.com")
        self.d.mark_message(self.msg(1), "important")
        self.assertEqual(self.d.natural_view(self.msg(1), "a"), "notifications")  # bez ruční značky


class RulePlacementTest(unittest.TestCase):
    """Zařazení pravidlem zůstane, i když se pravidlo smaže nebo změní; nové zprávy řadí platná pravidla."""
    def setUp(self):
        self.d = db.Database(Path(tempfile.mkdtemp()) / "test.db")
        self.d.set_my_addresses("a", {"me@example.com"})
        self.d.insert_messages("a", "INBOX", [rec(1), rec(2, subject="Faktura")])
        self.msg = lambda uid: self.d.message("a", "INBOX", uid)

    def uids(self, view):
        return sorted(m["uid"] for m in self.d.list_messages(["a"], "INBOX", 0, 50, view)["messages"])

    def rule_id(self, cat):
        return self.d.category_rules(cat)[0]["id"]

    def test_delete_rule_keeps_messages(self):
        self.d.add_category_rule("notifications", sender="eva@example.com")
        self.assertEqual(self.uids("notifications"), [1, 2])
        self.assertEqual(self.d.category_rules("notifications")[0]["messages"], 2)
        self.d.delete_category_rule("notifications", self.rule_id("notifications"))
        self.assertEqual(self.uids("notifications"), [1, 2])
        p = self.d.placement(self.msg(1), "a", "INBOX")
        self.assertEqual((p["layer"], p["target"], p["changed"]), ("rule", "notifications", True))
        self.assertEqual(p["rules"][0]["sender"], "eva@example.com")
        # nová zpráva od stejného odesílatele už pravidlo nemá
        self.d.insert_messages("a", "INBOX", [rec(3)])
        self.assertEqual(self.uids("important"), [3])

    def test_new_messages_follow_current_rule(self):
        self.d.insert_messages("a", "INBOX", [])
        self.d.add_category_rule("unimportant", subject="faktura")
        self.assertEqual(self.uids("unimportant"), [2])
        self.d.insert_messages("a", "INBOX", [rec(3, subject="Faktura 2"), rec(4)])
        self.assertEqual(self.uids("unimportant"), [2, 3])
        p = self.d.placement(self.msg(3), "a", "INBOX")
        self.assertEqual((p["layer"], p["changed"]), ("rule", False))

    def test_edit_rule_future_only(self):
        self.d.add_category_rule("unimportant", subject="faktura")
        rid = self.rule_id("unimportant")
        self.assertEqual(self.d.category_rule_preview("unimportant", "", "", "ahoj"), 1)
        self.d.update_category_rule("unimportant", rid, "", "", "ahoj")
        self.assertEqual(self.uids("unimportant"), [2])  # podle původního znění zůstává, č. 1 se nepřesune
        self.assertEqual(self.d.category_rules("unimportant")[0]["messages"], 1)
        self.d.insert_messages("a", "INBOX", [rec(3, subject="Ahoj"), rec(4, subject="Faktura")])
        self.assertEqual(self.uids("unimportant"), [2, 3])

    def test_edit_rule_apply_existing(self):
        self.d.add_category_rule("unimportant", subject="faktura")
        self.d.mark_message(self.msg(1), "important")  # ručně umístěná zpráva se nepřesune
        self.d.insert_messages("a", "INBOX", [rec(3, subject="Ahoj")])
        self.assertEqual(self.d.category_rule_preview("unimportant", "", "", "ahoj"), 1)
        self.d.update_category_rule("unimportant", self.rule_id("unimportant"), "", "", "ahoj", apply_existing=True)
        self.assertEqual(self.uids("unimportant"), [2, 3])
        self.assertEqual(self.uids("important"), [1])

    def test_migration_keeps_current_state(self):
        path = Path(tempfile.mkdtemp()) / "m.db"
        d = db.Database(path)
        d.set_my_addresses("a", {"me@example.com"})
        d.insert_messages("a", "INBOX", [rec(1), rec(2, subject="Faktura")])
        d._q("INSERT INTO category_rules(category, sender, recipient, subject, created) "
             "VALUES('notifications', 'eva@example.com', '', '', 1), ('unimportant', '', '', 'faktura', 1)")
        d._q("DELETE FROM meta WHERE key='rule_placements_migrated'")
        d._conn.close()
        d = db.Database(path)
        self.assertEqual(d.view_of("a", "INBOX", 1), "notifications")
        self.assertEqual(d.view_of("a", "INBOX", 2), "unimportant")  # přednost Nedůležitých
        d._q("DELETE FROM category_rules")
        self.assertEqual(d.view_of("a", "INBOX", 2), "unimportant")


if __name__ == "__main__":
    unittest.main()


class CondRulesMigrationTest(unittest.TestCase):
    """Pravidla Spamu a složek z dřívější verze (kind + value) se převedou na podmínky jako u kategorií."""

    def test_old_rules_are_converted(self):
        path = Path(tempfile.mkdtemp()) / "test.db"
        d = db.Database(path)
        fid = d.create_user_folder("Faktury", "a", "Faktury", False, False)
        d._q("INSERT INTO spam_rules(kind, value, created) VALUES('sender', 'spam@x.cz', 1)")
        d._q("INSERT INTO user_folder_rules(folder_id, kind, value, created, after_read) VALUES(?, 'subject', 'faktura', 1, 1)",
             (fid,))
        d._conn.close()
        d = db.Database(path)
        self.assertEqual([(r["sender"], r["subject"]) for r in d.spam_rules()], [("spam@x.cz", "")])
        self.assertEqual(d.spam_senders(), {"spam@x.cz"})
        r = d.folder_rules(fid)[0]
        self.assertEqual((r["sender"], r["subject"], r["subject_exact"], r["after_read"]), ("", "faktura", 0, 1))
        self.assertNotIn("kind", r)
        # stejné pravidlo znovu = totéž pravidlo (jedinečnost drží klíč z podmínek)
        self.assertEqual(d.add_folder_rule(fid, db.kind_conds("subject", "Faktura")), r["id"])
