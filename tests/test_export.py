"""Uložení zprávy jako .txt a všech příloh (kontextové menu nad otevřenou zprávou)."""

import os
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import mail  # noqa: E402


def raw_message(html=False, attachments=()):
    msg = EmailMessage()
    msg["From"] = "Eva Nová <eva@example.com>"
    msg["To"] = "me@example.com"
    msg["Subject"] = "Faktura: březen/2026"
    msg["Date"] = "Thu, 26 Nov 2026 10:00:00 +0100"
    if html:
        msg.set_content("<p>Dobrý den,<br>posílám <b>fakturu</b>.</p>", subtype="html")
    else:
        msg.set_content("Dobrý den,\nposílám fakturu.\n")
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


class ExportTest(unittest.TestCase):
    def test_text_has_headers_and_body(self):
        text = mail.message_as_text(raw_message(attachments=[("f.pdf", b"%PDF")]))
        self.assertIn("Eva Nová <eva@example.com>", text)
        self.assertIn("Faktura: březen/2026", text)
        self.assertIn("f.pdf", text)
        self.assertIn("posílám fakturu.", text)
        self.assertNotIn("\n", text.replace("\r\n", ""))  # konce řádků jako ve Windows

    def test_text_from_html(self):
        text = mail.message_as_text(raw_message(html=True))
        self.assertIn("fakturu", text)
        self.assertNotIn("<b>", text)

    def test_all_attachments(self):
        files = mail.all_attachments(raw_message(attachments=[("a.pdf", b"1"), ("b.pdf", b"22")]))
        self.assertEqual([(n, d) for n, _, d in files], [("a.pdf", b"1"), ("b.pdf", b"22")])
        self.assertEqual(mail.all_attachments(raw_message()), [])


if __name__ == "__main__":
    unittest.main()
