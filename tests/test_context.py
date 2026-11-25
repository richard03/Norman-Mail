"""Kontext: diskusní vlákno podle In-Reply-To / References, protistrana, stejný předmět."""

import email
import os
import sys
import tempfile
import unittest
from email import policy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import db  # noqa: E402
import mail  # noqa: E402

ME = "me@example.com"
FOLDERS = [{"name": "INBOX", "display": "Doručená pošta", "role": "inbox"},
           {"name": "Sent", "display": "Odeslané", "role": "sent"},
           {"name": "All", "display": "Všechny zprávy", "role": "all"}]


def rec(uid, sender, subject, to=ME, refs=None, date=0, mid=None, cc=()):
    r = {"uid": uid, "message_id": mid or f"<{uid}@x>", "subject": subject,
         "base_subject": mail.base_subject(subject).lower(),
         "from": [{"name": "", "addr": sender}], "to": [{"name": "", "addr": to}],
         "cc": [{"name": "", "addr": a} for a in cc], "date": 1000 + (date or uid), "seen": True,
         "flagged": False, "has_attachments": False, "size": 100}
    if refs is not None:
        r["refs"] = refs
    return r


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.d = db.Database(Path(tempfile.mkdtemp()) / "test.db")

    def related(self, uid, folder="INBOX", limit=100):
        row = self.d.message("a", folder, uid)
        cp = row["from"][0]["addr"]
        if cp == ME:
            cp = row["to"][0]["addr"]
        found = self.d.related("a", FOLDERS, ME, cp, row["base_subject"], row["message_id"],
                               self.d.refs_of("a", folder, uid), limit)
        return {(m["folder"], m["uid"]): m["reasons"] for m in found}

    def test_thread_headers(self):
        h = email.message_from_string("In-Reply-To: <b@x>\nReferences: <a@x>\n <b@x> <c@x>\n\n", policy=policy.compat32)
        self.assertEqual(mail.thread_refs(h), ["<b@x>", "<a@x>", "<c@x>"])

    def test_thread_follows_references_despite_subject_change(self):
        self.d.insert_messages("a", "INBOX", [
            rec(1, "eva@x.cz", "Projekt", refs=[]),
            rec(3, "eva@x.cz", "Úplně jiný předmět", refs=["<1@x>", "<2@x>"]),
            rec(4, "petr@y.cz", "Projekt", refs=[]),               # cizí člověk se stejným předmětem
            rec(5, "jan@z.cz", "Odpověď na odpověď", refs=["<3@x>"]),  # odpovídá na zprávu z vlákna
        ])
        self.d.insert_messages("a", "Sent", [rec(2, ME, "Re: Projekt", to="eva@x.cz", refs=["<1@x>"])])
        got = self.related(1)
        self.assertIn("thread", got[("Sent", 2)])
        self.assertIn("thread", got[("INBOX", 3)])
        self.assertIn("thread", got[("INBOX", 5)])   # druhé kolo přes nalezená ID
        self.assertEqual(got[("INBOX", 4)], ["subject"])
        # z prostředka vlákna najde i předchozí zprávy (podle vlastních References)
        got = self.related(3)
        self.assertIn("thread", got[("INBOX", 1)])
        self.assertIn("thread", got[("Sent", 2)])

    def test_fallback_for_messages_without_refs(self):
        self.d.insert_messages("a", "INBOX", [rec(1, "eva@x.cz", "Projekt"), rec(2, "eva@x.cz", "Re: Projekt"),
                                              rec(3, "petr@y.cz", "Projekt")])
        got = self.related(1)
        self.assertIn("thread", got[("INBOX", 2)])
        self.assertNotIn("thread", got[("INBOX", 3)])  # stejný předmět, ale jiný člověk
        self.d.set_refs("a", "INBOX", {2: []})       # hlavičky doplněné: odpověď bez odkazu do vlákna nepatří
        self.assertNotIn("thread", self.related(1)[("INBOX", 2)])

    def test_frequent_sender_does_not_push_out_thread(self):
        recs = [rec(1, "eva@x.cz", "Začátek", refs=[], date=1)]
        recs += [rec(100 + i, "eva@x.cz", f"Zpráva {i}", refs=[], date=100 + i) for i in range(150)]
        recs.append(rec(2, "eva@x.cz", "Re: Začátek", refs=["<1@x>"], date=2))
        self.d.insert_messages("a", "INBOX", recs)
        got = self.related(1, limit=100)
        self.assertIn("thread", got[("INBOX", 2)])
        self.assertEqual(sum("sender" in r for r in got.values()), 101)  # 100 nejnovějších + zpráva z vlákna

    def test_gmail_duplicate_once(self):
        self.d.insert_messages("a", "INBOX", [rec(1, "eva@x.cz", "Projekt", refs=[]),
                                              rec(2, "eva@x.cz", "Re: Projekt", refs=["<1@x>"])])
        self.d.insert_messages("a", "All", [rec(7, "eva@x.cz", "Re: Projekt", refs=["<1@x>"], mid="<2@x>")])
        got = self.related(1)
        self.assertIn(("INBOX", 2), got)
        self.assertNotIn(("All", 7), got)

    def test_backfill_helpers(self):
        self.d.insert_messages("a", "INBOX", [rec(1, "eva@x.cz", "A"), rec(2, "eva@x.cz", "B", refs=["<1@x>"])])
        self.assertEqual(self.d.missing_refs("a", "INBOX", 10), [1])
        self.assertIsNone(self.d.refs_of("a", "INBOX", 1))
        self.d.set_refs("a", "INBOX", {1: []})
        self.assertEqual((self.d.missing_refs("a", "INBOX", 10), self.d.refs_of("a", "INBOX", 1)), ([], []))
        self.assertEqual(self.d.refs_of("a", "INBOX", 2), ["<1@x>"])


if __name__ == "__main__":
    unittest.main()
