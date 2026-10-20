"""Deterministic logic that doesn't need a model: routing checks, topic filter, text handling."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shellmate import errors, news, packages, prompts, rag, router, ui  # noqa: E402
from shellmate.evidence import Sentence, off_topic  # noqa: E402


class Router(unittest.TestCase):
    def test_parse_normalises(self):
        it = router.parse('{"intent":"install","packages":["OBS Studio","obs-studio"],"text":"","action":"x"}')
        self.assertEqual((it.intent, it.packages, it.action), ("install", ["obs-studio"], ""))
        self.assertEqual(router.parse("garbage").intent, "unknown")

    def test_doubts_catch_confident_mistakes(self):
        D = router.doubts
        self.assertTrue(D(router.Intent("install", ["obs-studio"]), "поставь обсидиан", 0.5, 0.5))
        self.assertFalse(D(router.Intent("install", ["obsidian"]), "поставь обсидиан", 0.5, 0.5))
        self.assertFalse(D(router.Intent("install", ["steam", "discord"]), "поставь стим и дискорд", 0.5, 0.5))
        self.assertTrue(D(router.Intent("install", ["telegram-desktop"]), "снеси телегу", 0.5, 0.5))
        self.assertTrue(D(router.Intent("processes", ["obs-studio"]), "кинь мне обс", 0.5, 0.5))
        self.assertTrue(D(router.Intent("unknown"), "что-то", 0.5, 0.5))
        self.assertTrue(D(router.Intent("web", intent_p=0.3), "find me a torrent client", 0.5, 0.5))
        self.assertFalse(D(router.Intent("remove", ["obs-studio"]), "а теперь удали его", 0.5, 0.5))

    def test_deep_and_follow_up(self):
        self.assertEqual(router.wants_deep("подумай хорошенько, как настроить hdr"), (True, "как настроить hdr"))
        self.assertEqual(router.wants_deep("проверь внимательно систему"), (True, "проверь систему"))
        self.assertEqual(router.wants_deep("обнови систему"), (False, "обнови систему"))
        self.assertTrue(router.is_follow_up("а теперь удали его"))
        self.assertTrue(router.is_follow_up("и ещё дискорд"))
        self.assertFalse(router.is_follow_up("кинь мне обс"))


class TopicFilter(unittest.TestCase):
    def test_other_program_is_off_topic(self):
        be = mock.Mock(context_words="arch pacman")
        with mock.patch("shellmate.backends.current", return_value=be):
            self.assertTrue(off_topic(Sentence("x", "Add render_bit_depth 10.", "HDR › Sway"), "hdr in hyprland"))
            self.assertFalse(off_topic(Sentence("x", "Set cm = hdr.", "HDR › Hyprland"), "hdr in hyprland"))
            self.assertTrue(off_topic(Sentence("x", "Run sudo apt install foo."), "как поставить пакет"))
            self.assertFalse(off_topic(Sentence("x", "The x86 architecture needs pacman -S foo."), "как поставить пакет"))

    def test_debian_family_on_ubuntu(self):
        be = mock.Mock(context_words="debian ubuntu apt")
        with mock.patch("shellmate.backends.current", return_value=be):
            self.assertFalse(off_topic(Sentence("x", "On Debian run sudo apt install foo."), "как поставить foo"))
            self.assertTrue(off_topic(Sentence("x", "Use sudo pacman -S foo."), "как поставить foo"))
            self.assertTrue(off_topic(Sentence("x", "On Fedora run dnf install foo."), "как поставить foo"))


class Text(unittest.TestCase):
    def test_chunks_keep_sections(self):
        html = "<h2>Install</h2><p>Use the package manager to install it.</p><h3>Hyprland</h3><p>Set cm = hdr here.</p>"
        chunks = rag.chunks(rag.html_to_text(html))
        self.assertTrue(chunks[-1].startswith("§ Install › Hyprland"), chunks)

    def test_code_attached_to_sentence(self):
        text = "To remove unused packages and sync databases, execute:\n\n# pacman -Sc\n\nAnother paragraph here."
        sents = [s for _, s in rag.sentences([("x", text)])]
        self.assertIn("To remove unused packages and sync databases, execute:\n# pacman -Sc", sents)

    def test_meta_phrases_removed(self):
        self.assertEqual(rag.core_question("как очистить кэш, найди в инете"), "как очистить кэш")

    def test_wrap_keeps_indent(self):
        ui._COLOR = False
        lines = ui.wrap("  To remove all files from the cache use the clean switch twice", 30)
        self.assertTrue(all(line.startswith("  ") for line in lines), lines)


class Safety(unittest.TestCase):
    def test_defang(self):
        text, removed = prompts.defang("ls -la\nrm -rf /tmp/x\nsudo pacman -S foo\nsudo chattr -i f")
        self.assertTrue(removed)
        self.assertEqual(text.count("⚠"), 2)
        self.assertIn("sudo pacman -S foo", text)

    def test_error_dictionary(self):
        cases = {"error: target not found: foo": "foo", "ошибка: не найдена цель: bar": "bar",
                 "E: Unable to locate package baz": "baz", "No match for argument: qux": "qux"}
        for line, name in cases.items():
            hits = errors.match(line)
            self.assertTrue(hits and name in hits[0][0], line)


class Misc(unittest.TestCase):
    def test_translit(self):
        self.assertEqual(packages.spelled_words("поставь обсидиан пожалуйста"), ["obsidian"])

    def test_news_package_detection(self):
        installed = {"mkinitcpio", "which", "linux"}
        self.assertEqual(news._affected("Mkinitcpio >=42 requires manual intervention", installed), ["mkinitcpio"])
        self.assertEqual(news._affected("Breaking changes for `varnish`, which is renamed", installed), [])
        self.assertEqual(news._affected("Arch Linux 2026 Leader Election Results", installed), [])


if __name__ == "__main__":
    unittest.main()


class Config(unittest.TestCase):
    def test_flat_toml_matches_tomllib(self):
        from shellmate import config
        text = ('model = "bonsai-2:4b"\nuse_flatpak = true\nmax_results = 15\nescalate_quotes = 0.7\n'
                'docs = ["/home/u/Notes", "/tmp/x"]\naddress = "босс"\nstyle_custom = \'как пират\'  # comment\n')
        flat = config._read_flat_toml(text)
        self.assertEqual(flat["model"], "bonsai-2:4b")
        self.assertIs(flat["use_flatpak"], True)
        self.assertEqual((flat["max_results"], flat["escalate_quotes"]), (15, 0.7))
        self.assertEqual(flat["docs"], ["/home/u/Notes", "/tmp/x"])
        self.assertEqual((flat["address"], flat["style_custom"]), ("босс", "как пират"))
        try:
            import tomllib
        except ModuleNotFoundError:
            return
        self.assertEqual({k: v for k, v in tomllib.loads(text).items()}, flat)
