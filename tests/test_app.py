"""app.py: HTTP endpointy (Flask test client) nad skutečnou databází a synchronizací s falešným IMAP serverem."""

import base64
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

import db  # noqa: E402
import google_oauth  # noqa: E402
import mail  # noqa: E402
import sync  # noqa: E402
import updater  # noqa: E402
from fake_imap import FakeIMAP, FakePool, make_raw  # noqa: E402

import app as app_module  # noqa: E402

FOLDERS = (("INBOX", None), ("Sent", "sent"), ("Trash", "trash"), ("Spam", "junk"), ("Archiv", None),
           ("Projekty", None))
OLD = "Mon, 02 Jan 2023 10:00:00 +0000"


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def account(acc_id, email):
    return {"id": acc_id, "name": f"Účet {acc_id}", "email": email, "username": email, "display_name": "",
            "imap_host": "imap.example.com", "imap_port": 993, "imap_security": "ssl",
            "smtp_host": "smtp.example.com", "smtp_port": 465, "smtp_security": "ssl",
            "auth": "password", "password": "heslo"}


class FakeStore:
    def __init__(self, *accounts):
        self.accounts = {a["id"]: dict(a) for a in accounts}

    def get(self, acc_id):
        a = self.accounts.get(acc_id)
        return dict(a) if a else None

    def all(self):
        return [dict(a) for a in self.accounts.values()]

    def list_public(self):
        return [{k: v for k, v in a.items() if k != "password"} for a in self.accounts.values()]

    def find_by_email(self, address):
        return next((dict(a) for a in self.accounts.values() if a["email"].lower() == address.lower()), None)

    def save(self, acc_id, acc, password):
        acc_id = acc_id or f"n{len(self.accounts)}"
        self.accounts[acc_id] = {**acc, "id": acc_id, "password": password}
        return acc_id

    def delete(self, acc_id):
        self.accounts.pop(acc_id, None)


class AppBase(unittest.TestCase):
    def setUp(self):
        self.d = db.Database(Path(tempfile.mkdtemp()) / "test.db")
        self.A = account("a", "me@example.com")
        self.B = account("b", "druhy@example.com")
        self.store = FakeStore(self.A, self.B)
        self.M, self.N = FakeIMAP(FOLDERS), FakeIMAP(FOLDERS)
        self.pool = FakePool({"a": self.M, "b": self.N})
        self.syncer = sync.SyncManager(self.store, self.d, self.pool)
        self.d.set_my_addresses("a", {"me@example.com"})
        self.d.set_my_addresses("b", {"druhy@example.com"})
        self.patches = [
            mock.patch.object(app_module, "database", self.d),
            mock.patch.object(app_module, "store", self.store),
            mock.patch.object(app_module, "syncer", self.syncer),
            mock.patch.object(app_module, "pool", self.pool),
            mock.patch.object(app_module, "bg_pool", self.pool),
            mock.patch.object(self.syncer, "bayes_learn_async", lambda *a, **k: None),
            mock.patch.object(self.syncer, "start_bayes_bootstrap", lambda: None),
        ]
        for p in self.patches:
            p.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def req(self, method, url, body=None, status=200, **kw):
        headers = {"X-Mailer": "1"} if method != "get" else {}
        r = getattr(self.client, method)(url, json=body, headers=headers, base_url="http://127.0.0.1", **kw)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True))
        return r.get_json() if r.is_json else r

    def get(self, url, status=200, **kw):
        return self.req("get", url, status=status, **kw)

    def post(self, url, body=None, status=200):
        return self.req("post", url, body or {}, status)

    def put(self, url, body=None, status=200):
        return self.req("put", url, body or {}, status)

    def delete(self, url, status=200):
        return self.req("delete", url, None, status)

    def sync_all(self):
        for acc, M in ((self.A, self.M), (self.B, self.N)):
            self.syncer.refresh_folders(acc)
            for name in list(M.folders):
                self.syncer.sync_folder(acc, name)


class GuardTest(AppBase):
    def test_guard_and_headers(self):
        self.assertEqual(self.client.get("/api/accounts", base_url="http://evil.com").status_code, 403)
        self.assertEqual(self.client.post("/api/sync", base_url="http://127.0.0.1").status_code, 403)
        r = self.client.post("/api/sync", headers={"X-Mailer": "1", "Origin": "http://evil.com"},
                             base_url="http://127.0.0.1")
        self.assertEqual(r.status_code, 403)
        r = self.client.get("/api/info", base_url="http://localhost")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(self.client.get("/", base_url="http://127.0.0.1").status_code, 302)

    def test_unexpected_error_is_502(self):
        with mock.patch.object(self.store, "list_public", side_effect=RuntimeError("boom")):
            self.assertIn("boom", self.get("/api/accounts", status=502)["error"])
        self.assertEqual(self.get("/api/accounts/neni/folders", status=404)["error"], app_module.tr("err.accountMissing"))
        self.get("/api/accounts/a/messages", status=400)  # chybí folder


class AppInfoTest(AppBase):
    def test_info_and_update(self):
        self.assertEqual(len(self.get("/api/accounts")), 2)
        self.assertIn("keyring", self.get("/api/info"))
        self.assertIn("version", self.get("/api/app"))
        with mock.patch.object(app_module, "EMBEDDED", True), \
                mock.patch.object(app_module.taskbar, "shortcut_path", side_effect=OSError):
            self.assertEqual(self.get("/api/app")["shortcuts"], {})
        st = {"status": "idle", "version": None, "error": ("err.updateNetwork", {"error": "x"})}
        with mock.patch.object(updater, "status", return_value=dict(st)):
            res = self.get("/api/update")
            self.assertTrue(res["error"])
            self.assertTrue(res["auto"])
        with mock.patch.object(updater, "check_async") as c:
            self.post("/api/update/check")
        c.assert_called_once()

    def test_update_install(self):
        self.post("/api/update/install", status=400)
        with mock.patch.object(app_module, "EMBEDDED", True), mock.patch.object(app_module, "QUIT", lambda: None):
            with mock.patch.object(updater, "install", side_effect=updater.UpdateError("err.updateNotReady")):
                self.post("/api/update/install", status=409)
            with mock.patch.object(updater, "install"), mock.patch.object(app_module.threading, "Timer") as T:
                self.post("/api/update/install")
            T.return_value.start.assert_called_once()

    def test_shortcut_and_folders(self):
        self.post("/api/app/shortcut", {"where": "desktop"}, status=400)
        with mock.patch.object(app_module, "EMBEDDED", True):
            where = next(iter(app_module.taskbar.CSIDL))
            with mock.patch.object(app_module.taskbar, "create_shortcut") as cs:
                self.post("/api/app/shortcut", {"where": where})
            cs.assert_called_once()
            with mock.patch.object(app_module.taskbar, "create_shortcut", side_effect=OSError("denied")):
                self.post("/api/app/shortcut", {"where": where}, status=500)
            with mock.patch.object(app_module.os, "startfile", create=True) as sf:
                self.post("/api/app/data-folder")
                self.post("/api/i18n/folder")
            self.assertEqual(sf.call_count, 2)
        self.post("/api/app/data-folder", status=400)

    def test_settings_ui_i18n_bayes(self):
        res = self.put("/api/settings", {"spam_delete_after_week": True, "language": "xx-neni"})
        self.assertTrue(res["spam_delete_after_week"])
        self.assertTrue(self.get("/api/settings")["spam_delete_after_week"])
        with mock.patch.object(app_module.layout, "load", return_value={"columns": {"list": 300}}), \
                mock.patch.object(app_module.layout, "update") as up:
            self.assertEqual(self.put("/api/ui", {"columns": {"list": 300, "x": 5, "sidebar": 50}}),
                             {"columns": {"list": 300}})
        up.assert_called_once_with(columns={"list": 300})
        cat = self.get("/api/i18n?reload=1")
        self.assertIn("catalog", cat)
        self.assertIn("classes", self.get("/api/bayes"))
        self.assertIn("classes", self.post("/api/bayes/reset"))

    def test_sync_status(self):
        with mock.patch.object(updater, "status", return_value={"status": "idle", "version": None, "error": None}):
            res = self.get("/api/sync/status")
        self.assertIn("unread", res)
        self.post("/api/sync")


class AccountsTest(AppBase):
    NEW = {"email": "novy@example.com", "imap_host": "imap.example.com", "imap_port": 993,
           "smtp_host": "smtp.example.com", "smtp_port": 465, "password": "pw"}

    def test_create_update_delete(self):
        with mock.patch.object(mail, "test_account") as t:
            acc_id = self.post("/api/accounts", self.NEW)["id"]
            t.assert_called_once()
            self.post("/api/accounts", {**self.NEW, "password": ""}, status=400)
            # bez hesla na jiný server – heslo se znovu nepoužije
            self.put(f"/api/accounts/{acc_id}", {**self.NEW, "password": "", "imap_host": "jiny.cz"}, status=400)
            self.put(f"/api/accounts/{acc_id}", {**self.NEW, "password": "", "name": "Nové jméno"})
            self.assertEqual(self.store.get(acc_id)["password"], "pw")
            self.put(f"/api/accounts/{acc_id}", {**self.NEW, "imap_host": "jiny.cz"})  # jiná schránka – cache pryč
            self.put("/api/accounts/neni", self.NEW, status=404)
        oauth = account("g", "eva@gmail.com")
        oauth["auth"] = "oauth2"
        self.store.accounts["g"] = oauth
        with mock.patch.object(mail, "test_account"):
            self.put("/api/accounts/g", {**self.NEW, "imap_host": "evil.com"})
        self.assertEqual(self.store.get("g")["imap_host"], "imap.gmail.com")
        self.delete(f"/api/accounts/{acc_id}")
        self.assertIsNone(self.store.get(acc_id))


class OAuthTest(AppBase):
    def test_config_and_start(self):
        with mock.patch.object(google_oauth, "load_config", return_value={"client_id": "x"}):
            self.assertEqual(self.get("/api/google/config"), {"configured": True, "client_id": "x"})
        with mock.patch.object(google_oauth, "save_config") as sc:
            self.post("/api/google/config", {"client_id": "a", "client_secret": "b"})
        sc.assert_called_once_with("a", "b")
        with mock.patch.object(google_oauth, "start", return_value=("https://g", "st")) as st:
            self.assertEqual(self.post("/api/oauth/start", {"account_id": "a", "email": "x"}),
                             {"url": "https://g", "state": "st"})
        self.assertEqual(st.call_args[0][1]["target_id"], "a")
        self.assertIn("8765", app_module.redirect_uri())
        self.assertEqual(self.get("/api/oauth/status?state=neni")["status"], "error")
        with mock.patch.object(google_oauth, "get_pending",
                               return_value={"status": "done", "error": None, "account_id": "a"}):
            self.assertEqual(self.get("/api/oauth/status?state=x")["account_id"], "a")

    def callback(self, entry, query="?state=st&code=c", tok=None):
        tok = tok or {"email": "eva@gmail.com", "refresh_token": "r", "access_token": "t", "expires_in": 60}
        with mock.patch.object(google_oauth, "get_pending", return_value=entry), \
                mock.patch.object(google_oauth, "exchange", return_value=tok), \
                mock.patch.object(google_oauth, "finish") as fin, \
                mock.patch.object(mail, "test_account"):
            r = self.client.get("/oauth/callback" + query, base_url="http://127.0.0.1")
        return r.get_data(as_text=True), fin

    def test_callback(self):
        entry = {"status": "pending", "target_id": None, "name": "", "display_name": "", "redirect_uri": "u",
                 "verifier": "v"}
        html, _ = self.callback(None)
        self.assertIn(app_module.tr("oauth.invalidRequest"), html)
        html, fin = self.callback(entry, "?state=st&error=access_denied")
        self.assertEqual(fin.call_args.kwargs["error"], app_module.tr("oauth.cancelled"))
        html, fin = self.callback(entry)
        new_id = fin.call_args.kwargs["account_id"]
        self.assertEqual(self.store.get(new_id)["auth"], "oauth2")
        self.assertEqual(self.store.get(new_id)["password"], "r")
        # přihlášení k jinému účtu Googlu, než se opravuje
        html, fin = self.callback({**entry, "target_id": "a"})
        self.assertIn("eva@gmail.com", fin.call_args.kwargs["error"])


class MessagesTest(AppBase):
    def setUp(self):
        super().setUp()
        self.u1 = self.M.add("INBOX", make_raw(sender="eva@x.cz", subject="Projekt", mid="<1@x>",
                                               body="Ahoj, posílám návrh."))
        self.u2 = self.M.add("INBOX", make_raw(sender="petr@y.cz", subject="Oběd", mid="<2@x>", date=OLD),
                             ["\\Seen"])
        self.M.add("Sent", make_raw(sender="me@example.com", to="eva@x.cz", subject="Re: Projekt", mid="<3@x>",
                                    extra="In-Reply-To: <1@x>"))
        self.M.add("Trash", make_raw(subject="Smazaná"), ["\\Seen"])
        self.M.add("Spam", make_raw(subject="Výhra"))
        self.M.add("Archiv", make_raw(subject="Archivní"))
        self.N.add("INBOX", make_raw(sender="eva@x.cz", to="druhy@example.com", subject="Druhý účet"))
        self.sync_all()

    def test_folders(self):
        names = [f["name"] for f in self.get("/api/accounts/a/folders")]
        self.assertEqual(names[0], "INBOX")
        self.d._q("DELETE FROM folders WHERE account_id='a'")
        self.assertIn("Projekty", [f["name"] for f in self.get("/api/accounts/a/folders")])  # ze serveru

    def test_lists(self):
        res = self.get("/api/accounts/a/messages?folder=INBOX")
        self.assertEqual(len(res["messages"]), 2)
        res = self.get("/api/unified?unread=1&keep_acc=a&keep_folder=INBOX&keep_uid=2&q=")
        self.assertEqual(len(res["messages"]), 3)
        self.get("/api/unified?filter=neni", status=400)
        for url, n in (("/api/trash", 1), ("/api/sent", 1), ("/api/spam", 1), ("/api/archive", 1)):
            self.assertEqual(len(self.get(url)["messages"]), n, url)
        with mock.patch.object(self.store, "all", return_value=[]):
            self.assertEqual(self.get("/api/unified")["messages"], [])
        self.syncer.errors["b"] = "chyba"
        self.assertTrue(self.get("/api/unified")["errors"])

    def test_message_detail_from_server_then_cache(self):
        self.d._q("UPDATE messages SET seen=0 WHERE uid=?", (self.u2,))
        data = self.get(f"/api/accounts/a/messages/{self.u1}?folder=INBOX")
        self.assertEqual(data["subject"], "Projekt")
        self.assertIn("\\Seen", self.M.flags("INBOX", self.u1))
        self.assertIsNotNone(self.d.get_body("a", "INBOX", self.u1))
        data = self.get(f"/api/accounts/a/messages/{self.u1}?folder=INBOX")  # z cache
        self.assertTrue(data["seen"])
        self.get(f"/api/accounts/a/messages/{self.u2}?folder=INBOX")
        # zpráva, která na serveru už není
        res = self.get("/api/accounts/a/messages/999?folder=INBOX", status=404)
        self.assertTrue(res["gone"])

    def test_message_not_in_db(self):
        u = self.M.add("INBOX", make_raw(subject="Nová"))
        data = self.get(f"/api/accounts/a/messages/{u}?folder=INBOX")
        self.assertEqual((data["notification"], data["placement"]), (False, None))

    def test_attachment_and_export(self):
        raw = (b"From: a@x.cz\r\nSubject: Faktura\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
               b"--B\r\nContent-Type: text/plain\r\n\r\nText\r\n--B\r\nContent-Type: application/pdf\r\n"
               b"Content-Disposition: attachment; filename=f.pdf\r\n\r\nPDF\r\n--B--\r\n")
        u = self.M.add("INBOX", raw)
        r = self.get(f"/api/accounts/a/messages/{u}/attachments/1?folder=INBOX")
        self.assertEqual(r.data, b"PDF")
        self.d.put_body("a", "INBOX", u, raw)
        self.assertEqual(self.get(f"/api/accounts/a/messages/{u}/attachments/1?folder=INBOX").data, b"PDF")
        r = self.get(f"/api/accounts/a/messages/{self.u1}/export?folder=INBOX&format=eml")
        self.assertIn("Projekt.eml", r.headers["Content-Disposition"])
        r = self.get(f"/api/accounts/a/messages/{self.u1}/export?folder=INBOX&format=txt")
        self.assertTrue(r.data.startswith("﻿".encode()))
        self.get(f"/api/accounts/a/messages/{self.u1}/export?folder=INBOX&format=pdf", status=400)

    def test_flags_delete_archive(self):
        self.post(f"/api/accounts/a/messages/{self.u1}/flags", {"folder": "INBOX", "flag": "\\Flagged", "value": True})
        self.assertIn("\\Flagged", self.M.flags("INBOX", self.u1))
        self.assertTrue(self.d.message("a", "INBOX", self.u1)["flagged"])
        self.post(f"/api/accounts/a/messages/{self.u1}/delete", {"folder": "INBOX"})
        self.assertNotIn(self.u1, self.M.uids("INBOX"))
        self.assertIsNone(self.d.message("a", "INBOX", self.u1))
        self.post(f"/api/accounts/a/messages/{self.u2}/archive", {"folder": "INBOX"})
        self.assertEqual(self.M.uids("INBOX"), [])
        self.assertEqual(len(self.M.uids("Archiv")), 2)

    def test_todo_and_snooze(self):
        res = self.post(f"/api/accounts/a/messages/{self.u1}/todo", {"folder": "INBOX"})
        self.assertEqual(res["view"], "todo")
        self.post(f"/api/accounts/a/messages/{self.u1}/todo", {"folder": "INBOX", "value": False})
        self.post(f"/api/accounts/a/messages/1/todo", {"folder": "Sent"}, status=400)
        until = time.time() + 3600
        self.post(f"/api/accounts/a/messages/{self.u1}/snooze", {"folder": "INBOX", "until": until})
        self.assertEqual(self.d.message("a", "INBOX", self.u1)["snoozed_until"], until)
        self.post(f"/api/accounts/a/messages/{self.u1}/snooze", {"folder": "INBOX", "until": "x"}, status=400)
        self.post(f"/api/accounts/a/messages/{self.u1}/snooze", {"folder": "INBOX", "until": 1}, status=400)
        u = self.M.add("INBOX", make_raw(mid="<new@x>"))  # není v databázi – Message-ID ze serveru
        self.post(f"/api/accounts/a/messages/{u}/snooze", {"folder": "INBOX", "until": until})
        self.post(f"/api/accounts/a/messages/{self.u1}/unsnooze", {"folder": "INBOX"})
        self.assertIsNone(self.d.message("a", "INBOX", self.u1)["snoozed_until"])

    def test_important_and_categorize(self):
        url = f"/api/accounts/a/messages/{self.u1}"
        self.post(f"{url}/important", {"folder": "INBOX", "scope": "x"}, status=400)
        self.assertEqual(self.post(f"{url}/important", {"folder": "INBOX", "scope": "release"})["view"], "important")
        res = self.post(f"{url}/categorize", {"folder": "INBOX", "category": "notifications", "scope": "sender"})
        self.assertEqual((res["sender"], res["view"]), ("eva@x.cz", "notifications"))
        self.assertEqual(self.get("/api/categories/notifications/rules")[0]["sender"], "eva@x.cz")
        self.post(f"{url}/categorize", {"folder": "INBOX", "category": "unimportant", "scope": "subject"})
        self.post(f"{url}/categorize", {"folder": "INBOX", "category": "unimportant", "scope": "custom",
                                        "rule": {"recipient": "me@example.com"}})
        self.post(f"{url}/categorize", {"folder": "INBOX", "category": "unimportant", "scope": "custom",
                                        "rule": {}}, status=400)
        self.post(f"{url}/categorize", {"folder": "INBOX", "category": "unimportant", "value": False})
        self.post(f"{url}/categorize", {"folder": "INBOX", "category": "neni"}, status=400)
        self.post("/api/accounts/a/messages/999/categorize", {"folder": "INBOX", "category": "unimportant"},
                  status=400)  # zpráva není ani na serveru

    def test_category_rules_and_spam_rules(self):
        self.d.add_category_rule("notifications", sender="a@x.cz")
        self.d.add_category_rule("notifications", sender="c@x.cz")
        rid = self.d._q("SELECT id FROM category_rules WHERE sender='a@x.cz'")[0][0]
        self.assertEqual(self.post("/api/categories/notifications/rules/preview", {"sender": "b@x.cz"})["count"], 0)
        self.post("/api/categories/notifications/rules/preview", {}, status=400)
        self.put(f"/api/categories/notifications/rules/{rid}", {"sender": "b@x.cz", "apply_existing": True})
        self.put(f"/api/categories/notifications/rules/{rid}", {"sender": "c@x.cz"}, status=400)  # už existuje
        self.put(f"/api/categories/notifications/rules/{rid}", {"sender": "spatne"}, status=400)
        self.put(f"/api/categories/notifications/rules/{rid}", {"subject": "x" * 201}, status=400)
        self.delete(f"/api/categories/notifications/rules/{rid}")
        self.get("/api/categories/neni/rules", status=404)
        self.assertEqual(self.post("/api/categories/notifications/mark-read")["count"], 0)
        sid = self.d.add_spam_rule("s@x.cz")
        other = self.d.add_spam_rule("t@x.cz")
        self.put(f"/api/spam-rules/{sid}", {"sender": "u@x.cz"})
        self.put(f"/api/spam-rules/{sid}", {"sender": "t@x.cz"}, status=400)  # už existuje
        self.put(f"/api/spam-rules/{sid}", {"sender": "spatne"}, status=400)
        self.put(f"/api/spam-rules/{sid}", {"sender": "me@example.com"}, status=400)  # vlastní adresa
        self.put(f"/api/spam-rules/{sid}", {"subject": "výhra", "subject_exact": True})
        self.assertEqual({(r["subject"], r["subject_exact"]) for r in self.get("/api/spam-rules")},
                         {("výhra", 1), ("", 0)})
        self.assertEqual(len(self.get("/api/spam-rules")), 2)
        self.delete(f"/api/spam-rules/{other}")

    def test_add_rules_manually(self):
        # Nedůležité / Notifikace: zařadí i zprávy, které už v doručené poště jsou
        rules = self.post("/api/categories/notifications/rules", {"sender": "Eva@x.cz"})
        self.assertEqual([r["sender"] for r in rules], ["eva@x.cz"])
        self.assertEqual(self.d.view_of("a", "INBOX", self.u1), "notifications")
        # jednoduché pravidlo se stejnou podmínkou v jiné kategorii se nahradí
        self.post("/api/categories/unimportant/rules", {"sender": "eva@x.cz"})
        self.assertEqual(self.get("/api/categories/notifications/rules"), [])
        self.post("/api/categories/unimportant/rules", {"sender": "eva@x.cz", "subject": "projekt"})
        self.assertEqual(len(self.get("/api/categories/unimportant/rules")), 2)
        # jen doména odesílatele (i s @ na začátku), celý předmět
        self.post("/api/categories/notifications/rules", {"sender": "@X.cz", "subject": "Re: Oběd", "subject_exact": True})
        r = next(r for r in self.get("/api/categories/notifications/rules") if r["sender"] == "x.cz")
        self.assertEqual((r["subject"], r["subject_exact"]), ("oběd", 1))
        self.post("/api/categories/notifications/rules", {"sender": "neni domena"}, status=400)
        self.post("/api/categories/unimportant/rules", {}, status=400)
        self.post("/api/categories/unimportant/rules", {"sender": "spatne"}, status=400)
        self.post("/api/categories/neni/rules", {"sender": "a@x.cz"}, status=404)
        # Spam: pravidlo a přesun zpráv, které už ve schránkách jsou (na pozadí)
        with mock.patch.object(app_module.threading, "Thread") as T:
            res = self.post("/api/spam-rules", {"sender": "Eva@x.cz"})
        self.assertEqual([r["sender"] for r in res["rules"]], ["eva@x.cz"])
        self.assertEqual(self.get("/api/categories/unimportant/rules")[0]["subject"], "projekt")  # složené zůstalo
        app_module._spam_sender_job(*T.call_args.kwargs["args"])
        self.assertEqual(self.get(f"/api/jobs/{res['job_id']}")["moved"], 2)  # z obou účtů
        self.post("/api/spam-rules", {"sender": "spatne"}, status=400)
        self.post("/api/spam-rules", {"sender": "me@example.com"}, status=400)  # vlastní adresa
        self.post("/api/spam-rules", {}, status=400)
        # doména + předmět: na serveru se hledá podle domény, přesně se ověří obě podmínky
        self.M.add("INBOX", make_raw(sender="info@shop.cz", subject="Sleva"))
        self.M.add("INBOX", make_raw(sender="info@shop.cz", subject="Faktura"))
        before = len(self.M.uids("Spam"))
        with mock.patch.object(app_module.threading, "Thread") as T:
            res = self.post("/api/spam-rules", {"sender": "shop.cz", "subject": "sleva", "subject_exact": True})
        app_module._spam_sender_job(*T.call_args.kwargs["args"])
        self.assertEqual(self.get(f"/api/jobs/{res['job_id']}")["moved"], 1)
        self.assertEqual(len(self.M.uids("Spam")), before + 1)
        rule = next(r for r in self.get("/api/spam-rules") if r["sender"] == "shop.cz")
        self.assertEqual((rule["subject"], rule["subject_exact"], rule["hits"]), ("sleva", 1, 1))

    def test_spam_with_sender_job(self):
        self.M.add("INBOX", make_raw(sender="eva@x.cz", subject="Starší", date=OLD))
        with mock.patch.object(app_module.threading, "Thread") as T:
            res = self.post(f"/api/accounts/a/messages/{self.u1}/spam", {"folder": "INBOX"})
        self.assertEqual(res["sender"], "eva@x.cz")
        self.assertIn("eva@x.cz", self.d.spam_senders())
        app_module._spam_sender_job(*T.call_args.kwargs["args"])
        job = self.get(f"/api/jobs/{res['job_id']}")
        self.assertEqual((job["status"], job["moved"]), ("done", 2))  # z obou účtů
        self.get(f"/api/jobs/{res['job_id']}", status=404)  # hotová úloha se po přečtení smaže
        self.assertEqual(len(self.M.uids("Spam")), 3)
        # jen zpráva, bez pravidla; zpráva mimo databázi
        u = self.M.add("INBOX", make_raw(sender="x@y.cz"))
        self.assertIsNone(self.post(f"/api/accounts/a/messages/{u}/spam", {"folder": "INBOX", "scope": "message"})
                          ["job_id"])
        self.post("/api/accounts/a/messages/999/spam", {"folder": "INBOX"}, status=400)

    def test_spam_failure_marks_job_done(self):
        self.M.fail["create"] = "NO"
        del self.M.folders["Spam"]
        res = self.post(f"/api/accounts/a/messages/{self.u1}/spam", {"folder": "INBOX"})
        self.assertEqual(self.get(f"/api/jobs/{res['job_id']}")["status"], "done")
        self.assertTrue(self.syncer.notices)

    def test_mark_all_read(self):
        self.assertEqual(self.post("/api/mark-all-read", {"type": "unified", "view": "all"})["count"], 2)
        self.assertNotIn(self.u1, [u for u in self.M.uids("INBOX") if "\\Seen" not in self.M.flags("INBOX", u)])
        self.assertEqual(self.post("/api/mark-all-read", {"type": "account", "account_id": "a"})["count"], 0)
        self.assertEqual(self.post("/api/mark-all-read", {"type": "spam"})["count"], 1)
        self.post("/api/mark-all-read", {"type": "unified", "view": "x"}, status=400)
        self.post("/api/mark-all-read", {"type": "x"}, status=400)
        fid = self.d.create_user_folder("P", "a", "Projekty", False, False)
        self.post("/api/mark-all-read", {"type": "ufolder", "id": fid})
        fid2 = self.d.create_user_folder("Bez účtu", None, "X", False, False)
        self.post("/api/mark-all-read", {"type": "ufolder", "id": fid2}, status=400)

    def test_trash_empty(self):
        self.assertEqual(self.post("/api/trash/empty")["deleted"], 1)
        self.assertEqual(self.M.uids("Trash"), [])
        self.N.fail["select"] = "NO"
        self.post("/api/trash/empty", status=400)

    def test_locate(self):
        loc = lambda folder, uid: self.get(f"/api/accounts/a/messages/{uid}/locate?folder={folder}")["view"]
        self.assertEqual(loc("INBOX", self.u1)["type"], "unified")
        self.assertEqual(loc("Trash", 1)["type"], "trash")
        self.assertEqual(loc("Sent", 1)["type"], "sent")
        self.assertEqual(loc("Spam", 1)["type"], "spam")
        self.assertEqual(loc("Archiv", 1)["type"], "archive")
        self.assertEqual(loc("Projekty", 1), None)
        self.M.add("Projekty", make_raw())
        self.syncer.sync_folder(self.A, "Projekty")
        self.assertEqual(loc("Projekty", 1)["type"], "folder")
        fid = self.d.create_user_folder("P", "a", "Projekty", False, False)
        self.assertEqual(loc("Projekty", 1), {"type": "ufolder", "id": fid, "title": "P"})

    def test_search(self):
        self.assertEqual(self.get("/api/search")["messages"], [])
        self.assertEqual(len(self.get("/api/search?q=projekt")["messages"]), 2)
        self.assertEqual(len(self.get("/api/search?from=eva@x.cz&att=0")["messages"]), 2)
        # na serveru: shoda v textu zprávy a zpráva, která ještě není stažená
        self.M.add("INBOX", make_raw(subject="Nestažená", body="návrh smlouvy"))
        res = self.get("/api/search/remote?q=návrh")
        self.assertEqual(sorted(m["subject"] for m in res["messages"]), ["Nestažená", "Projekt"])
        self.assertEqual(self.get("/api/search/remote?q=x")["messages"], [])
        for scope in ("unified&filter=important", "spam", "trash", "sent", "archive", "folder&account=a&folder=Archiv"):
            self.get(f"/api/search/remote?q=návrh&scope={scope}")
        fid = self.d.create_user_folder("P", "a", "Projekty", False, False)
        self.get(f"/api/search/remote?q=návrh&scope=ufolder&id={fid}")
        self.get("/api/search/remote?q=návrh&scope=nesmysl", status=400)
        self.get("/api/search/remote?q=návrh&scope=unified&filter=x", status=400)
        self.N.fail["select"] = RuntimeError("down")
        self.assertTrue(self.get("/api/search/remote?q=návrh")["errors"])

    def test_context(self):
        res = self.get(f"/api/accounts/a/messages/{self.u1}/context?folder=INBOX")  # synchronizováno → z databáze
        self.assertTrue(res["cached"])
        self.assertEqual([m["subject"] for m in res["messages"]], ["Re: Projekt"])
        res = self.get("/api/accounts/a/messages/1/context?folder=Sent")
        self.assertEqual(res["counterpart"], "eva@x.cz")
        with mock.patch.object(self.syncer, "context_ready", return_value=False):
            res = self.get(f"/api/accounts/a/messages/{self.u1}/context?folder=INBOX")
            self.assertEqual([m["subject"] for m in res["messages"]], ["Re: Projekt"])
            res = self.get("/api/accounts/a/messages/1/context?folder=Sent")
            self.assertEqual(res["counterpart"], "eva@x.cz")
            self.get("/api/accounts/a/messages/999/context?folder=INBOX", status=400)

    def test_contacts(self):
        self.assertIn("contacts", self.get("/api/contacts"))


class UserFoldersTest(AppBase):
    def setUp(self):
        super().setUp()
        self.u1 = self.M.add("INBOX", make_raw(sender="eva@x.cz", subject="Faktura 1", mid="<1@x>"))
        self.u2 = self.M.add("INBOX", make_raw(sender="eva@x.cz", subject="Faktura 2", mid="<2@x>"))
        self.sync_all()

    def create(self, **kw):
        return self.post("/api/user-folders", {"name": "Faktury", "account_id": "a", "new_folder": "Faktury", **kw})

    def test_crud_and_validation(self):
        f = self.create()
        self.assertEqual(f["imap_name"], "Faktury")
        self.post("/api/user-folders", {"name": "faktury", "account_id": "a", "new_folder": "X"}, status=400)
        self.post("/api/user-folders", {"name": "", "account_id": "a"}, status=400)
        self.post("/api/user-folders", {"name": "Y", "account_id": "neni"}, status=400)
        self.post("/api/user-folders", {"name": "Y", "account_id": "a", "new_folder": "a/b"}, status=400)
        self.post("/api/user-folders", {"name": "Y", "account_id": "a", "new_folder": "Spam"}, status=400)
        self.post("/api/user-folders", {"name": "Y", "account_id": "a", "server_folder": "Neni"}, status=400)
        self.post("/api/user-folders", {"name": "Y", "account_id": "a", "server_folder": "Sent"}, status=400)
        self.post("/api/user-folders", {"name": "Y", "account_id": "a", "new_folder": "Faktury"}, status=400)
        p = self.post("/api/user-folders", {"name": "Projekty", "account_id": "a", "server_folder": "Projekty"})
        self.put(f"/api/user-folders/{p['id']}", {"name": "Projekty 2", "account_id": "a", "server_folder": "Projekty",
                                                  "auto_sort": True})
        self.assertEqual(len(self.get("/api/user-folders")), 2)
        self.put("/api/user-folders/999", {"name": "X"}, status=404)
        self.delete(f"/api/user-folders/{p['id']}")
        with mock.patch.object(app_module.threading, "Thread") as T:
            self.assertTrue(self.delete(f"/api/user-folders/{f['id']}?server=1")["server"])
        T.return_value.start.assert_called_once()
        orphan = self.d.create_user_folder("Sirotek", None, "S", False, False)
        self.delete(f"/api/user-folders/{orphan}?server=1", status=400)
        self.assertEqual(self.get(f"/api/user-folders/{orphan}/messages")["messages"], [])

    def test_rules_and_to_folder(self):
        f = self.create(mark_read=True)
        fid = f["id"]
        rules = self.post(f"/api/user-folders/{fid}/rules", {"subject": "Faktura"})
        self.assertEqual((rules[0]["subject"], rules[0]["subject_exact"]), ("faktura", 0))
        self.post(f"/api/user-folders/{fid}/rules", {"sender": "spatne"}, status=400)
        self.post(f"/api/user-folders/{fid}/rules", {}, status=400)
        both = self.post(f"/api/user-folders/{fid}/rules", {"sender": "x.cz", "recipient": "me@example.com",
                                                             "after_read": True})
        self.assertEqual({(r["sender"], r["recipient"], r["after_read"]) for r in both},
                         {("", "", 0), ("x.cz", "me@example.com", 1)})
        self.delete(f"/api/user-folder-rules/{next(r['id'] for r in both if r['sender'])}")
        rid = self.get(f"/api/user-folders/{fid}/rules")[0]["id"]
        self.put(f"/api/user-folder-rules/{rid}", {"subject": "faktura 1"})
        res = self.post(f"/api/accounts/a/messages/{self.u2}/to-folder", {"folder": "INBOX", "target_id": fid})
        self.assertEqual((res["kind"], res["value"], res["moved"]), ("sender", "eva@x.cz", True))
        self.assertEqual(self.M.uids("INBOX"), [self.u1])
        self.assertEqual(len(self.M.uids("Faktury")), 1)
        msgs = self.get(f"/api/user-folders/{fid}/messages")["messages"]
        self.assertEqual([m["subject"] for m in msgs], ["Faktura 2"])
        self.delete(f"/api/user-folder-rules/{rid}")
        orphan = self.d.create_user_folder("Sirotek", None, "S", False, False)
        self.post(f"/api/accounts/a/messages/{self.u1}/to-folder", {"folder": "INBOX", "target_id": orphan}, status=400)

    def test_move_to_folder_and_inbox(self):
        f = self.create()
        items = [{"account_id": "a", "folder": "INBOX", "uid": u} for u in (self.u1, self.u2)]
        prev = self.post("/api/messages/move/preview", {"target": {"type": "ufolder", "id": f["id"]}, "items": items})
        self.assertEqual((prev["count"], prev["senders"]), (2, ["eva@x.cz"]))
        self.post("/api/messages/move", {"target": {"type": "ufolder", "id": f["id"]}, "items": items,
                                         "scope": "sender"})
        self.assertEqual(self.M.uids("INBOX"), [])
        self.assertEqual(self.d.folder_rules(f["id"])[0]["sender"], "eva@x.cz")
        back = [{"account_id": "a", "folder": "Faktury", "uid": u} for u in self.M.uids("Faktury")]
        self.post("/api/messages/move", {"target": {"type": "inbox", "account_id": "b"}, "items": back,
                                         "scope": "release"})
        self.assertEqual(len(self.N.uids("INBOX")), 2)

    def test_move_to_spam_and_trash(self):
        items = [{"account_id": "a", "folder": "INBOX", "uid": self.u1}]
        self.d.update_settings({"spam_delete_after_week": True})
        prev = self.post("/api/messages/move/preview", {"target": {"type": "spam"}, "items": items})
        self.assertEqual(prev["scopes"], ["message", "sender"])
        self.post("/api/messages/move", {"target": {"type": "spam"}, "items": items, "scope": "sender"})
        self.assertIn("eva@x.cz", self.d.spam_senders())
        trash_items = [{"account_id": "a", "folder": "INBOX", "uid": self.u2}, {"account_id": "neni", "folder": "X",
                                                                                "uid": 1}, {"bad": 1}]
        self.post("/api/messages/move/preview", {"target": {"type": "trash"}, "items": trash_items})
        self.d.update_settings({"spam_delete_after_week": False, "spam_delete_after_year": True,
                                "trash_delete_after_year": True})
        old = [{"account_id": "a", "folder": "INBOX", "uid": self.u2}]
        prev = self.post("/api/messages/move/preview", {"target": {"type": "spam"}, "items": old})
        self.assertEqual(len(prev["warnings"]), 1)
        prev = self.post("/api/messages/move/preview", {"target": {"type": "trash"}, "items": old})
        self.assertEqual(len(prev["warnings"]), 1)
        self.d.update_settings({"trash_delete_after_year": False})
        prev = self.post("/api/messages/move/preview", {"target": {"type": "trash"}, "items": old})
        self.assertEqual(prev["warnings"], [])
        self.post("/api/messages/move", {"target": {"type": "trash"}, "items": trash_items})
        self.assertEqual(self.M.uids("INBOX"), [])
        self.post("/api/messages/move", {"target": {"type": "nikam"}, "items": items}, status=400)
        self.post("/api/messages/move", {"target": {"type": "trash"}, "items": []}, status=400)
        self.post("/api/messages/move", {"target": {"type": "trash"}, "items": items, "scope": "sender"}, status=400)
        self.post("/api/messages/move", {"target": {"type": "inbox", "view": "x"}, "items": items}, status=400)


class SendTest(AppBase):
    def send(self, status=200, **body):
        smtp = mock.MagicMock()
        with mock.patch.object(mail, "smtp_connect", return_value=smtp):
            res = self.post("/api/send", {"account_id": "a", "to": "eva@x.cz", "subject": "S", "body": "B", **body},
                            status=status)
        return res, smtp

    def test_send_now_with_attachments(self):
        src = self.M.add("INBOX", make_raw(sender="x@y.cz"))
        res, smtp = self.send(attachments=[{"filename": "a.txt", "data": base64.b64encode(b"hi").decode()}],
                              forward={"account_id": "a", "folder": "INBOX", "uid": src, "indexes": []})
        self.assertIsNone(res["warning"])
        smtp.send_message.assert_called_once()
        self.assertEqual(len(self.M.uids("Sent")), 1)
        self.send(status=400, attachments=[{"filename": "x", "data": "!!!"}])
        self.send(status=400, to="spatne")

    def test_delayed_send_commit_cancel_flush(self):
        with mock.patch.object(app_module.threading, "Timer"):
            res, _ = self.send(delay=5)
            sid = res["pending_id"]
            with mock.patch.object(mail, "smtp_connect", return_value=mock.MagicMock()):
                self.assertIsNone(self.post(f"/api/send/{sid}/commit")["warning"])
            self.post(f"/api/send/{sid}/commit", status=404)
            res, _ = self.send(delay=5)
            self.post(f"/api/send/{res['pending_id']}/cancel")
            self.post(f"/api/send/{res['pending_id']}/cancel", status=404)
            res, _ = self.send(delay=5)
            with mock.patch.object(mail, "smtp_connect", side_effect=OSError("down")):
                app_module.flush_pending_sends()
            self.post(f"/api/send/{res['pending_id']}/cancel", status=400)  # už se odesílalo
            r = self.post(f"/api/send/{res['pending_id']}/commit", status=400)
            self.assertIn("down", r["error"])


if __name__ == "__main__":
    unittest.main()
