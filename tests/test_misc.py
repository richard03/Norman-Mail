"""Doplňkové testy: slova zprávy a první učení bayesovského filtru, běh na pozadí updateru, jediná instance."""

import contextlib
import io
import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import bayes  # noqa: E402
import db  # noqa: E402
import instance  # noqa: E402
import updater  # noqa: E402
from fake_imap import FakeIMAP, FakePool, make_raw  # noqa: E402


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class TokensTest(unittest.TestCase):
    def test_rich_message(self):
        raw = (b"From: =?utf-8?q?Super_Shop?= <info@mail.shop.example.com>\r\n"
               b"Reply-To: podvod@jinde.cz\r\nTo: a@x.cz, b@x.cz\r\nCc: c@x.cz\r\n"
               b"Subject: VYHRAJTE AUTO!\r\nX-Mailer: Mailchimp 2.0\r\nList-Unsubscribe: <mailto:u@x>\r\n"
               b"Precedence: bulk\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
               b"--B\r\nContent-Type: text/html; charset=nonexistent\r\n\r\n"
               b"<p>Klikn\xc4\x9bte <a href=\"https://track.evil.example.org/x\">zde</a><img src=x><img src=y>"
               b" supercalifragilisticexpialidocious</p>\r\n"
               b"--B\r\nContent-Type: application/zip\r\nContent-Disposition: attachment; filename=\"faktura.ZIP\"\r\n\r\n"
               b"PK\r\n--B--\r\n")
        t = bayes.message_tokens(raw)
        for tok in ("subj:vyhrajte", "subj:ALLCAPS", "subj:exclaim", "from:info@mail.shop.example.com",
                    "fromdom:example.com", "fromname:super", "replyto:otherdomain", "rcpt:2-4", "mailer:mailchimp",
                    "hdr:list-unsubscribe", "precedence:bulk", "ctype:multipart/mixed", "part:application/zip",
                    "attach:zip", "url:track.evil.example.org", "url:example.org", "body:htmlonly", "img:2-4",
                    "skip:s30"):
            self.assertIn(tok, t)

    def test_plain_with_url(self):
        t = bayes.message_tokens(make_raw(body="Viz https://www.firma.cz/a a ok to je"))
        self.assertIn("url:firma.cz", t)
        self.assertIn("img:0", t)
        self.assertNotIn("ok", t)  # krátká slova se vynechají
        self.assertIsNone(bayes.raw_tokens(None))

    def test_fetch_tokens(self):
        M = FakeIMAP()
        M.add("INBOX", make_raw(subject="Casino"))
        M.select('"INBOX"')
        self.assertIn("subj:casino", bayes.fetch_tokens(M, [1])[1])
        M.fail["uid FETCH"] = "NO"
        self.assertEqual(bayes.fetch_tokens(M, [1]), {})


class DatabaseOpenRetryTest(unittest.TestCase):
    """Po instalaci nové verze drží soubory databáze chvíli antivirus – start to musí přečkat."""

    def test_transient_readonly_is_retried(self):
        real, calls = db.sqlite3.connect, []

        def connect(*a, **kw):
            calls.append(1)
            if len(calls) <= 2:
                raise db.sqlite3.OperationalError("attempt to write a readonly database")
            return real(*a, **kw)
        path = Path(tempfile.mkdtemp()) / "m.db"
        with mock.patch.object(db.sqlite3, "connect", connect), mock.patch.object(db.time, "sleep") as sleep:
            d = db.Database(path)
        self.assertEqual((len(calls), sleep.call_count), (3, 2))
        self.assertIsNotNone(d._conn.execute("SELECT 1 FROM meta").fetchone())

    def test_gives_up_after_deadline_and_on_other_errors(self):
        path = Path(tempfile.mkdtemp()) / "m.db"
        err = db.sqlite3.OperationalError("attempt to write a readonly database")
        with mock.patch.object(db.sqlite3, "connect", side_effect=err), mock.patch.object(db.time, "sleep"), \
                mock.patch.object(db, "OPEN_RETRY_FOR", 0), self.assertRaises(db.sqlite3.OperationalError):
            db.Database(path)
        with mock.patch.object(db.sqlite3, "connect", side_effect=db.sqlite3.OperationalError("no such table")) as c, \
                self.assertRaises(db.sqlite3.OperationalError):
            db.Database(path)
        self.assertEqual(c.call_count, 1)  # trvalá chyba se nezkouší znovu


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.d = db.Database(Path(tempfile.mkdtemp()) / "test.db")
        self.d.set_my_addresses("a", {"me@example.com"})
        self.f = bayes.BayesFilter(self.d)
        self.M = FakeIMAP((("INBOX", None), ("Spam", "junk"), ("Faktury", None)))
        self.acc = {"id": "a", "name": "A"}

    def test_bootstrap_learns_from_existing(self):
        import mail
        for i in range(3):
            self.M.add("Spam", make_raw(sender="spam@bad.cz", subject=f"Casino {i}", mid=f"<s{i}@x>"))
            self.M.add("INBOX", make_raw(sender="boss@work.cz", subject=f"Projekt {i}", mid=f"<i{i}@x>"),
                       ["\\Seen"])
            self.M.add("Faktury", make_raw(sender="billing@x.cz", subject=f"Faktura {i}", mid=f"<f{i}@x>"))
        self.d.add_spam_rule("spam@bad.cz")
        self.d.add_category_rule("notifications", sender="boss@work.cz")
        self.d.upsert_folders("a", mail.list_folders(self.M, with_counts=False))
        for folder in ("Spam", "INBOX", "Faktury"):
            self.M.select(mail.q(folder))
            self.d.insert_messages("a", folder, mail.fetch_header_records(self.M, self.M.uids(folder)))
        self.d.put_body("a", "INBOX", 1, self.M.folders["INBOX"]["msgs"][1]["raw"])  # slova bez stahování
        fid = self.d.create_user_folder("Faktury", "a", "Faktury", False, False)
        self.assertTrue(self.f.needs_bootstrap())
        store = mock.MagicMock()
        store.all.return_value = [self.acc]
        self.f.bootstrap(store, FakePool({"a": self.M}))
        self.assertEqual(self.f.counts(), {"spam": 3, "notifications": 3, f"folder:{fid}": 3})
        self.assertFalse(self.f.needs_bootstrap())
        self.assertEqual(self.f.bootstrap_state["status"], "done")
        st = self.f.status()
        self.assertEqual(st["classes"]["spam"], 3)
        self.f.forget_class(f"folder:{fid}")
        self.assertNotIn(f"folder:{fid}", self.f.counts())
        self.f.reset()
        self.assertEqual(self.f.counts(), {})

    def test_bootstrap_runs_once(self):
        self.f._bootstrap_lock.acquire()
        self.f.bootstrap(mock.MagicMock(), None)  # už běží – nic
        self.f._bootstrap_lock.release()

    def test_legacy_migration(self):
        c = self.d._conn
        c.executescript("""CREATE TABLE bayes_trained(key TEXT, is_spam INTEGER, tokens TEXT, created REAL);
                           CREATE TABLE bayes_messages(key TEXT, account_id TEXT, tokens TEXT, score REAL,
                                                       moved INTEGER, created REAL);""")
        c.execute("INSERT INTO bayes_trained VALUES('k1', 1, '[\"casino\"]', 0)")
        c.execute("INSERT INTO bayes_trained VALUES('k2', 0, '[\"meeting\"]', 0)")
        c.execute("INSERT INTO bayes_messages VALUES('k3', 'a', '[]', 0.99, 1, 0)")
        f = bayes.BayesFilter(self.d)
        self.assertEqual(f.counts(), {"spam": 1, "inbox": 1})
        self.assertEqual(self.d._q("SELECT target FROM bf_auto WHERE key='k3'")[0][0], "spam")


class UpdaterBackgroundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.exe = self.tmp / "NormanMail.exe"
        self.exe.write_bytes(b"exe")
        self.patches = [mock.patch.object(updater, "frozen", lambda: True),
                        mock.patch.object(updater, "exe_path", lambda: self.exe)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        updater._state.update(status="idle", version=None, notes="", error=None, checked=None)
        updater._restart = False

    def test_start_loop_checks_once(self):
        calls = []

        class Stop(BaseException):
            pass

        def fake_sleep(s):
            if s == 3600:
                raise Stop
        with mock.patch.object(updater.threading, "Thread") as T, \
                mock.patch.object(updater, "check", side_effect=lambda: calls.append(1)), \
                mock.patch.object(updater.time, "sleep", fake_sleep):
            updater.start(lambda: True)
            with self.assertRaises(Stop):
                T.call_args.kwargs["target"]()
        self.assertEqual(calls, [1])
        with mock.patch.object(updater, "frozen", lambda: False), mock.patch.object(updater.threading, "Thread") as T:
            updater.start(lambda: True)
            updater.cleanup()
        T.assert_not_called()

    def test_install_on_exit_and_relaunch(self):
        with contextlib.redirect_stdout(io.StringIO()):  # chyby se jen vypisují
            self._install_on_exit_and_relaunch()

    def _install_on_exit_and_relaunch(self):
        updater._state["status"] = "idle"
        updater.install_on_exit()  # nic ke stažení
        updater._state["status"] = "ready"
        with mock.patch.object(updater, "install", side_effect=updater.UpdateError("err.update.notReady")):
            updater.install_on_exit()  # chyba se jen vypíše
        with mock.patch.object(updater.subprocess, "Popen") as P:
            updater.relaunch()
            P.assert_not_called()
            updater._restart = True
            updater.relaunch()
            self.assertEqual(P.call_args[0][0][1], "--after-update")
            P.side_effect = OSError("denied")
            updater.relaunch()

    def test_cleanup_removes_old_files(self):
        for suffix in (".old", ".123.old", ".part"):
            (self.tmp / ("NormanMail.exe" + suffix)).write_bytes(b"x")
        with mock.patch.object(updater.threading, "Thread") as T:
            updater.cleanup()
            T.call_args.kwargs["target"]()
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["NormanMail.exe"])


@unittest.skipUnless(sys.platform == "win32", "jen Windows")
class InstanceTest(unittest.TestCase):
    def test_mutex(self):
        d = tempfile.mkdtemp()
        self.assertTrue(instance.acquire(d))
        self.assertFalse(instance.acquire(d))  # stejná data podruhé
        self.assertFalse(instance.wait_and_acquire(d, timeout=0))
        self.assertTrue(instance.wait_and_acquire(tempfile.mkdtemp(), timeout=0))

    def test_activate_missing_window(self):
        self.assertFalse(instance.activate_existing("Okno, které neexistuje 1234"))

    def test_cleanup_stale_bundles(self):
        tmp = Path(tempfile.mkdtemp())
        current = tmp / "_MEI1"
        ours, foreign, fresh, half = tmp / "_MEI2", tmp / "_MEI3", tmp / "_MEI4", tmp / "_MEI5.del"
        for d in (current, ours, foreign, fresh, half):
            d.mkdir()
        for d in (ours, fresh):
            (d / "static").mkdir()
            (d / "assets").mkdir()
            (d / "static" / "index.html").write_text("")
            (d / "assets" / "mailer.ico").write_text("")
        old = time.time() - 3600
        for d in (ours, foreign, half):
            os.utime(d, (old, old))
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", str(current), create=True):
            instance.cleanup_stale_bundles()
        self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["_MEI1", "_MEI3", "_MEI4"])
        instance.cleanup_stale_bundles()  # ze zdrojáků nic
        with mock.patch.object(instance.threading, "Thread") as T:
            instance.cleanup_in_background()
        T.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
