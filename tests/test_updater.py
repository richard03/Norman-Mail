"""Testy automatických aktualizací: podpis a obsah latest.json, stažení přes místní HTTP server, výměna exe."""

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ed25519  # noqa: E402
import updater  # noqa: E402

SECRET = bytes(range(32))
PUBLIC = ed25519.public_key(SECRET)


def make_release(folder: Path, version: str, exe: bytes, /, **override):
    (folder / updater.ASSET).write_bytes(exe)
    info = {"version": version, "sha256": hashlib.sha256(exe).hexdigest(), "size": len(exe), "notes": "Novinky"}
    info.update(override)
    body = json.dumps(info).encode()
    (folder / updater.MANIFEST).write_bytes(body)
    (folder / (updater.MANIFEST + ".sig")).write_text(ed25519.sign(SECRET, body).hex())
    return body


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.web = self.tmp / "web"
        self.app = self.tmp / "app"
        self.web.mkdir()
        self.app.mkdir()
        self.exe = self.app / "NormanMail.exe"
        self.exe.write_bytes(b"old version")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(self.web)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_address[1]
        self.patches = [
            mock.patch.object(updater, "PUBLIC_KEY", PUBLIC),
            mock.patch.object(updater, "VERSION", "1.0.0"),
            mock.patch.object(updater, "frozen", lambda: True),
            mock.patch.object(updater, "exe_path", lambda: self.exe),
            mock.patch.dict(os.environ, {"NORMANMAIL_UPDATE_URL": f"http://127.0.0.1:{port}"}),
        ]
        for p in self.patches:
            p.start()
        updater._state.update(status="idle", version=None, notes="", error=None, checked=None)
        updater._manifest = None
        updater._restart = False

    def tearDown(self):
        for p in self.patches:
            p.stop()
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def test_parse_version(self):
        self.assertEqual(updater.parse_version("1.2.3"), (1, 2, 3))
        self.assertGreater(updater.parse_version("1.10.0"), updater.parse_version("1.9.9"))
        for bad in ("", "1", "v1.2", "1.2.x", None, 1.2, "1.2.3.4.5"):
            self.assertIsNone(updater.parse_version(bad))

    def test_verify_manifest_rejects_bad_signature_and_content(self):
        body = make_release(self.web, "1.0.1", b"exe")
        sig = (self.web / "latest.json.sig").read_bytes()
        self.assertEqual(updater.verify_manifest(body, sig)["version"], "1.0.1")
        for b, s in [(body.replace(b"1.0.1", b"9.0.1"), sig), (body, b"zz"), (body, b"00" * 64)]:
            with self.assertRaises(updater.UpdateError) as cm:
                updater.verify_manifest(b, s)
            self.assertEqual(cm.exception.key, "err.update.badSignature")
        for bad in ({"version": "x"}, {"size": 0}, {"sha256": "abc"}, {"size": "3"}):
            body = make_release(self.web, "1.0.1", b"exe", **bad)
            with self.assertRaises(updater.UpdateError) as cm:
                updater.verify_manifest(body, (self.web / "latest.json.sig").read_bytes())
            self.assertEqual(cm.exception.key, "err.update.badManifest")

    def test_check_download_install(self):
        make_release(self.web, "1.0.1", b"new version" * 1000)
        updater.check()
        st = updater.status()
        self.assertEqual((st["status"], st["version"], st["notes"]), ("ready", "1.0.1", "Novinky"))
        self.assertEqual((self.app / "NormanMail.exe.new").read_bytes(), b"new version" * 1000)
        updater.install(restart=True)
        self.assertEqual(self.exe.read_bytes(), b"new version" * 1000)
        self.assertEqual((self.app / "NormanMail.exe.old").read_bytes(), b"old version")
        self.assertFalse((self.app / "NormanMail.exe.new").exists())
        self.assertEqual(updater.status()["status"], "installed")
        self.assertTrue(updater._restart)

    def test_up_to_date_and_missing_release(self):
        make_release(self.web, "1.0.0", b"same")
        updater.check()
        self.assertEqual(updater.status()["status"], "current")
        for f in self.web.iterdir():
            f.unlink()
        updater.check()  # 404: zatím žádné vydání
        self.assertEqual(updater.status()["status"], "current")

    def test_tampered_exe_is_rejected(self):
        make_release(self.web, "1.0.1", b"genuine")
        (self.web / updater.ASSET).write_bytes(b"malware")  # podepsaný manifest, jiný exe
        updater.check()
        st = updater.status()
        self.assertEqual((st["status"], st["error"][0]), ("error", "err.update.badHash"))
        self.assertFalse((self.app / "NormanMail.exe.new").exists())
        self.assertFalse((self.app / "NormanMail.exe.part").exists())
        self.assertEqual(self.exe.read_bytes(), b"old version")
        with self.assertRaises(updater.UpdateError):
            updater.install(restart=False)

    def test_new_file_changed_before_install(self):
        make_release(self.web, "1.0.1", b"genuine")
        updater.check()
        (self.app / "NormanMail.exe.new").write_bytes(b"swapped")
        with self.assertRaises(updater.UpdateError) as cm:
            updater.install(restart=False)
        self.assertEqual(cm.exception.key, "err.update.badHash")
        self.assertEqual(self.exe.read_bytes(), b"old version")

    def test_offline(self):
        self.server.shutdown()
        self.server.server_close()
        updater.check()
        st = updater.status()
        self.assertEqual((st["status"], st["error"][0]), ("error", "err.update.network"))
        self.server = None


if __name__ == "__main__":
    unittest.main()
