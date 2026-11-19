"""Testy lokalizace: .venv\\Scripts\\python -m unittest discover tests"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MAILER_DATA", tempfile.mkdtemp())

import i18n  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_plain_and_quoted(self):
        d = i18n.parse('a: Hello: world\nb: "Quoted # not comment"\nc: \'It\'\'s\'\nd: text # comment\n')
        self.assertEqual(d, {"a": "Hello: world", "b": "Quoted # not comment", "c": "It's", "d": "text"})

    def test_no_type_conversion(self):
        d = i18n.parse("a: no\nb: yes\nc: 1.0\nd: null\ne: ~\n")
        self.assertEqual(d, {"a": "no", "b": "yes", "c": "1.0", "d": "null", "e": "~"})

    def test_block_literal_and_folded(self):
        d = i18n.parse("a: |\n  line 1\n    indented\n\n  line 3\nb: >\n  joined\n  text\n\n  second\nc: x\n")
        self.assertEqual(d["a"], "line 1\n  indented\n\nline 3")
        self.assertEqual(d["b"], "joined text\nsecond")
        self.assertEqual(d["c"], "x")

    def test_nested_plural_and_bom(self):
        d = i18n.parse('\ufeffmeta:\n  language: cs\n  name: Čeština\nn:\n  one: "{count} zpráva"\n  other: "{count} zpráv"\n')
        self.assertEqual(d["meta"], {"language": "cs", "name": "Čeština"})
        self.assertEqual(d["n"], {"one": "{count} zpráva", "other": "{count} zpráv"})

    def test_escapes(self):
        self.assertEqual(i18n.parse(r'a: "x\ny \"q\""' + "\n")["a"], 'x\ny "q"')

    def test_errors_have_line_numbers(self):
        for text, line in [("a: x\n  b: y\n", 2), ("a: x\na: y\n", 2), ("a: \"open\n", 1),
                           ("ok: x\n\tb: y\n", 2), ("klíč: x\n", 1), ("just text\n", 1),
                           ("a: [x]\n", 1), ("m:\n  a: x\n   b: y\n", 3), ("a:\n", 1)]:
            with self.subTest(text=text):
                with self.assertRaises(i18n.ParseError) as cm:
                    i18n.parse(text)
                self.assertEqual(cm.exception.line, line)

    def test_validate(self):
        meta, texts = i18n.validate({"meta": {"language": "pt-BR", "name": "Português"}, "a": "b"})
        self.assertEqual((meta["language"], meta["locale"], texts), ("pt-BR", "pt-BR", {"a": "b"}))
        for bad in [{"a": "b"}, {"meta": {"language": "Czech"}}, {"meta": {"language": "cs", "x": "y"}},
                    {"meta": {"language": "cs"}, "n": {"one": "a"}}, {"meta": {"language": "cs"}, "n": {"foo": "a", "other": "b"}}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                i18n.validate(bad)


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._dir = i18n.user_dir
        i18n.user_dir = lambda: self.dir

    def tearDown(self):
        i18n.user_dir = self._dir
        i18n.reload()
        i18n.set_language("en")

    def test_builtin_catalogs_are_valid_and_complete(self):
        i18n.reload()
        en = set(i18n.catalog("en")["texts"])
        for lang in i18n.languages():
            if lang["builtin"]:
                self.assertEqual(lang["missing"], 0, f"{lang['code']}: chybí překlady")
        self.assertIn("cs", {l["code"] for l in i18n.languages()})
        self.assertTrue(en)

    def test_user_file_override_new_language_and_bad_file(self):
        (self.dir / "cs.yaml").write_text("meta:\n  language: cs\nsettings.app.title: Můj titulek\n", "utf-8")
        (self.dir / "xx.yaml").write_text("meta:\n  language: xx\n  name: Test\nsettings.app.title: <b>x</b>\n", "utf-8")
        (self.dir / "bad.yaml").write_text("meta:\n  language: de\nbroken\n", "utf-8")
        i18n.reload()
        self.assertEqual(i18n.catalog("cs")["texts"]["settings.app.title"], "Můj titulek")
        langs = {l["code"]: l for l in i18n.languages()}
        self.assertIn("xx", langs)
        self.assertGreater(langs["xx"]["missing"], 0)
        self.assertNotIn("de", langs)
        self.assertEqual(len(i18n.problems()), 1)
        self.assertIn(" 3: ", i18n.problems()[0]["error"])  # „řádek 3: …“ / „line 3: …“

    def test_t_params_plural_and_fallback(self):
        (self.dir / "cs.yaml").write_text(
            'meta:\n  language: cs\nt.n:\n  one: "{count} zpráva"\n  few: "{count} zprávy"\n  other: "{count} zpráv"\n'
            't.p: "Ahoj {name} {x.__class__}"\n', "utf-8")
        i18n.reload()
        i18n.set_language("cs")
        self.assertEqual([i18n.t("t.n", count=c) for c in (1, 3, 5)], ["1 zpráva", "3 zprávy", "5 zpráv"])
        self.assertEqual(i18n.t("t.p", name="Eva"), "Ahoj Eva {x.__class__}")
        self.assertEqual(i18n.t("does.not.exist"), "does.not.exist")

    def test_keys_used_in_code_exist(self):
        """Každý klíč z kódu (t('…'), tr("…"), data-i18n) je v angličtině i češtině."""
        import re
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "app.js").read_text("utf-8")
        html = (root / "static" / "index.html").read_text("utf-8")
        py = "".join((root / f).read_text("utf-8") for f in
                     ("app.py", "mail.py", "sync.py", "store.py", "google_oauth.py", "db.py", "main.py", "taskbar.py"))
        keys = set(re.findall(r"\bt[n]?\(\s*'([A-Za-z0-9_.\-]+)'", js))
        keys |= set(re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', html))
        keys |= set(re.findall(r"dataset\.i18n\w*\s*=\s*'([^']+)'", js))
        keys |= set(re.findall(r"""\b(?:tr|_tr|i18n\.t)\(\s*["']([A-Za-z0-9_.\-]+)["']""", py))
        i18n.reload()
        for lang in ("en", "cs"):
            texts = i18n._catalogs[lang]["texts"]
            self.assertEqual(sorted(k for k in keys if k not in texts), [], f"{lang}: chybějící klíče")

    def test_resolve(self):
        i18n.reload()
        self.assertEqual(i18n.resolve("cs"), "cs")
        self.assertEqual(i18n.resolve("zz"), "en")
        self.assertEqual(i18n.resolve(""), "en")  # nezvolený jazyk = angličtina, ne jazyk Windows

    def test_has_user_files(self):
        i18n.reload()
        self.assertFalse(i18n.has_user_files())
        (self.dir / "de.yaml").write_text("meta:\n  language: de\n  name: Deutsch\nnav.important: Wichtig\n", "utf-8")
        i18n.reload()
        self.assertTrue(i18n.has_user_files())


if __name__ == "__main__":
    unittest.main()
