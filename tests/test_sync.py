"""sync.py: synchronizace složek, pravidla, přesuny, koš, úklid kategorií, odložené zprávy (falešný IMAP)."""

import logging
import os
import sys
import tempfile
import time
import unittest
from email.utils import formatdate
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import bayes  # noqa: E402
import db  # noqa: E402
import mail  # noqa: E402
import sync  # noqa: E402
from fake_imap import FakeIMAP, FakePool, make_raw  # noqa: E402

ME = "me@example.com"
FOLDERS = (("INBOX", None), ("Sent", "sent"), ("Trash", "trash"), ("Spam", "junk"), ("Archiv", None))
OLD = "Mon, 02 Jan 2023 10:00:00 +0000"


def setUpModule():
    logging.disable(logging.CRITICAL)  # chyby, které testy vyvolávají schválně


def tearDownModule():
    logging.disable(logging.NOTSET)


class FakeStore:
    def __init__(self, *accounts):
        self.accounts = {a["id"]: a for a in accounts}

    def all(self):
        return list(self.accounts.values())

    def get(self, acc_id):
        return self.accounts.get(acc_id)


class StopLoop(Exception):
    pass


def acc(acc_id="a"):
    return {"id": acc_id, "name": f"Účet {acc_id}", "email": f"{acc_id}@example.com"}


class SyncBase(unittest.TestCase):
    def setUp(self):
        self.d = db.Database(Path(tempfile.mkdtemp()) / "test.db")
        self.d.set_my_addresses("a", {ME})
        self.A, self.B = acc("a"), acc("b")
        self.M = FakeIMAP(FOLDERS)
        self.N = FakeIMAP(FOLDERS)
        self.s = sync.SyncManager(FakeStore(self.A, self.B), self.d, FakePool({"a": self.M, "b": self.N}))

    def inbox(self, acc_id="a", folder="INBOX"):
        return sorted(self.d.cached_flags(acc_id, folder))

    def full_sync(self, account=None):
        account = account or self.A
        self.s.refresh_folders(account)
        for name in self.M.folders if account is self.A else self.N.folders:
            self.s.sync_folder(account, name)


class HelpersTest(unittest.TestCase):
    def test_uid_ranges(self):
        self.assertEqual(sync.uid_ranges([10, 1, 2, 3, 7, 9]), "1:3,7,9:10")
        self.assertEqual(sync.uid_ranges([]), "")

    def test_context_folders(self):
        gmail = [{"name": "INBOX", "role": "inbox"}, {"name": "All", "role": "all"},
                 {"name": "Štítek", "role": None}, {"name": "Moje", "role": None}, {"name": "Sent", "role": "sent"}]
        self.assertEqual([f["name"] for f in sync.context_folders(gmail, {"Moje"})], ["INBOX", "Moje", "Sent", "All"])
        plain = [{"name": "Trash", "role": "trash"}, {"name": "X", "role": None}, {"name": "INBOX", "role": "inbox"}]
        self.assertEqual([f["name"] for f in sync.context_folders(plain)], ["INBOX", "X"])


class SyncFolderTest(SyncBase):
    def test_initial_and_incremental(self):
        u1 = self.M.add("INBOX", make_raw(mid="<1@x>"))
        u2 = self.M.add("INBOX", make_raw(mid="<2@x>"), ["\\Seen"])
        u3 = self.M.add("INBOX", make_raw(mid="<3@x>"), ["\\Deleted"])
        folders = self.s.refresh_folders(self.A)
        self.assertIn("Trash", [f["name"] for f in folders])
        self.assertTrue(self.s.sync_folder(self.A, "INBOX"))
        self.assertEqual(self.inbox(), [u1, u2])
        # server: příznaky se změnily, jedna zpráva zmizela, jedna přibyla, jedna se označila ke smazání
        self.M.flags("INBOX", u1).add("\\Flagged")
        del self.M.folders["INBOX"]["msgs"][u3]
        self.M.flags("INBOX", u2).add("\\Deleted")
        u4 = self.M.add("INBOX", make_raw(mid="<4@x>"))
        self.s.sync_folder(self.A, "INBOX")
        self.assertEqual(self.d.cached_flags("a", "INBOX"), {u1: (False, True), u4: (False, False)})
        # nová UIDVALIDITY – cache se zahodí a stáhne znovu
        self.M.folders["INBOX"]["uidvalidity"] = 2
        self.s.sync_folder(self.A, "INBOX")
        self.assertEqual(self.inbox(), [u1, u4])

    def test_batches_and_ensure_synced(self):
        for i in range(5):
            self.M.add("Archiv", make_raw(mid=f"<{i}@x>"))
        with mock.patch.object(sync, "BATCH", 2):
            self.assertFalse(self.s.sync_folder(self.A, "Archiv", max_batches=1))
            self.assertEqual(self.inbox(folder="Archiv"), [4, 5])
        self.s.ensure_synced(self.A, "INBOX")
        self.assertTrue(self.d.folder("a", "INBOX")["last_sync"])

    def test_trash_is_marked_read(self):
        self.M.add("Trash", make_raw())
        self.s.refresh_folders(self.A)
        self.s.sync_folder(self.A, "Trash")
        self.assertIn("\\Seen", self.M.flags("Trash", 1))
        self.assertEqual(self.d.unseen_uids("a", "Trash"), [])

    def test_spam_rule_moves_new_messages(self):
        self.d.add_spam_rule("Spammer@bad.cz")
        self.d.add_category_rule("notifications", sender="shop@bad.cz")
        self.M.add("INBOX", make_raw(sender="spammer@bad.cz"))
        self.M.add("INBOX", make_raw(sender="shop@bad.cz"))  # kategorie má přednost před spamem
        self.d.add_spam_rule("shop@bad.cz")
        self.s.refresh_folders(self.A)
        self.s.sync_folder(self.A, "INBOX")
        self.assertEqual(self.inbox(), [2])
        self.assertEqual(len(self.M.uids("Spam")), 1)
        self.assertEqual({r["sender"]: r["hits"] for r in self.d.spam_rules()}["spammer@bad.cz"], 1)

    def test_folder_rule_immediate(self):
        fid = self.d.create_user_folder("Faktury", "a", "Faktury", False, True)
        self.d.add_folder_rule(fid, db.kind_conds("subject", "faktura"))
        self.M.add("INBOX", make_raw(subject="Faktura za leden"))
        self.M.add("INBOX", make_raw(subject="Ahoj"))
        self.s.refresh_folders(self.A)
        self.s.sync_folder(self.A, "INBOX")
        self.assertEqual(self.inbox(), [2])
        self.assertEqual(len(self.M.uids("Faktury")), 1)
        self.assertIn("\\Seen", self.M.flags("Faktury", 1))
        self.assertEqual(self.d.folder_rules(fid)[0]["hits"], 1)

    def test_apply_folder_rules_after_read(self):
        u = self.M.add("INBOX", make_raw(sender="eva@x.cz"), ["\\Seen"])
        self.full_sync()
        fid = self.d.create_user_folder("Eva", "a", "Eva", True, False)
        self.d.add_folder_rule(fid, db.kind_conds("sender", "eva@x.cz"), after_read=True)
        self.s.apply_folder_rules()
        self.assertNotIn(u, self.M.uids("INBOX"))
        self.assertEqual(self.inbox(), [])
        self.assertEqual(len(self.d.cached_flags("a", "Eva")), 1)

    def test_backfill_refs(self):
        self.M.add("INBOX", make_raw(mid="<2@x>", extra="References: <1@x>"))
        self.s.sync_folder(self.A, "INBOX")
        self.d._q("UPDATE messages SET refs=NULL")
        self.assertTrue(self.s.backfill_refs(self.A, "INBOX"))
        self.assertEqual(self.d.refs_of("a", "INBOX", 1), ["<1@x>"])

    def test_context_ready(self):
        self.assertFalse(self.s.context_ready("a"))
        self.full_sync()
        self.assertTrue(self.s.context_ready("a"))


class OperationsTest(SyncBase):
    def test_enqueue_success_and_error(self):
        u = self.M.add("INBOX", make_raw())
        self.s.sync_folder(self.A, "INBOX")
        self.s.enqueue(self.A, "INBOX", u, "Smazat", lambda M: mail.trash_uids(M, "INBOX", [u]), remove=True)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.s.pending, set())
        errors = []
        self.s.enqueue(self.A, "INBOX", [1, 2], "Akce", lambda: 1 / 0, on_error=errors.append, use_conn=False)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.s.notices[-1][1], errors[0])

    def test_trash_spam_and_empty(self):
        u1 = self.M.add("INBOX", make_raw(mid="<1@x>"))
        u2 = self.M.add("INBOX", make_raw(mid="<2@x>"))
        self.full_sync()
        self.s.trash_messages(self.A, "INBOX", [u1])
        self.assertEqual(len(self.d.cached_flags("a", "Trash")), 1)
        self.s.spam_messages(self.A, "INBOX", [u2])
        self.assertEqual(len(self.d.cached_flags("a", "Spam")), 1)
        self.assertEqual(self.s.empty_trash(self.A, "Trash"), 1)
        self.assertEqual(self.d.cached_flags("a", "Trash"), {})

    def test_spam_creates_folder(self):
        M = FakeIMAP((("INBOX", None),))
        self.s.pool.servers["a"] = M
        u = M.add("INBOX", make_raw())
        self.s.refresh_folders(self.A)
        self.s.sync_folder(self.A, "INBOX")
        self.s.spam_messages(self.A, "INBOX", [u])
        self.assertEqual(self.d.spam_folders(), {"a": "Spam"})
        self.assertEqual(len(self.d.cached_flags("a", "Spam")), 1)

    def test_delete_server_folder(self):
        self.M.add("Archiv", make_raw())
        self.full_sync()
        self.s.delete_server_folder(self.A, "Archiv", "Archiv")
        self.assertNotIn("Archiv", self.M.folders)
        self.assertEqual(self.d.cached_flags("a", "Archiv"), {})
        self.s.delete_server_folder(self.A, "Neni", "Neni")  # chyba → hlášení
        self.assertTrue(self.s.notices)

    def test_move_between_accounts(self):
        u1 = self.M.add("INBOX", make_raw(mid="<1@x>"), ["\\Flagged"])
        u2 = self.M.add("INBOX", make_raw(mid="<2@x>"))
        self.M.size_override[("INBOX", u2)] = mail.MAX_MESSAGE + 1
        self.full_sync()
        fid = self.d.create_user_folder("Cíl", "b", "Cíl", False, True)
        target = self.d.user_folder(fid)
        with self.assertRaises(mail.MessageTooLarge):
            self.s.move_messages(self.A, "INBOX", [u1, u2], target)
        self.assertEqual(self.M.uids("INBOX"), [u2])
        self.assertEqual(self.N.flags("Cíl", 1), {"\\Flagged", "\\Seen"})
        self.assertEqual(len(self.d.cached_flags("b", "Cíl")), 1)
        self.assertEqual(self.s.move_messages(self.A, "INBOX", [], target), [])
        u3 = self.M.add("INBOX", make_raw(mid="<3@x>"))
        self.N.fail["append"] = "NO"
        with self.assertRaises(mail.MailError):
            self.s.move_messages(self.A, "INBOX", [u3], target)
        self.assertIn(u3, self.M.uids("INBOX"))  # nevložená zpráva zůstává ve zdroji
        gone = {**target, "account_id": "neni"}
        with self.assertRaises(mail.MailError):
            self.s.move_messages(self.A, "INBOX", [u2], gone)

    def test_mark_category_read_and_cleanup(self):
        self.d.add_category_rule("notifications", sender="shop@x.cz")
        self.M.add("INBOX", make_raw(sender="shop@x.cz", date=OLD))
        self.M.add("Spam", make_raw(date=OLD))
        self.full_sync()
        self.assertEqual(self.s.mark_category_read("notifications"), 1)
        self.assertIn("\\Seen", self.M.flags("INBOX", 1))
        self.d.update_settings({"notify_delete_read_after_week": True, "spam_delete_after_month": True})
        self.s.cleanup_categories()
        self.assertEqual(self.M.uids("INBOX"), [])
        self.assertEqual(self.M.uids("Spam"), [])
        self.assertEqual(len(self.M.uids("Trash")), 2)
        self.assertEqual(self.inbox(), [])
        # Smazané: natrvalo, jen zprávy starší než lhůta
        self.M.add("Trash", make_raw(date=formatdate()), ["\\Seen"])  # čerstvá zůstane
        self.full_sync()
        self.d.update_settings({"trash_delete_after_year": True})
        self.s.cleanup_categories()
        self.assertEqual(len(self.M.uids("Trash")), 1)
        self.assertEqual(len(self.d.trash_folders()), 1)
        self.assertEqual(self.d.trash_total(["a"]), 1)

    def test_delete_age(self):
        self.assertIsNone(db.delete_age({}, "trash"))
        self.assertEqual(db.delete_age({"spam_delete_after_year": True}, "spam"), db.YEAR)
        both = {"trash_delete_after_month": True, "trash_delete_after_year": True}
        self.assertEqual(db.delete_age(both, "trash"), db.MONTH)

    def test_snoozes(self):
        u = self.M.add("INBOX", make_raw(mid="<1@x>"), ["\\Seen"])
        self.s.sync_folder(self.A, "INBOX")
        self.d.add_snooze("a", "INBOX", u, "<1@x>", time.time() - 1)
        self.d.add_snooze("zmizel", "INBOX", 5, "<5@x>", time.time() - 1)
        self.s.process_snoozes()
        self.assertNotIn("\\Seen", self.M.flags("INBOX", u))
        self.assertEqual(self.d.cached_flags("a", "INBOX")[u], (False, False))
        self.assertEqual(self.d.due_snoozes(), [])

    def test_errors_are_recorded(self):
        self.M.fail["status"] = "NO"
        self.assertEqual(self.s._sync_inbox(self.A), [])
        self.assertIn("a", self.s.errors)
        self.s._sync_target(self.A, "INBOX")
        self.M.fail.clear()
        self.s._sync_inbox(self.A)
        self.assertNotIn("a", self.s.errors)

    def test_loop_one_cycle(self):
        self.M.add("INBOX", make_raw())
        self.N.add("Archiv", make_raw())
        with mock.patch.object(self.s._wake, "wait", side_effect=StopLoop), \
                mock.patch.object(self.s, "start_bayes_bootstrap"):
            with self.assertRaises(StopLoop):
                self.s._loop()
        self.assertEqual(self.inbox(), [1])
        self.assertEqual(len(self.d.cached_flags("b", "Archiv")), 1)

    def test_start_threads(self):
        with mock.patch.object(sync.threading, "Thread") as T:
            self.s.start()
            self.s.start()
        self.assertEqual(T.call_count, 2)


class BayesSyncTest(SyncBase):
    def fresh(self, sender, subject, body):
        return make_raw(sender=sender, subject=subject, body=body, date=formatdate())

    def teach(self):
        for i in range(12):
            self.s.bayes.train(f"s{i}", {"viagra", "casino", "winner", f"n{i}"}, bayes.SPAM)
            self.s.bayes.train(f"i{i}", {"meeting", "project", "thanks", f"n{i}"}, bayes.INBOX)

    def test_new_spam_is_moved(self):
        self.teach()
        self.M.add("INBOX", self.fresh("x@spam.biz", "Winner", "viagra casino winner"))
        self.M.add("INBOX", self.fresh("boss@work.cz", "Project", "meeting project thanks"))
        self.s.refresh_folders(self.A)
        self.s.sync_folder(self.A, "INBOX")
        self.assertEqual(self.inbox(), [2])
        self.assertEqual(len(self.M.uids("Spam")), 1)
        self.assertEqual(self.d._q("SELECT target FROM bf_auto")[0][0], bayes.SPAM)
        self.assertEqual(self.d._q("SELECT COUNT(*) FROM bf_seen")[0][0], 2)

    def test_trusted_authserv(self):
        for _ in range(10):
            self.M.add("INBOX", make_raw(extra="Authentication-Results: mx.google.com; dmarc=pass"))
        self.assertEqual(self.s._trusted_authserv(self.A), "mx.google.com")
        self.M.fail["select"] = "NO"
        self.assertEqual(self.s._trusted_authserv(self.A), "mx.google.com")  # z cache
        self.d.meta_set("authserv:a", {"id": "old", "ts": 0})
        self.M.fail["select"] = RuntimeError("down")
        self.assertEqual(self.s._trusted_authserv(self.A), "old")

    def test_learn_from_user_action(self):
        self.M.add("INBOX", make_raw(subject="Casino", body="viagra winner"))
        self.s.bayes_learn(self.M, self.A, "INBOX", {1: bayes.SPAM, 2: None})
        self.assertEqual(self.s.bayes.counts(), {bayes.SPAM: 1})
        with mock.patch.object(self.s.bayes, "learn", side_effect=RuntimeError):
            self.s.bayes_learn(self.M, self.A, "INBOX", {1: bayes.INBOX})  # chyba se jen zaloguje

    def test_learn_async_and_maintenance(self):
        self.M.add("INBOX", make_raw(subject="Casino", body="viagra winner"))
        self.s.sync_folder(self.A, "INBOX")
        with mock.patch.object(sync.threading, "Thread") as T:
            self.s.bayes_learn_async(self.A, "INBOX", {1: bayes.SPAM})
            T.call_args.kwargs["target"]()
            self.s.bayes_learn_async(self.A, "INBOX", {1: None})
            self.assertEqual(T.call_count, 1)
            self.s.bayes_maintenance()  # v doručené poště jsou zprávy, filtr ještě není naučený
            self.assertEqual(self.s.bayes.bootstrap_state["status"], "running")
        self.assertEqual(self.s.bayes.counts(), {bayes.SPAM: 1})


if __name__ == "__main__":
    unittest.main()
