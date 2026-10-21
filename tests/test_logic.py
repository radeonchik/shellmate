"""Deterministic logic that doesn't need a model: routing checks, topic filter, text handling."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shellmate import errors, hardware, news, packages, prompts, rag, router, ui  # noqa: E402
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

    def test_wrap_carries_styles(self):
        with mock.patch.object(ui, "_COLOR", True):
            line = "¹ " + ui.dim(ui.italic("one two three four five six")) + " " + ui.bold("seven") + " eight nine"
            parts = ui.wrap(line, 12)
        self.assertTrue(all(p.lstrip().startswith("\x1b[2m\x1b[3m") for p in parts[1:3]), parts)
        self.assertFalse(parts[-1].lstrip().startswith("\x1b["), parts)  # after the reset: plain again

    def test_wrap_keeps_indent(self):
        ui._COLOR = False
        lines = ui.wrap("  To remove all files from the cache use the clean switch twice", 30)
        self.assertTrue(all(line.startswith("  ") for line in lines), lines)


class Ranking(unittest.TestCase):
    def test_answer_shape_ranks_in_facts(self):
        items = [("w", "Bismarck spent the rest of his life writing; sixteen years younger than the king."),
                 ("w", "Otto von Bismarck was born on 1 April 1815 and died on 30 July 1898."),
                 ("w", "Bismarck was a German statesman.")]
        q = "годы жизни Бисмарка Bismarck years of life"
        self.assertNotEqual(rag.rank(q, items, k=1)[0], items[1])  # the words of the question miss it
        merged = rag.rank_many([q, "Bismarck was born in 1815 and died in 1898."], items)
        self.assertIn(items[1], merged[:2])
        self.assertEqual(sorted(merged), sorted(items))

    def test_video_pages_skipped(self):
        from shellmate import web
        res = [web.Result("Bismarck — YouTube", "https://www.youtube.com/watch?v=x", "", "[11:46] he was sixteen years younger"),
               web.Result("Bismarck — Wikipedia", "https://en.wikipedia.org/wiki/Bismarck", "", "Bismarck was born in 1815.")]
        self.assertEqual({src for src, _ in web.gather("Bismarck born", res)}, {res[1].url})


class Translate(unittest.TestCase):
    def test_copy_is_redone_with_the_target_named(self):
        from shellmate import cli, config
        app = cli.App(config.Config(name="t"), dry_run=True)
        app._big = ""
        replies = iter(['{"target_language": "Russian", "translation": "спасибо"}', "Thank you."])
        with mock.patch.object(cli.llm, "chat", side_effect=lambda *a, **k: next(replies)) as chat, \
                mock.patch.object(cli.ui, "panel") as panel:
            app.translate(router.Intent("translate", text="спасибо"), "переведи «спасибо»")
        self.assertIn("Translate into English", chat.call_args_list[1].args[2][1]["content"])
        self.assertEqual(panel.call_args.args[1], ["Thank you."])


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

    def test_old_radeon_gets_vulkan(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for node, gfx in (("0", 0), ("1", 80003)):  # CPU node + Polaris (Radeon Pro 555X)
                (Path(d) / node).mkdir()
                (Path(d) / node / "properties").write_text(f"cpu_cores_count 0\ngfx_target_version {gfx}\n")
            self.assertFalse(hardware.amd_rocm_ok(Path(d)))
            (Path(d) / "1" / "properties").write_text("gfx_target_version 110000\n")  # RX 7900
            self.assertTrue(hardware.amd_rocm_ok(Path(d)))


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

    def test_save_load_roundtrip(self):
        import os
        import tempfile

        from shellmate import config
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            cfg = config.load("t")
            cfg.escalate_intent, cfg.docs, cfg.lang = 0.35, ["/x"], "en"
            config.save(cfg)
            self.assertEqual(config.load("t"), cfg)
            # 0.2.0 wrote floats as strings: they must still load as numbers
            path = config.config_path("t")
            path.write_text(path.read_text().replace("escalate_intent = 0.35", 'escalate_intent = "0.35"'))
            self.assertEqual(config.load("t").escalate_intent, 0.35)


class Language(unittest.TestCase):
    def test_both_languages(self):
        from shellmate import errors, i18n, persona
        try:
            for lang in i18n.LANGS:
                i18n.set_lang(lang)
                self.assertTrue(router.doubts(router.Intent("unknown"), "x", 0.5, 0.5))
                self.assertTrue(errors.match("error: target not found: foo")[0][0])
                for style in persona.STYLES.values():
                    self.assertEqual(set(style.phrases), set(style.phrases_en))
            i18n.set_lang("en")
            self.assertIn("foo", errors.match("error: target not found: foo")[0][0])
            self.assertTrue(all(k.what_en and k.fix_en for k in errors.KNOWN))
        finally:
            i18n.set_lang("ru")

    def test_system_lang(self):
        from shellmate import i18n
        with mock.patch.dict("os.environ", {"LC_ALL": "", "LC_MESSAGES": "", "LANG": "ru_RU.UTF-8"}):
            self.assertEqual(i18n.system_lang(), "ru")
        with mock.patch.dict("os.environ", {"LC_ALL": "de_DE.UTF-8"}):
            self.assertEqual(i18n.system_lang(), "en")
