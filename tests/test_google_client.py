import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import google_client  # noqa: E402
import google_oauth  # noqa: E402

CID = "123-abc.apps.googleusercontent.com"


class ParseClient(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(google_client.parse_client(json.dumps({"client_id": CID, "client_secret": "s"})),
                         {"client_id": CID, "client_secret": "s"})

    def test_console_download(self):
        text = json.dumps({"installed": {"client_id": CID, "client_secret": "s", "project_id": "x"}})
        self.assertEqual(google_client.parse_client(text), {"client_id": CID, "client_secret": "s"})

    def test_invalid(self):
        for text in ("", "nejson", "[]", json.dumps({"client_id": "x", "client_secret": "s"}),
                     json.dumps({"client_id": CID})):
            self.assertIsNone(google_client.parse_client(text), text)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"NORMANMAIL_GOOGLE_CLIENT": "C:/x/klient.json"}):
            self.assertEqual(google_client.build_client_file(), Path("C:/x/klient.json"))


class LoadConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.user = Path(self.dir.name) / "user.json"
        self.builtin = Path(self.dir.name) / "builtin.json"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(google_oauth, "CONFIG_FILE", self.user).start()
        mock.patch.object(google_oauth, "_builtin_file", lambda: self.builtin).start()

    def write(self, path, cid):
        path.write_text(json.dumps({"client_id": cid, "client_secret": "s"}), "utf-8")

    def test_none(self):
        self.assertIsNone(google_oauth.load_config())

    def test_builtin(self):
        self.write(self.builtin, CID)
        self.assertEqual(google_oauth.load_config()["client_id"], CID)

    def test_user_client_wins(self):
        """Vlastní klient má přednost – jeho tokeny by s vestavěným klientem nešly obnovit."""
        self.write(self.builtin, CID)
        self.write(self.user, "999-own.apps.googleusercontent.com")
        self.assertEqual(google_oauth.load_config()["client_id"], "999-own.apps.googleusercontent.com")

    def test_broken_user_file_falls_back(self):
        self.user.write_text("{", "utf-8")
        self.write(self.builtin, CID)
        self.assertEqual(google_oauth.load_config()["client_id"], CID)


if __name__ == "__main__":
    unittest.main()
