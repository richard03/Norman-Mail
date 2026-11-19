"""Přesun dat portable verze z dřívější podsložky data přímo do složky s exe."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402


class MigratePortableTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        (self.base / "portable.txt").write_text("x")
        old = self.base / "data"
        (old / "webview").mkdir(parents=True)
        (old / "accounts.json").write_text("{}")
        (old / "mailer.db").write_bytes(b"db")
        (old / "webview" / "state").write_text("s")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_moves_everything_and_removes_old_folder(self):
        self.assertEqual(store._migrate_portable(self.base), self.base)
        self.assertEqual((self.base / "mailer.db").read_bytes(), b"db")
        self.assertTrue((self.base / "accounts.json").is_file())
        self.assertTrue((self.base / "webview" / "state").is_file())
        self.assertFalse((self.base / "data").exists())

    def test_existing_data_in_base_is_left_alone(self):
        (self.base / "accounts.json").write_text('{"new": 1}')
        self.assertEqual(store._migrate_portable(self.base), self.base)
        self.assertEqual((self.base / "accounts.json").read_text(), '{"new": 1}')
        self.assertTrue((self.base / "data" / "mailer.db").is_file())  # nic se nepřesouvalo

    def test_fresh_folder(self):
        shutil.rmtree(self.base / "data")
        self.assertEqual(store._migrate_portable(self.base), self.base)


if __name__ == "__main__":
    unittest.main()
