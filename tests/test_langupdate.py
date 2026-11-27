"""Oficiální překlady z vydání: stažení ověřené podpisem a aktualizace se zachováním úprav uživatele."""

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import ed25519  # noqa: E402
import i18n  # noqa: E402
import langupdate  # noqa: E402
import updater  # noqa: E402
from test_updater import PUBLIC, SECRET, _Quiet  # noqa: E402

OLD = '''meta:
  language: cs
  name: Čeština
nav.important: "Důležité"
nav.spam: "Spam"
old.removed: "Zrušený text"
msg.count:
  one: "{count} zpráva"
  other: "{count} zpráv"
'''
NEW = '''meta:
  language: cs
  name: Čeština
nav.important: "Důležité zprávy"
nav.spam: "Nevyžádaná pošta"
msg.count:
  one: "{count} zpráva"
  few: "{count} zprávy"
  other: "{count} zpráv"
new.text: "Nový text"
'''


def release(web: Path, version, files: dict, languages=True):
    folder = web / f"v{version}"
    folder.mkdir(parents=True)
    langs = {}
    for name, text in files.items():
        data = text.encode("utf-8")
        (folder / name).write_bytes(data)
        langs[name[:-5]] = {"name": "Čeština", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    info = {"version": version, "sha256": "0" * 64, "size": 1, "notes": ""}
    if languages:
        info["languages"] = langs
    body = json.dumps(info).encode()
    (folder / "latest.json").write_bytes(body)
    (folder / "latest.json.sig").write_text(ed25519.sign(SECRET, body).hex())


class MergeTest(unittest.TestCase):
    def test_merge_keeps_user_edits(self):
        old = {"a": "A", "b": "B", "gone": "G", "edited_gone": "x"}
        new = {"a": "A2", "b": "B2", "c": "C"}
        user = {"a": "A", "b": "Moje B", "gone": "G", "edited_gone": "moje", "own": "vlastní"}
        self.assertEqual(langupdate.merge(user, [old], new),
                         {"a": "A2", "b": "Moje B", "c": "C", "edited_gone": "moje", "own": "vlastní"})
        # víc předchozích znění (soubor bez verze): shoda s kterýmkoli = neupraveno
        self.assertEqual(langupdate.merge({"a": "A1"}, [{"a": "A0"}, {"a": "A1"}], {"a": "A2"}), {"a": "A2"})

    def test_dump_roundtrip(self):
        meta = {"language": "cs", "name": "Čeština", "locale": "cs-CZ", "app_version": "1.0.5"}
        texts = {"a": 'Uvozovky "x", lomítko \\ a\nnový řádek', "b": {"one": "{count} kus", "other": "{count} kusů"},
                 "c": "# není komentář: ano"}
        path = Path(tempfile.mkdtemp()) / "cs.yaml"
        path.write_text(langupdate.dump(meta, texts), "utf-8")
        self.assertEqual(i18n.load_file(path), (meta, texts))


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.web = self.tmp / "web"
        self.langs = self.tmp / "languages"
        self.web.mkdir()
        self.langs.mkdir()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(self.web)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_address[1]
        self.patches = [
            mock.patch.object(updater, "PUBLIC_KEY", PUBLIC),
            mock.patch.object(langupdate, "VERSION", "1.0.5"),
            mock.patch.object(i18n, "user_dir", lambda: self.langs),
            mock.patch.dict(os.environ, {"NORMANMAIL_UPDATE_URL": f"http://127.0.0.1:{port}"}),
        ]
        for p in self.patches:
            p.start()
        langupdate._manifest = None
        release(self.web, "1.0.4", {"cs.yaml": OLD}, languages=False)  # starší vydání bez kontrolních součtů
        release(self.web, "1.0.5", {"cs.yaml": NEW})

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.server.shutdown()
        self.server.server_close()
        langupdate._manifest = None
        i18n.reload()

    def user_file(self, version="1.0.4"):
        text = OLD.replace('nav.spam: "Spam"', 'nav.spam: "Můj spam"')
        if version:
            text = text.replace("  name: Čeština", f"  name: Čeština\n  app_version: {version}")
        path = self.langs / "cs.yaml"
        path.write_text(text, "utf-8")
        return path

    def test_update_keeps_user_edits(self):
        path = self.user_file()
        gen = langupdate.generation
        self.assertTrue(langupdate.update_all())
        meta, texts = i18n.load_file(path)
        self.assertEqual(meta["app_version"], "1.0.5")
        self.assertEqual(texts, {"nav.important": "Důležité zprávy", "nav.spam": "Můj spam",
                                 "msg.count": {"one": "{count} zpráva", "few": "{count} zprávy",
                                               "other": "{count} zpráv"},
                                 "new.text": "Nový text"})
        self.assertTrue((self.langs / "_cs.yaml.bak").exists())
        self.assertEqual(langupdate.generation, gen + 1)
        self.assertEqual(i18n.catalog("cs")["texts"]["new.text"], "Nový text")
        # podruhé už nic (soubor patří k běžící verzi)
        before = path.read_text("utf-8")
        self.assertTrue(langupdate.update_all())
        self.assertEqual(path.read_text("utf-8"), before)

    def test_tampered_translation_is_rejected(self):
        path = self.user_file()
        before = path.read_text("utf-8")
        (self.web / "v1.0.5" / "cs.yaml").write_text(NEW.replace("Nový text", "Zadejte heslo"), "utf-8")
        self.assertFalse(langupdate.update_all())
        self.assertEqual(path.read_text("utf-8"), before)

    def test_file_without_version_uses_all_releases(self):
        path = self.user_file(version=None)
        with mock.patch.object(langupdate, "all_bases", lambda code: [i18n.validate(i18n.parse(OLD))[1]]):
            self.assertTrue(langupdate.update_all())
        self.assertEqual(i18n.load_file(path)[1]["nav.important"], "Důležité zprávy")
        # bez předchozího znění se soubor nepřepíše (staré texty by se jinak tvářily jako úpravy)
        path = self.user_file(version=None)
        before = path.read_text("utf-8")
        with mock.patch.object(langupdate, "all_bases", lambda code: []):
            self.assertFalse(langupdate.update_all())
        self.assertEqual(path.read_text("utf-8"), before)

    def test_install_and_official(self):
        self.assertEqual(list(langupdate.official()), ["cs"])
        path = langupdate.install("cs")
        meta, texts = i18n.load_file(path)
        self.assertEqual((meta["app_version"], texts["new.text"]), ("1.0.5", "Nový text"))
        with self.assertRaises(langupdate.LanguageError):
            langupdate.install("de")

    def test_missing_release_is_quiet(self):
        with mock.patch.object(langupdate, "VERSION", "9.9.9"):
            self.assertTrue(langupdate.update_all())  # vývojová verze bez vydání


if __name__ == "__main__":
    unittest.main()
