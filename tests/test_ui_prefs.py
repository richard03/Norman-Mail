"""Rozvržení okna (šířky sloupců) se ukládá do window.json spolu s polohou okna – ne do databáze
ani jen do úložiště WebView2 (to při ukončení aplikace poslední zápisy ztrácí)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import layout  # noqa: E402

import app as app_module  # noqa: E402


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.file = Path(tempfile.mkdtemp()) / "window.json"
        self.patch = mock.patch.object(layout, "WINDOW_FILE", self.file)
        self.patch.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.patch.stop()

    def put(self, body):
        return self.client.put("/api/ui", json=body, headers={"X-Mailer": "1"}, base_url="http://127.0.0.1")

    def get(self):
        return self.client.get("/api/ui", base_url="http://127.0.0.1").get_json()

    def test_empty_by_default(self):
        self.assertEqual(self.get(), {})

    def test_columns_saved_and_validated(self):
        self.put({"columns": {"sidebar": 345.4, "list": 279, "context": 5, "evil": 300, "reader": "x"}})
        self.assertEqual(self.get(), {"columns": {"sidebar": 345, "list": 279}})
        self.assertEqual(json.loads(self.file.read_text("utf-8"))["columns"], {"sidebar": 345, "list": 279})

    def test_window_geometry_and_columns_keep_each_other(self):
        """main.py při zavření okna ukládá polohu, app.py za běhu šířky – žádný zápis nesmaže ten druhý."""
        layout.update(x=10, y=20, width=1600, height=900, maximized=False)
        self.put({"columns": {"sidebar": 200, "list": 300, "context": 250}})
        layout.update(x=11, y=21, width=1500, height=800, maximized=True)
        data = json.loads(self.file.read_text("utf-8"))
        self.assertEqual(data["columns"], {"sidebar": 200, "list": 300, "context": 250})
        self.assertEqual((data["x"], data["width"], data["maximized"]), (11, 1500, True))

    def test_broken_file_is_ignored(self):
        self.file.write_text("{nedopsan", "utf-8")
        self.assertEqual(self.get(), {})
        self.put({"columns": {"list": 300}})
        self.assertEqual(self.get(), {"columns": {"list": 300}})


if __name__ == "__main__":
    unittest.main()
