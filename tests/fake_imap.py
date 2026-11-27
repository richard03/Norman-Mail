"""Falešný IMAP server pro testy: v paměti, rozhraní jako imaplib.IMAP4 (jen to, co používá mail.py)."""

import email
import imaplib
import re
import sys
from contextlib import contextmanager
from email import policy
from email.header import Header
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mail  # noqa: E402

ROLE_FLAG = {"sent": "\\Sent", "trash": "\\Trash", "junk": "\\Junk", "archive": "\\Archive",
             "all": "\\All", "drafts": "\\Drafts"}


def make_raw(sender="eva@example.com", to="me@example.com", subject="Ahoj", body="Text zprávy",
             mid=None, date="Mon, 02 Jan 2023 10:00:00 +0000", extra="", html=None, cc=None):
    """Jednoduchá zpráva jako bajty."""
    if not subject.isascii():
        subject = Header(subject, "utf-8").encode()
    headers = [f"From: {sender}", f"To: {to}", f"Subject: {subject}", f"Date: {date}"]
    if cc:
        headers.append(f"Cc: {cc}")
    if mid:
        headers.append(f"Message-ID: {mid}")
    if extra:
        headers += [h for h in extra.split("\n") if h]
    if html is not None:
        headers += ["MIME-Version: 1.0", 'Content-Type: multipart/alternative; boundary="B"']
        text = (f"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
                f"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n{html}\r\n--B--\r\n")
    else:
        headers += ["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8"]
        text = body
    return ("\r\n".join(headers) + "\r\n\r\n" + text).encode("utf-8")


class FakeIMAP:
    def __init__(self, folders=(("INBOX", None),), caps=("IMAP4REV1", "UIDPLUS", "MOVE"), delim="/"):
        self.folders = {}
        self.delim = delim
        self.caps = set(caps)
        self.selected = None
        self.literal = None
        self.calls = []
        self.fail = {}          # příkaz -> výsledek ("NO") nebo výjimka
        self.size_override = {}  # (složka, uid) -> nahlášená velikost
        for name, role in folders:
            self.add_folder(name, role)

    # ---- příprava dat
    def add_folder(self, name, role=None, flags=()):
        self.folders[name] = {"role": role, "uidvalidity": 1, "next": 1, "msgs": {},
                              "flags": list(flags)}

    def add(self, folder, raw, flags=(), idate="02-Jan-2023 10:00:00 +0000"):
        f = self.folders[folder]
        uid = f["next"]
        f["next"] += 1
        f["msgs"][uid] = {"raw": raw, "flags": set(flags), "idate": idate}
        return uid

    def uids(self, folder):
        return sorted(self.folders[folder]["msgs"])

    def flags(self, folder, uid):
        return self.folders[folder]["msgs"][uid]["flags"]

    # ---- pomocné
    def _check(self, cmd):
        self.calls.append(cmd)
        r = self.fail.get(cmd)
        if isinstance(r, BaseException) or (isinstance(r, type) and issubclass(r, BaseException)):
            raise r
        return r

    @staticmethod
    def _name(quoted):
        return mail._unquote(quoted)

    def _msgs(self):
        return self.folders[self.selected]["msgs"]

    def _uidset(self, s):
        msgs = self._msgs()
        out = []
        for part in str(s).split(","):
            if ":" in part:
                a, b = part.split(":")
                hi = max(msgs) if msgs else 0
                a = int(a)
                b = hi if b == "*" else int(b)
                out += [u for u in sorted(msgs) if a <= u <= b]
            elif int(part) in msgs:
                out.append(int(part))
        return out

    def _header(self, uid, name):
        h = email.message_from_bytes(self._msgs()[uid]["raw"], policy=policy.compat32)
        return str(h.get(name) or "")

    def _match(self, uid, key, value):
        key = key.upper()
        value = value.lower()
        if key == "FROM":
            return value in self._header(uid, "From").lower()
        if key == "TO":
            return value in self._header(uid, "To").lower()
        if key == "CC":
            return value in self._header(uid, "Cc").lower()
        if key == "SUBJECT":
            return value in mail.dec(self._header(uid, "Subject")).lower()
        if key == "TEXT":
            return (value in self._msgs()[uid]["raw"].decode("utf-8", "replace").lower()
                    or value in mail.dec(self._header(uid, "Subject")).lower())
        raise AssertionError(key)

    # ---- příkazy imaplib
    def capability(self):
        return "OK", [" ".join(sorted(self.caps)).encode()]

    def noop(self):
        self._check("noop")
        return "OK", [b""]

    def logout(self):
        self.calls.append("logout")
        return "BYE", [b""]

    def shutdown(self):
        self.calls.append("shutdown")

    def list(self):
        r = self._check("list")
        if r:
            return r, [None]
        out = []
        for name, f in self.folders.items():
            flags = list(f["flags"]) + ([ROLE_FLAG[f["role"]]] if f["role"] in ROLE_FLAG else [])
            out.append(f'({" ".join(flags)}) "{self.delim}" "{name}"'.encode())
        return "OK", out

    def status(self, qname, items):
        if self._check("status"):
            return "NO", [None]
        name = self._name(qname)
        if name not in self.folders:
            raise imaplib.IMAP4.error("no such folder")
        f = self.folders[name]
        unseen = sum(1 for m in f["msgs"].values() if "\\Seen" not in m["flags"])
        return "OK", [f'"{name}" (UNSEEN {unseen} UIDVALIDITY {f["uidvalidity"]} MESSAGES {len(f["msgs"])})'.encode()]

    def select(self, qname, readonly=False):
        name = self._name(qname)
        if self._check("select") or name not in self.folders:
            return "NO", [b"no such folder"]
        self.selected = name
        return "OK", [str(len(self.folders[name]["msgs"])).encode()]

    def create(self, qname):
        if self._check("create"):
            return "NO", [b""]
        self.add_folder(self._name(qname))
        return "OK", [b""]

    def subscribe(self, qname):
        self._check("subscribe")
        return "OK", [b""]

    def delete(self, qname):
        if self._check("delete"):
            return "NO", [b""]
        del self.folders[self._name(qname)]
        return "OK", [b""]

    def expunge(self):
        self.calls.append("expunge")
        msgs = self._msgs()
        for u in [u for u, m in msgs.items() if "\\Deleted" in m["flags"]]:
            del msgs[u]
        return "OK", [b""]

    def append(self, qname, flags, date, raw):
        r = self._check("append")
        if r:
            return r, [b""]
        name = self._name(qname)
        fl = flags.strip("()").split()
        idate = date.strip('"') if isinstance(date, str) else "x"
        self.add(name, raw, fl, idate)
        return "OK", [b""]

    def authenticate(self, mech, cb):
        self._check("authenticate")
        self.auth = cb(b"")
        return "OK", [b""]

    def login(self, user, password):
        self._check("login")
        self.user = (user, password)
        return "OK", [b""]

    def starttls(self, ssl_context=None):
        self.calls.append("starttls")

    def _fetch_items(self, uids, spec, seq=None):
        out = []
        msgs = self._msgs()
        for i, uid in enumerate(uids):
            m = msgs[uid]
            raw = m["raw"]
            meta = f"{(seq or {}).get(uid, i + 1)} (UID {uid}"
            if "FLAGS" in spec:
                meta += f" FLAGS ({' '.join(sorted(m['flags']))})"
            if "RFC822.SIZE" in spec:
                meta += f" RFC822.SIZE {self.size_override.get((self.selected, uid), len(raw))}"
            if "INTERNALDATE" in spec:
                meta += f' INTERNALDATE "{m["idate"]}"'
            lit = None
            if "HEADER.FIELDS" in spec:
                lit = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            else:
                part = re.search(r"BODY\.PEEK\[\](?:<0\.(\d+)>)?", spec)
                if part:
                    lit = raw[:int(part.group(1))] if part.group(1) else raw
            if lit is None:
                out.append((meta + ")").encode())
            else:
                out.append(((meta + f" BODY[] {{{len(lit)}}}").encode(), lit))
                out.append(b")")
        return out

    def fetch(self, seqset, spec):
        self._check("fetch")
        msgs = sorted(self._msgs())
        a, b = seqset.split(":")
        chosen = msgs[int(a) - 1:int(b)]
        return "OK", self._fetch_items(chosen, spec, {u: msgs.index(u) + 1 for u in chosen})

    def uid(self, cmd, *args):
        cmd = cmd.upper()
        r = self._check("uid " + cmd)
        if r:
            return r, [None]
        msgs = self._msgs()
        if cmd == "SEARCH":
            return "OK", [" ".join(str(u) for u in self._search(args)).encode()]
        if cmd == "FETCH":
            return "OK", self._fetch_items(self._uidset(args[0]), args[1])
        if cmd == "STORE":
            uids, op, fl = self._uidset(args[0]), args[1], args[2].strip("()").split()
            for u in uids:
                if op.startswith("+"):
                    msgs[u]["flags"] |= set(fl)
                else:
                    msgs[u]["flags"] -= set(fl)
            return "OK", [b""]
        if cmd in ("MOVE", "COPY"):
            dest = self._name(args[1])
            if dest not in self.folders:
                return "NO", [b"no dest"]
            for u in self._uidset(args[0]):
                m = msgs[u]
                self.add(dest, m["raw"], m["flags"], m["idate"])
                if cmd == "MOVE":
                    del msgs[u]
            return "OK", [b""]
        if cmd == "EXPUNGE":
            for u in self._uidset(args[0]):
                if "\\Deleted" in msgs[u]["flags"]:
                    del msgs[u]
            return "OK", [b""]
        raise AssertionError(cmd)

    def _search(self, args):
        msgs = self._msgs()
        if args == ("ALL",):
            return sorted(msgs)
        if args[:2] == ("CHARSET", "UTF-8"):
            value, self.literal = self.literal.decode("utf-8"), None
            return [u for u in sorted(msgs) if self._match(u, args[2], value)]
        if args[0] == "HEADER":
            value = self._name(args[2])
            return [u for u in sorted(msgs) if value in self._header(u, args[1])]
        if len(args) == 1 and args[0].startswith("OR OR "):
            # OR OR FROM "cp" CC "cp" (FROM "me" TO "cp")
            vals = re.findall(r'"((?:[^"\\]|\\.)*)"', args[0])
            cp, me = vals[0], vals[2]
            return [u for u in sorted(msgs) if self._match(u, "FROM", cp) or self._match(u, "CC", cp)
                    or (self._match(u, "FROM", me) and self._match(u, "TO", cp))]
        if len(args) == 2:
            return [u for u in sorted(msgs) if self._match(u, args[0], self._name(args[1]))]
        raise AssertionError(args)


class FakePool:
    """Náhrada mail.ImapPool: vždy vrací stejný FakeIMAP pro účet."""

    def __init__(self, servers):
        self.servers = servers  # acc_id -> FakeIMAP

    @contextmanager
    def get(self, acc):
        yield self.servers[acc["id"]]

    def drop(self, acc_id):
        pass
