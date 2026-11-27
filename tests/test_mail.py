"""mail.py: pomocné funkce, chybové hlášky, sestavení zprávy a IMAP operace (proti falešnému serveru)."""

import email
import imaplib
import os
import smtplib
import socket
import ssl
import sys
import tempfile
import unittest
from email import policy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import google_oauth  # noqa: E402
import mail  # noqa: E402
from fake_imap import FakeIMAP, make_raw  # noqa: E402

ME = "me@example.com"
GMAIL = (("INBOX", None), ("[Gmail]/Sent Mail", "sent"), ("[Gmail]/All Mail", "all"),
         ("[Gmail]/Spam", "junk"), ("[Gmail]/Trash", "trash"), ("Faktury", None))
PLAIN = (("INBOX", None), ("Sent", "sent"), ("Trash", "trash"), ("Archiv", None), ("Projekty/Web", None))


def headers(raw):
    return email.message_from_bytes(raw, policy=policy.compat32)


class HelpersTest(unittest.TestCase):
    def test_utf7_roundtrip(self):
        for name in ("Odeslaná pošta", "Koš", "A&B", "日本語", "plain"):
            self.assertEqual(mail.decode_utf7(mail.encode_utf7(name)), name)
        self.assertEqual(mail.encode_utf7("Koš"), "Ko&AWE-")
        self.assertEqual(mail.encode_utf7("A&B"), "A&-B")

    def test_decode_utf7_broken(self):
        self.assertEqual(mail.decode_utf7("abc&def"), "abc&def")       # bez ukončení
        self.assertEqual(mail.decode_utf7("x&A-y"), "x&A-y")           # neplatné base64

    def test_unquote_and_q(self):
        self.assertEqual(mail._unquote('"a \\"b\\" \\\\c"'), 'a "b" \\c')
        self.assertEqual(mail._unquote("INBOX"), "INBOX")
        self.assertEqual(mail.q('a"b'), '"a\\"b"')
        with self.assertRaises(mail.MailError):
            mail.q("a\r\nLOGOUT")

    def test_dec_and_dates(self):
        self.assertEqual(mail.dec("=?utf-8?q?P=C5=99=C3=ADloha?="), "Příloha")
        self.assertEqual(mail.dec(None), "")
        self.assertEqual(mail.parse_date("Mon, 02 Jan 2023 10:00:00 +0000"), 1672653600.0)
        self.assertEqual(mail.parse_date("Mon, 02 Jan 2023 10:00:00"), 1672653600.0)  # bez zóny = UTC
        self.assertIsNone(mail.parse_date("nesmysl"))
        self.assertIsNone(mail.parse_date(""))

    def test_parse_addrs(self):
        self.assertEqual(mail.parse_addrs('"Eva" <eva@x.cz>, petr@y.cz'),
                         [{"name": "Eva", "addr": "eva@x.cz"}, {"name": "", "addr": "petr@y.cz"}])
        self.assertEqual(mail.parse_addrs(""), [])

    def test_html_to_text(self):
        html = "<html><head><title>T</title><style>p{}</style></head><body><p>Ahoj</p><div>světe&nbsp;!</div>" \
               "<script>x()</script></body></html>"
        self.assertEqual(mail.html_to_text(html), "Ahoj\n\nsvěte\xa0!")

    def test_base_subject(self):
        self.assertEqual(mail.base_subject("Re: Fwd: [list] RE[2]: Odp: Projekt  X"), "Projekt X")
        self.assertEqual(mail.base_subject(None), "")

    def test_clean_filename(self):
        self.assertEqual(mail.clean_filename("faktura‮fdp.exe"), "fakturafdp.exe")
        self.assertEqual(mail.clean_filename("../a:b.txt"), "_a_b.txt")
        self.assertEqual(mail.clean_filename("   "), "priloha")

    def test_size_batches(self):
        self.assertEqual(mail.size_batches([1, 2, 3, 4], {1: 6, 2: 6, 3: 20, 4: 1}, limit=10), [[1], [2], [3], [4]])
        self.assertEqual(mail.size_batches([1, 2, 3], {1: 3, 2: 3, 3: 3}, limit=10), [[1, 2, 3]])
        self.assertEqual(mail.size_batches([], {}), [])

    def test_parse_fetch_joins_trailing_parts(self):
        data = [(b"1 (UID 5 BODY[] {3}", b"abc"), b" FLAGS (\\Seen))", b"2 (UID 6 FLAGS ())"]
        items = mail._parse_fetch(data)
        self.assertEqual(len(items), 2)
        self.assertIn(b"\\Seen", items[0][0])
        self.assertEqual(mail._flags(items[0][0]), ["\\Seen"])

    def test_thread_refs(self):
        h = headers(b"In-Reply-To: <b@x>\r\nReferences: <a@x> <b@x>\r\n\r\n")
        self.assertEqual(mail.thread_refs(h), ["<b@x>", "<a@x>"])


class FriendlyErrorTest(unittest.TestCase):
    def test_messages(self):
        cases = [
            (mail.MailError("vlastní"), "vlastní"),
            (google_oauth.OAuthError("oauth"), "oauth"),
            (smtplib.SMTPAuthenticationError(535, b"bad"), mail.tr("err.smtpLogin")),
            (smtplib.SMTPAuthenticationError(534, b"Application-specific password required"),
             mail.tr("err.gmailPassword")),
            (imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] Invalid credentials'"), mail.tr("err.imapLogin")),
            (socket.gaierror(), mail.tr("err.serverNotFound")),
            (socket.timeout(), mail.tr("err.timeout")),
            (ConnectionRefusedError(), mail.tr("err.connectionRefused")),
        ]
        for exc, expected in cases:
            self.assertEqual(mail.friendly_error(exc), expected, repr(exc))

    def test_formatted_messages(self):
        self.assertIn("a@x.cz", mail.friendly_error(smtplib.SMTPRecipientsRefused({"a@x.cz": (550, b"no")})))
        self.assertIn("kaput", mail.friendly_error(smtplib.SMTPException("kaput")))
        self.assertIn("weird", mail.friendly_error(imaplib.IMAP4.error("weird")))
        self.assertIn("cert", mail.friendly_error(ssl.SSLError("cert")))
        self.assertIn("net", mail.friendly_error(OSError("net")))
        self.assertIn("boom", mail.friendly_error(ValueError("boom")))


class AuthResultsTest(unittest.TestCase):
    def check(self, value, sender, expected):
        h = headers(f"Authentication-Results: {value}\r\n\r\n".encode())
        self.assertEqual(mail.auth_results(h, sender), expected)

    def test_dmarc_pass(self):
        self.check("mx.google.com; dkim=pass header.i=@other.com; spf=pass; dmarc=pass (p=NONE sp=NONE) "
                   "header.from=shop.cz", "info@shop.cz", ("mx.google.com", True))

    def test_dkim_aligned_subdomain(self):
        self.check("mx.example; dkim=pass header.d=mail.shop.cz", "info@shop.cz", ("mx.example", True))

    def test_fail_and_misaligned(self):
        self.check("mx.example; dkim=pass header.d=evil.com; dmarc=fail header.from=shop.cz", "info@shop.cz",
                   ("mx.example", False))

    def test_microsoft_without_authserv(self):
        self.check("dmarc=bestguesspass action=none header.from=shop.cz", "x@shop.cz", ("", True))

    def test_missing(self):
        self.assertEqual(mail.auth_results(headers(b"\r\n"), "a@b.cz"), ("", False))

    def test_header_record(self):
        raw = make_raw(subject="Re: Faktura", mid="<1@x>",
                       extra="Content-Type: multipart/mixed; boundary=x\nIn-Reply-To: <0@x>")
        r = mail.header_record(7, ["\\Seen", "\\Deleted"], 123, headers(raw))
        self.assertEqual((r["uid"], r["base_subject"], r["seen"], r["flagged"], r["deleted"], r["size"]),
                         (7, "faktura", True, False, True, 123))
        self.assertEqual(r["refs"], ["<0@x>"])
        self.assertEqual(r["from"][0]["addr"], "eva@example.com")


class ComposeTest(unittest.TestCase):
    ACC = {"email": ME, "display_name": "Já Sám", "smtp_host": "smtp.example.com"}

    def test_parse_recipients(self):
        self.assertEqual(mail.parse_recipients(to="a@x.cz; b@x.cz", cc="", bcc="a@x.cz, c@y.cz,"),
                         ["a@x.cz", "b@x.cz", "c@y.cz"])
        with self.assertRaises(mail.MailError):
            mail.parse_recipients(to="neplatna-adresa")
        with self.assertRaises(mail.MailError):
            mail.parse_recipients(custom="x@")

    def test_build_message(self):
        msg = mail.build_message(self.ACC, "a@x.cz", "b@x.cz", "Předmět", "Tělo zprávy",
                                 in_reply_to="<p@x>", attachments=[("a.txt", "text/plain", b"hi"),
                                                                    ("b.bin", None, b"\0")])
        self.assertIn("Já Sám", str(msg["From"]))
        self.assertEqual(msg["References"], "<p@x>")
        self.assertEqual(msg["Content-Language"], "cs")
        self.assertTrue(msg["Message-ID"].endswith("@example.com>"))
        names = [p.get_filename() for p in msg.iter_attachments()]
        self.assertEqual(names, ["a.txt", "b.bin"])

    def test_send_requires_recipient(self):
        with self.assertRaises(mail.MailError):
            mail.send_message(self.ACC, None, [], None)

    def test_send_saves_copy_to_sent(self):
        M = FakeIMAP(PLAIN)
        pool = mock.MagicMock()
        pool.get.return_value.__enter__.return_value = M
        smtp = mock.MagicMock()
        msg = mail.build_message(self.ACC, "a@x.cz", "", "S", "B")
        with mock.patch.object(mail, "smtp_connect", return_value=smtp):
            self.assertIsNone(mail.send_message(self.ACC, msg, ["a@x.cz"], pool))
        smtp.send_message.assert_called_once()
        self.assertEqual(len(M.uids("Sent")), 1)
        self.assertIn("\\Seen", M.flags("Sent", 1))

    def test_send_gmail_skips_copy_and_reports_copy_error(self):
        pool = mock.MagicMock()
        with mock.patch.object(mail, "smtp_connect", return_value=mock.MagicMock()):
            self.assertIsNone(mail.send_message({**self.ACC, "smtp_host": "smtp.gmail.com"}, mock.MagicMock(),
                                                ["a@x.cz"], pool))
            pool.get.assert_not_called()
            pool.get.side_effect = OSError("down")
            err = mail.send_message(self.ACC, mock.MagicMock(), ["a@x.cz"], pool)
        self.assertIn("down", err)


class ParseMessageTest(unittest.TestCase):
    def test_html_with_inline_image_and_attachment(self):
        raw = (b"From: Eva <eva@x.cz>\r\nTo: me@example.com\r\nSubject: =?utf-8?q?P=C5=99=C3=ADloha?=\r\n"
               b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"M\"\r\n\r\n"
               b"--M\r\nContent-Type: multipart/related; boundary=\"R\"\r\n\r\n"
               b"--R\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
               b"<meta http-equiv=\"refresh\" content=\"0\"><p>Ahoj <img src=\"cid:logo\"></p>\r\n"
               b"--R\r\nContent-Type: image/png\r\nContent-ID: <logo>\r\nContent-Transfer-Encoding: base64\r\n\r\n"
               b"iVBORw0K\r\n--R--\r\n"
               b"--M\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"f.pdf\"\r\n"
               b"Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n--M--\r\n")
        m = mail.parse_message(raw, 3, "INBOX", ["\\Seen"])
        self.assertEqual(m["subject"], "Příloha")
        self.assertIn("data:image/png;base64,", m["html"])
        self.assertNotIn("refresh", m["html"])
        self.assertEqual(m["text"], "Ahoj")
        self.assertEqual([a["filename"] for a in m["attachments"]], ["f.pdf"])
        self.assertTrue(m["seen"])
        self.assertEqual(mail.all_attachments(raw), [("f.pdf", "application/pdf", b"%PDF")])
        with self.assertRaises(mail.MailError):
            mail.attachments_from_raw(raw, [99])

    def test_truncated(self):
        m = mail.parse_message(make_raw(), 1, "INBOX", [], size=mail.MAX_MESSAGE + 1)
        self.assertTrue(m["truncated"])
        self.assertEqual(m["attachments"], [])

    def test_bad_charset_falls_back(self):
        raw = b"Subject: x\r\nContent-Type: text/plain; charset=nonexistent\r\n\r\nahoj"
        self.assertEqual(mail.parse_message(raw, 1, "INBOX", [])["text"].strip(), "ahoj")

    def test_message_as_text(self):
        text = mail.message_as_text(make_raw(subject="Ahoj", body="Řádek 1\nŘádek 2", cc="c@x.cz"))
        self.assertIn("Ahoj", text)
        self.assertIn("c@x.cz", text)
        self.assertTrue(text.endswith("Řádek 1\r\nŘádek 2"))


class FolderTest(unittest.TestCase):
    def test_list_folders_roles_and_counts(self):
        M = FakeIMAP(PLAIN)
        M.add_folder("Odeslané")              # druhá složka s rolí „sent“ podle názvu – role jen jednou
        M.add_folder("Hidden", flags=["\\Noselect"])
        M.add("INBOX", make_raw())
        M.add("INBOX", make_raw(), ["\\Seen"])
        folders = mail.list_folders(M)
        by = {f["name"]: f for f in folders}
        self.assertEqual(folders[0]["name"], "INBOX")
        self.assertEqual(by["INBOX"]["unseen"], 1)
        self.assertEqual(by["Sent"]["role"], "sent")
        self.assertIsNone(by["Odeslané"]["role"])
        self.assertEqual((by["Projekty/Web"]["display"], by["Projekty/Web"]["depth"]), ("Web", 1))
        self.assertNotIn("Hidden", by)
        self.assertEqual(mail.find_role(M, "trash"), "Trash")

    def test_list_folders_literal_and_role_by_name(self):
        M = FakeIMAP(())
        M.list = lambda: ("OK", [(b'(\\HasNoChildren) "/" {4}', b"Spam"), b'(\\HasNoChildren) NIL "Ko&AWE-"',
                                 b"garbage", None])
        by = {f["name"]: f for f in mail.list_folders(M, with_counts=False)}
        self.assertEqual(by["Spam"]["role"], "junk")
        self.assertEqual((by["Ko&AWE-"]["role"], by["Ko&AWE-"]["path"]), ("trash", "Koš"))

    def test_list_folders_error(self):
        M = FakeIMAP()
        M.fail["list"] = "NO"
        with self.assertRaises(mail.MailError):
            mail.list_folders(M)

    def test_folder_status_and_uids(self):
        M = FakeIMAP()
        M.add("INBOX", make_raw())
        self.assertEqual(mail.folder_status(M, "INBOX"), {"uidvalidity": 1, "messages": 1})
        M.fail["status"] = "NO"
        with self.assertRaises(mail.MailError):
            mail.folder_status(M, "INBOX")
        mail._select(M, "INBOX", True)
        self.assertEqual(mail.all_uids(M), {1})
        with self.assertRaises(mail.MailError):
            mail._select(M, "Neni", True)

    def test_ensure_and_delete_folder(self):
        M = FakeIMAP(PLAIN)
        mail.ensure_named_folder(M, "Nová")
        mail.ensure_named_folder(M, "Nová")  # podruhé nic
        self.assertEqual(M.calls.count("create"), 1)
        M.add("Nová", make_raw())
        mail.delete_folder_with_messages(M, "Nová")
        self.assertNotIn("Nová", M.folders)
        self.assertEqual(len(M.uids("Trash")), 1)
        M.fail["create"] = "NO"
        with self.assertRaises(mail.MailError):
            mail.ensure_named_folder(M, "Další")
        M.fail["delete"] = "NO"
        with self.assertRaises(mail.MailError):
            mail.delete_folder_with_messages(M, "Archiv")


class MessageOpsTest(unittest.TestCase):
    def setUp(self):
        self.M = FakeIMAP(PLAIN)
        self.u1 = self.M.add("INBOX", make_raw(sender="eva@x.cz", subject="Projekt", mid="<1@x>"))
        self.u2 = self.M.add("INBOX", make_raw(sender="petr@y.cz", subject="Oběd", mid="<2@x>"), ["\\Seen"])

    def test_list_messages_and_search(self):
        res = mail.list_messages(self.M, "INBOX", page_size=1)
        self.assertEqual((res["total"], res["has_more"], res["messages"][0]["uid"]), (2, True, self.u2))
        self.assertEqual(mail.list_messages(self.M, "INBOX", query="projekt")["total"], 1)
        self.assertEqual(mail.list_messages(self.M, "INBOX", query="oběd")["total"], 1)  # UTF-8 hledání
        self.M.fail["uid SEARCH"] = "NO"
        with self.assertRaises(mail.MailError):
            mail.list_messages(self.M, "INBOX")

    def test_get_message_marks_seen(self):
        m = mail.get_message(self.M, "INBOX", self.u1)
        self.assertEqual(m["subject"], "Projekt")
        self.assertIn("\\Seen", self.M.flags("INBOX", self.u1))
        with self.assertRaises(mail.MessageGone):
            mail.get_message(self.M, "INBOX", 999)

    def test_raw_and_attachments_size_limit(self):
        self.assertEqual(mail.get_raw(self.M, "INBOX", self.u1), self.M.folders["INBOX"]["msgs"][self.u1]["raw"])
        self.assertEqual(mail.get_attachments(self.M, "INBOX", self.u1, []), [])
        self.M.size_override[("INBOX", self.u1)] = mail.MAX_MESSAGE + 1
        with self.assertRaises(mail.MessageTooLarge):
            mail.get_raw(self.M, "INBOX", self.u1)
        with self.assertRaises(mail.MessageTooLarge):
            mail.get_attachments(self.M, "INBOX", self.u1, [0])

    def test_set_flag(self):
        mail.set_flag(self.M, "INBOX", self.u1, "\\Flagged", True)
        self.assertIn("\\Flagged", self.M.flags("INBOX", self.u1))
        mail.set_flag(self.M, "INBOX", self.u2, "\\Seen", False)
        self.assertNotIn("\\Seen", self.M.flags("INBOX", self.u2))
        with self.assertRaises(mail.MailError):
            mail.set_flag(self.M, "INBOX", self.u1, "\\Deleted", True)
        self.M.fail["uid STORE"] = "NO"
        with self.assertRaises(mail.MailError):
            mail.set_flag(self.M, "INBOX", self.u1, "\\Seen", True)

    def test_fetch_flags_and_headers(self):
        mail._select(self.M, "INBOX", True)
        self.assertEqual(mail.fetch_flags(self.M, "1:*"), {self.u1: (False, False, False), self.u2: (True, False, False)})
        recs = mail.fetch_header_records(self.M, [b"1", 2])
        self.assertEqual([r["message_id"] for r in recs], ["<1@x>", "<2@x>"])
        self.assertEqual(mail.fetch_header_records(self.M, []), [])
        self.assertEqual(mail._fetch_headers(self.M, []), [])

    def test_mark_unseen_by_id(self):
        self.assertEqual(mail.mark_unseen_by_id(self.M, "INBOX", self.u2, "<2@x>"), self.u2)
        self.assertNotIn("\\Seen", self.M.flags("INBOX", self.u2))
        # UID se změnilo – dohledá podle Message-ID
        self.assertEqual(mail.mark_unseen_by_id(self.M, "INBOX", self.u1, "<2@x>"), self.u2)
        self.assertIsNone(mail.mark_unseen_by_id(self.M, "INBOX", 999, "<neni@x>"))

    def test_trash_and_expunge(self):
        mail.delete_message(self.M, "INBOX", self.u1)
        self.assertEqual(self.M.uids("INBOX"), [self.u2])
        self.assertIn("\\Seen", self.M.flags("Trash", 1))
        mail.trash_uids(self.M, "Trash", [1])  # z koše natrvalo
        self.assertEqual(self.M.uids("Trash"), [])
        mail.trash_uids(self.M, "INBOX", [])
        self.assertEqual(mail.expunge_all(self.M, "INBOX"), 1)
        self.assertEqual(self.M.uids("INBOX"), [])

    def test_move_without_move_capability(self):
        self.M.caps = {"IMAP4REV1"}
        mail.move_to_named_folder(self.M, "INBOX", [self.u1], "Archiv", mark_read=True)
        self.assertIn("expunge", self.M.calls)
        self.assertEqual(self.M.uids("INBOX"), [self.u2])
        self.assertIn("\\Seen", self.M.flags("Archiv", 1))
        self.assertEqual(mail.move_to_named_folder(self.M, "INBOX", [], "Archiv"), 0)
        self.M.fail["uid COPY"] = "NO"
        with self.assertRaises(mail.MailError):
            mail.move_to_named_folder(self.M, "INBOX", [self.u2], "Archiv")

    def test_move_from_gmail_all_mail_only_copies(self):
        M = FakeIMAP(GMAIL)
        u = M.add("[Gmail]/All Mail", make_raw())
        self.assertEqual(mail.move_to_named_folder(M, "[Gmail]/All Mail", [u], "Faktury"), 1)
        self.assertEqual(M.uids("[Gmail]/All Mail"), [u])
        self.assertEqual(len(M.uids("Faktury")), 1)

    def test_archive(self):
        mail.archive_message(self.M, "INBOX", self.u1)  # „Archiv“ podle názvu
        self.assertEqual(len(self.M.uids("Archiv")), 1)
        with self.assertRaises(mail.MailError):
            mail.archive_message(self.M, "Archiv", 1)
        M = FakeIMAP()
        u = M.add("INBOX", make_raw())
        mail.archive_message(M, "INBOX", u)  # bez archivu – vytvoří „Archive“
        self.assertEqual(len(M.uids("Archive")), 1)

    def test_spam(self):
        sender, ts = mail.spam_message(self.M, "INBOX", self.u1)
        self.assertEqual((sender, ts), ("eva@x.cz", 1672653600.0))
        self.assertEqual(len(self.M.uids("Spam")), 1)
        with self.assertRaises(mail.MailError):
            mail.spam_message(self.M, "Spam", 1)
        with self.assertRaises(mail.MessageGone):
            mail.spam_message(self.M, "INBOX", 999)
        self.assertEqual(mail.move_to_spam(self.M, "INBOX", [self.u2]), 1)
        self.assertEqual(mail.move_to_spam(self.M, "INBOX", []), 0)

    def test_move_sender_to_spam(self):
        self.M.add("INBOX", make_raw(sender="eva@x.cz", date="Tue, 03 Jan 2023 10:00:00 +0000"))
        self.M.add("INBOX", make_raw(sender="neva@x.cz"))  # podřetězec – nepřesune se
        n = mail.move_sender_to_spam(self.M, "eva@x.cz", mail.parse_date("Mon, 02 Jan 2023 12:00:00 +0000"))
        self.assertEqual(n, 1)
        self.assertEqual(mail.move_sender_to_spam(self.M, "éva@x.cz", None), 0)

    def test_move_sender_to_spam_skips_todo(self):
        self.M.add("INBOX", make_raw(sender="eva@x.cz", mid="<todo@x.cz>"))
        self.M.add("INBOX", make_raw(sender="eva@x.cz", mid="<other@x.cz>"))
        self.assertEqual(mail.move_sender_to_spam(self.M, "eva@x.cz", None, {"<todo@x.cz>"}), 2)  # + <1@x> ze setUp
        mail._select(self.M, "INBOX", True)
        left = [r["message_id"] for r in mail.fetch_header_records(self.M, self.M.uids("INBOX"))]
        self.assertIn("<todo@x.cz>", left)
        self.assertNotIn("<other@x.cz>", left)

    def test_transfer_between_accounts(self):
        mail._select(self.M, "INBOX", True)
        sizes = mail.message_sizes(self.M, "INBOX", [self.u1, self.u2])
        self.assertEqual(set(sizes), {self.u1, self.u2})
        items = mail.fetch_for_transfer(self.M, "INBOX", [self.u1, self.u2])
        self.assertEqual(items[1][2], ["\\Seen"])
        dst = FakeIMAP()
        done, err = mail.append_messages(dst, "Cíl", items + [(9, b"x", [], None)], mark_read=True)
        self.assertEqual((done, err), ([self.u1, self.u2, 9], None))
        self.assertTrue(all("\\Seen" in dst.flags("Cíl", u) for u in dst.uids("Cíl")))
        dst.fail["append"] = "NO"
        self.assertIsInstance(mail.append_messages(dst, "Cíl", items)[1], mail.MailError)
        dst.fail["append"] = imaplib.IMAP4.error("boom")
        self.assertEqual(mail.append_messages(dst, "Cíl", items)[0], [])

    def test_probe_authserv(self):
        M = FakeIMAP()
        self.assertEqual(mail.probe_authserv(M), ("", 0, 0))
        for i in range(3):
            M.add("INBOX", make_raw(extra="Authentication-Results: mx.google.com; dmarc=pass"))
        M.add("INBOX", make_raw())
        self.assertEqual(mail.probe_authserv(M), ("mx.google.com", 3, 4))


class FindRelatedTest(unittest.TestCase):
    def test_plain_server(self):
        M = FakeIMAP(PLAIN)
        M.add("INBOX", make_raw(sender="eva@x.cz", subject="Projekt", mid="<1@x>"))
        M.add("INBOX", make_raw(sender="jan@z.cz", subject="Úplně jiné", mid="<3@x>",
                                extra="References: <1@x>"))
        M.add("INBOX", make_raw(sender="petr@y.cz", subject="Re: Projekt", mid="<4@x>"))
        M.add("INBOX", make_raw(sender="ota@y.cz", subject="Nic", mid="<5@x>", cc="eva@x.cz"))
        M.add("Sent", make_raw(sender=ME, to="eva@x.cz", subject="Jiné", mid="<2@x>",
                               date="Tue, 03 Jan 2023 10:00:00 +0000"))
        M.add("Trash", make_raw(sender="eva@x.cz", subject="Projekt", mid="<6@x>"))
        found = mail.find_related(M, ME, "eva@x.cz", "Projekt", "<1@x>")
        got = {m["subject"]: m["reasons"] for m in found}
        self.assertEqual(found[0]["subject"], "Jiné")  # nejnovější první
        self.assertEqual(got["Jiné"], ["mine"])
        self.assertEqual(got["Úplně jiné"], ["thread"])
        self.assertEqual(got["Re: Projekt"], ["subject"])
        self.assertEqual(got["Nic"], ["cc"])
        self.assertNotIn("Projekt", got)  # samotná zpráva i kopie v koši se vynechají

    def test_gmail_utf8_subject_and_stale(self):
        M = FakeIMAP(GMAIL)
        M.add("[Gmail]/All Mail", make_raw(sender="x@y.cz", subject="Oběd zítra", mid="<9@x>"))
        M.add("Faktury", make_raw(sender="x@y.cz", subject="Oběd zítra", mid="<9@x>"))
        found = mail.find_related(M, ME, "", "Re: Oběd zítra", None)
        self.assertEqual([(m["folder"], m["reasons"]) for m in found], [("[Gmail]/All Mail", ["subject"])])
        self.assertIsNone(mail.find_related(M, ME, "", "x", None, is_stale=lambda: True))

    def test_utf8_search_unsupported(self):
        M = FakeIMAP(PLAIN)
        M.add("INBOX", make_raw(subject="Oběd"))
        orig = M.uid

        def uid(cmd, *args):
            if "CHARSET" in args:
                raise imaplib.IMAP4.error("BADCHARSET")
            return orig(cmd, *args)
        M.uid = uid
        self.assertEqual(mail.find_related(M, ME, "", "Oběd", None), [])
        self.assertIsNone(M.literal)


class ConnectTest(unittest.TestCase):
    ACC = {"id": "a", "imap_host": "imap.x", "imap_port": 993, "imap_security": "ssl", "username": "u",
           "password": "p", "smtp_host": "smtp.x", "smtp_port": 465, "smtp_security": "ssl"}

    def test_imap_password_login(self):
        M = FakeIMAP()
        with mock.patch.object(imaplib, "IMAP4_SSL", return_value=M):
            self.assertIs(mail.imap_connect(self.ACC), M)
        self.assertEqual(M.user, ("u", "p"))
        self.assertIn("MOVE", M.caps)

    def test_imap_starttls_oauth(self):
        M = FakeIMAP()
        with mock.patch.object(imaplib, "IMAP4", return_value=M), \
                mock.patch.object(google_oauth, "xoauth2_string", return_value="tok"):
            mail.imap_connect({**self.ACC, "imap_security": "starttls", "auth": "oauth2"})
        self.assertIn("starttls", M.calls)
        self.assertEqual(M.auth, b"tok")

    def test_imap_oauth_failure_and_cleanup(self):
        M = FakeIMAP()
        M.fail["authenticate"] = imaplib.IMAP4.error("denied")
        with mock.patch.object(imaplib, "IMAP4_SSL", return_value=M), \
                mock.patch.object(google_oauth, "xoauth2_string", return_value="tok"):
            with self.assertRaises(mail.MailError):
                mail.imap_connect({**self.ACC, "auth": "oauth2"})
        self.assertIn("shutdown", M.calls)

    def test_smtp(self):
        s = mock.MagicMock()
        with mock.patch.object(smtplib, "SMTP_SSL", return_value=s):
            self.assertIs(mail.smtp_connect(self.ACC), s)
        s.login.assert_called_once_with("u", "p")
        s2 = mock.MagicMock()
        with mock.patch.object(smtplib, "SMTP", return_value=s2), \
                mock.patch.object(google_oauth, "xoauth2_string", return_value="tok"):
            mail.smtp_connect({**self.ACC, "smtp_security": "starttls", "auth": "oauth2"})
        s2.starttls.assert_called_once()
        s2.auth.assert_called_once()
        s3 = mock.MagicMock()
        s3.login.side_effect = smtplib.SMTPAuthenticationError(535, b"x")
        with mock.patch.object(smtplib, "SMTP_SSL", return_value=s3):
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                mail.smtp_connect(self.ACC)
        s3.close.assert_called_once()

    def test_test_account(self):
        with mock.patch.object(mail, "imap_connect", return_value=FakeIMAP()) as i, \
                mock.patch.object(mail, "smtp_connect", return_value=mock.MagicMock()) as s:
            mail.test_account(self.ACC)
        i.assert_called_once()
        s.assert_called_once()

    def test_pool_reconnects(self):
        conns = [FakeIMAP(), FakeIMAP()]
        pool = mail.ImapPool()
        with mock.patch.object(mail, "imap_connect", side_effect=conns):
            with pool.get(self.ACC) as M:
                self.assertIs(M, conns[0])
            with self.assertRaises(imaplib.IMAP4.error):  # NO/BAD – spojení zůstává
                with pool.get(self.ACC):
                    raise imaplib.IMAP4.error("NO")
            with pool.get(self.ACC) as M:
                self.assertIs(M, conns[0])
            with self.assertRaises(imaplib.IMAP4.abort):  # přerušené spojení se zahodí
                with pool.get(self.ACC):
                    raise imaplib.IMAP4.abort("gone")
            with pool.get(self.ACC) as M:
                self.assertIs(M, conns[1])
            with self.assertRaises(ValueError):
                with pool.get(self.ACC):
                    raise ValueError()
            self.assertIn("logout", conns[1].calls)
            pool.drop("a")


if __name__ == "__main__":
    unittest.main()
