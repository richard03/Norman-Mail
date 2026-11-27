"""store.py (účty, hesla, validace) a google_oauth.py (přihlášení přes Google, tokeny)."""

import base64
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import google_oauth  # noqa: E402
import store  # noqa: E402

VALID = {"email": "eva@example.com", "imap_host": "imap.example.com", "imap_port": "993",
         "smtp_host": "smtp.example.com", "smtp_port": 465}


class NormalizeTest(unittest.TestCase):
    def test_defaults(self):
        acc = store.normalize({**VALID, "imap_security": "nesmysl", "auth": "x"})
        self.assertEqual((acc["imap_port"], acc["imap_security"], acc["smtp_security"], acc["auth"]),
                         (993, "ssl", "ssl", "password"))
        self.assertEqual((acc["username"], acc["name"]), ("eva@example.com", "eva@example.com"))

    def test_errors(self):
        for bad in ({**VALID, "email": ""}, {**VALID, "email": "bez-zavinace"}, {**VALID, "imap_port": "abc"},
                    {**VALID, "smtp_port": 70000}):
            with self.assertRaises(store.ValidationError, msg=bad):
                store.normalize(bad)


class FakeKeyring:
    def __init__(self):
        self.data = {}

    def set_password(self, service, key, pw):
        self.data[(service, key)] = pw

    def get_password(self, service, key):
        return self.data.get((service, key))

    def delete_password(self, service, key):
        del self.data[(service, key)]


class AccountStoreTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(store, "DATA_DIR", d), mock.patch.object(store, "FILE", d / "accounts.json")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_keyring(self):
        kr = FakeKeyring()
        with mock.patch.object(store, "keyring", kr), mock.patch.object(store, "KEYRING_OK", True):
            s = store.AccountStore()
            acc_id = s.save(None, store.normalize(VALID), "tajné")
            self.assertNotIn("tajné", store.FILE.read_text("utf-8"))
            self.assertEqual(s.get(acc_id)["password"], "tajné")
            self.assertEqual(store.AccountStore().find_by_email("EVA@example.com")["id"], acc_id)  # znovu načtený
            s.save(acc_id, store.normalize({**VALID, "name": "Práce"}), "nové")
            self.assertEqual(s.list_public()[0]["name"], "Práce")
            self.assertEqual([a["password"] for a in s.all()], ["nové"])
            s.delete(acc_id)
            s.delete(acc_id)  # podruhé nic
            self.assertEqual(kr.data, {})
            self.assertIsNone(s.get(acc_id))
            self.assertIsNone(s.find_by_email("eva@example.com"))

    def test_without_keyring_and_broken_keyring(self):
        with mock.patch.object(store, "KEYRING_OK", False):
            s = store.AccountStore()
            acc_id = s.save(None, store.normalize(VALID), "pw")
            self.assertEqual(s.get(acc_id)["password"], "pw")
        broken = mock.MagicMock()
        broken.get_password.side_effect = RuntimeError
        broken.delete_password.side_effect = RuntimeError
        s._accounts[0]["password_in_keyring"] = True
        with mock.patch.object(store, "keyring", broken):
            self.assertEqual(s.get(acc_id)["password"], "")
            s.delete(acc_id)
        self.assertEqual(s.list_public(), [])


class DataDirTest(unittest.TestCase):
    def test_env_and_portable(self):
        with mock.patch.dict(os.environ, {"MAILER_DATA": "X:/data"}):
            self.assertEqual(store._data_dir(), Path("X:/data"))
        base = Path(tempfile.mkdtemp())
        (base / store.PORTABLE_MARKER).write_text("")
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", str(base / "NormanMail.exe")):
            self.assertEqual(store.portable_dir(), base.resolve())
            with mock.patch.dict(os.environ, {"MAILER_DATA": ""}):
                self.assertEqual(store._data_dir(), base.resolve())
        self.assertIsNone(store.portable_dir())

    def test_appdata_copies_legacy(self):
        appdata = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"MAILER_DATA": "", "APPDATA": str(appdata)}), \
                mock.patch.object(store, "portable_dir", return_value=None):
            self.assertEqual(store._data_dir(), appdata / "Mailer")


# --------------------------------------------------------------------------

def id_token(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).rstrip(b"=").decode()
    return f"h.{payload}.s"


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body):
    return urllib.error.HTTPError(google_oauth.TOKEN_URL, code, "err", {}, io.BytesIO(body))


CFG = {"client_id": "x.apps.googleusercontent.com", "client_secret": "s"}


class OAuthTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(google_oauth, "DATA_DIR", d),
                        mock.patch.object(google_oauth, "CONFIG_FILE", d / "google_client.json"),
                        mock.patch.object(google_oauth, "_builtin_file", return_value=d / "neni.json")]
        for p in self.patches:
            p.start()
        google_oauth._pending.clear()
        google_oauth._tokens.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_config(self):
        self.assertIsNone(google_oauth.load_config())
        with self.assertRaises(google_oauth.OAuthError):
            google_oauth.start("http://127.0.0.1/cb", {})
        with self.assertRaises(google_oauth.OAuthError):
            google_oauth.save_config("spatne-id", "s")
        with self.assertRaises(google_oauth.OAuthError):
            google_oauth.save_config(CFG["client_id"], " ")
        google_oauth.save_config(CFG["client_id"], "s")
        self.assertEqual(google_oauth.load_config()["client_id"], CFG["client_id"])

    def test_builtin_file_from_source(self):
        self.patches[2].stop()
        try:
            with mock.patch.object(google_oauth, "build_client_file", return_value=Path("b.json")):
                self.assertEqual(google_oauth._builtin_file(), Path("b.json"))
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "_MEIPASS", "C:/meipass", create=True):
                self.assertEqual(google_oauth._builtin_file(), Path("C:/meipass") / google_oauth.BUILTIN_NAME)
        finally:
            self.patches[2].start()

    def test_flow(self):
        google_oauth.save_config(**CFG)
        url, state = google_oauth.start("http://127.0.0.1/cb", {"email": "eva@gmail.com"})
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(params["login_hint"], ["eva@gmail.com"])
        self.assertEqual(params["state"], [state])
        self.assertTrue(google_oauth.has_pending())
        entry = google_oauth.get_pending(state)
        self.assertIsNone(google_oauth.get_pending("neni"))

        ok = {"refresh_token": "r", "access_token": "a", "expires_in": 100, "scope": "https://mail.google.com/ email",
              "id_token": id_token("eva@gmail.com")}
        with mock.patch("urllib.request.urlopen", return_value=Resp(json.dumps(ok).encode())) as urlopen:
            res = google_oauth.exchange("kod", entry)
        sent = urllib.parse.parse_qs(urlopen.call_args[0][0].data.decode())
        self.assertEqual(sent["code_verifier"], [entry["verifier"]])
        self.assertEqual(res, {"email": "eva@gmail.com", "refresh_token": "r", "access_token": "a", "expires_in": 100})
        google_oauth.finish(state, account_id="acc")
        self.assertEqual(google_oauth.get_pending(state)["status"], "done")
        self.assertFalse(google_oauth.has_pending())

        for bad in ({**ok, "refresh_token": None}, {**ok, "scope": "email"}, {**ok, "id_token": "x"}):
            with mock.patch("urllib.request.urlopen", return_value=Resp(json.dumps(bad).encode())):
                with self.assertRaises(google_oauth.OAuthError):
                    google_oauth.exchange("kod", entry)

    def test_post_errors(self):
        cases = [(http_error(400, b'{"error": "invalid_grant"}'), "err.oauthRevoked"),
                 (http_error(401, b'{"error": "invalid_client"}'), "err.oauthInvalidClient"),
                 (http_error(500, b"not json"), "HTTP 500"),
                 (OSError("offline"), "offline")]
        for exc, expected in cases:
            with mock.patch("urllib.request.urlopen", side_effect=exc):
                with self.assertRaises(google_oauth.OAuthError) as cm:
                    google_oauth._post({})
            text = str(cm.exception)
            if expected.startswith("err."):
                self.assertEqual(text, google_oauth.tr(expected))
            else:
                self.assertIn(expected, text)

    def test_access_token_cache_and_refresh(self):
        google_oauth.save_config(**CFG)
        acc = {"id": "a", "username": "eva@gmail.com", "password": "refresh"}
        with self.assertRaises(google_oauth.OAuthError):
            google_oauth.access_token({**acc, "password": ""})
        with mock.patch.object(google_oauth, "_post", return_value={"access_token": "t1", "expires_in": 3600}) as p:
            self.assertEqual(google_oauth.access_token(acc), "t1")
            self.assertEqual(google_oauth.xoauth2_string(acc), "user=eva@gmail.com\x01auth=Bearer t1\x01\x01")
        p.assert_called_once()
        with mock.patch.object(google_oauth, "_post", return_value={"access_token": "t2"}):
            self.assertEqual(google_oauth.access_token({**acc, "password": "jiný"}), "t2")  # jiný refresh token


if __name__ == "__main__":
    unittest.main()
