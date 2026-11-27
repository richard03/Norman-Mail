"""main.py: kontrola navigace a odkazů, ukládání příloh/zpráv přes most pywebview; taskbar: cíl spuštění."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import main  # noqa: E402
import taskbar  # noqa: E402
from fake_imap import FakeIMAP, FakePool  # noqa: E402

RAW = (b"From: a@x.cz\r\nSubject: Faktura\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
       b"--B\r\nContent-Type: text/plain\r\n\r\nText\r\n"
       b"--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=f.pdf\r\n\r\nPDF1\r\n"
       b"--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=f.pdf\r\n\r\nPDF2\r\n--B--\r\n")


class NavigationTest(unittest.TestCase):
    def test_open_external_only_safe_schemes(self):
        with mock.patch.object(main.webbrowser, "open", return_value=True) as o:
            self.assertTrue(main.open_external("https://example.com"))
            self.assertTrue(main.Api().open_external("mailto:a@b.cz"))
            for url in ("file:///C:/Windows/system32/calc.exe", "ms-settings:", "javascript:alert(1)"):
                self.assertFalse(main.open_external(url))
        self.assertEqual(o.call_count, 2)

    def test_navigation_rules(self):
        self.assertTrue(main.navigation_allowed(main.APP_URL))
        self.assertTrue(main.navigation_allowed("about:blank"))
        self.assertFalse(main.navigation_allowed("https://mailer-app.example.evil.com/"))
        self.assertTrue(main.frame_navigation_allowed("about:srcdoc"))
        self.assertFalse(main.frame_navigation_allowed(main.APP_URL))

    def test_guard_cancels(self):
        handler = main._guard(main.navigation_allowed)
        ok, bad, broken = (SimpleNamespace(Uri=main.APP_URL, Cancel=False), SimpleNamespace(Uri="https://x", Cancel=False),
                           SimpleNamespace(Uri=None, Cancel=False))
        for args in (ok, bad):
            handler(None, args)
        main._guard(lambda uri: 1 / 0)(None, broken)
        self.assertEqual((ok.Cancel, bad.Cancel, broken.Cancel), (False, True, True))


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_free_path(self):
        p = self.dir / "a.pdf"
        self.assertEqual(main.free_path(p), p)
        p.write_text("")
        (self.dir / "a (2).pdf").write_text("")
        self.assertEqual(main.free_path(p).name, "a (3).pdf")

    @unittest.skipUnless(sys.platform == "win32", "alternativní datové proudy jen na NTFS")
    def test_mark_from_internet(self):
        p = self.dir / "f.pdf"
        p.write_bytes(b"x")
        main.mark_from_internet(p)
        try:
            with open(f"{p}:Zone.Identifier", "rb") as f:
                self.assertIn(b"ZoneId=3", f.read())
        except OSError:
            self.skipTest("disk bez alternativních datových proudů")
        main.mark_from_internet(self.dir / "neni" / "x")  # chyba se ignoruje


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.M = FakeIMAP()
        self.uid = self.M.add("INBOX", RAW)
        acc = {"id": "a", "name": "A", "email": "me@example.com"}
        self.patches = [mock.patch.object(main.server_app.store, "get", lambda i: acc if i == "a" else None),
                        mock.patch.object(main.server_app, "pool", FakePool({"a": self.M})),
                        mock.patch.object(main.server_app.database, "get_body", lambda *a: None)]
        for p in self.patches:
            p.start()
        self.api = main.Api()
        self.window = mock.MagicMock()
        self.api._window = self.window

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_request(self):
        self.assertEqual(self.api.request("GET", "/api/info")["status"], 200)
        self.assertEqual(self.api.request("GET", "/static/index.html")["status"], 400)
        self.assertEqual(self.api.request("PATCH", "/api/info")["status"], 400)

    def test_save_attachment(self):
        self.window.create_file_dialog.return_value = [str(self.dir / "out.pdf")]
        res = self.api.save_attachment("a", "INBOX", self.uid, 1)
        self.assertTrue(res["ok"])
        self.assertEqual((self.dir / "out.pdf").read_bytes(), b"PDF1")
        self.window.create_file_dialog.return_value = None
        self.assertEqual(self.api.save_attachment("a", "INBOX", self.uid, 1), {"ok": False})
        self.assertIn("error", self.api.save_attachment("neni", "INBOX", self.uid, 1))
        self.assertIn("error", self.api.save_attachment("a", "INBOX", 999, 1))

    def test_save_message_and_all_attachments(self):
        self.window.create_file_dialog.return_value = str(self.dir / "zprava.eml")
        self.assertTrue(self.api.save_message("a", "INBOX", self.uid, "eml")["ok"])
        self.assertEqual((self.dir / "zprava.eml").read_bytes(), RAW)
        self.assertIn("error", self.api.save_message("a", "INBOX", self.uid, "pdf"))
        self.window.create_file_dialog.return_value = None
        self.assertEqual(self.api.save_message("a", "INBOX", self.uid, "txt"), {"ok": False})
        self.assertEqual(self.api.save_attachments("a", "INBOX", self.uid), {"ok": False})
        self.window.create_file_dialog.return_value = str(self.dir)
        res = self.api.save_attachments("a", "INBOX", self.uid)
        self.assertEqual(res["count"], 2)
        self.assertEqual(sorted(p.name for p in self.dir.glob("f*.pdf")), ["f (2).pdf", "f.pdf"])
        no_att = self.M.add("INBOX", b"Subject: x\r\n\r\ntext")
        self.assertIn("error", self.api.save_attachments("a", "INBOX", no_att))
        self.assertIn("error", self.api.save_attachments("a", "INBOX", 999))


class TaskbarTest(unittest.TestCase):
    def test_launch_target(self):
        exe, args, icon = taskbar.launch_target()
        self.assertIn("main.py", args)
        self.assertTrue(icon.endswith("mailer.ico,0"))
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(taskbar.launch_target(), (sys.executable, "", sys.executable + ",0"))

    @unittest.skipUnless(sys.platform == "win32", "jen Windows")
    def test_shortcut_path(self):
        where = next(iter(taskbar.CSIDL))
        self.assertTrue(taskbar.shortcut_path(where, "Norman Mail").endswith("Norman Mail.lnk"))


if __name__ == "__main__":
    unittest.main()
