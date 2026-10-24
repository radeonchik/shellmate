"""The offline library: source formats -> text, storing and searching, trusted-site matching."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shellmate import library, trusted  # noqa: E402
from shellmate import library_sources as src  # noqa: E402

TLDR_TAR = """# tar

> Archiving utility.
> Often combined with a compression method, such as `gzip` or `bzip2`.
> More information: <https://www.gnu.org/software/tar/manual/tar.html>.

- [c]reate an archive and write it to a [f]ile:

`tar cf {{path/to/target.tar}} {{path/to/file1 path/to/file2 ...}}`

- E[x]tract a (compressed) archive [f]ile into the target directory:

`tar xf {{path/to/source.tar.ext}} {{[-C|--directory]}} {{path/to/directory}}`
"""

ARCHWIKI_PAGE = """<html><head><title>pacman - ArchWiki</title></head><body>
<main id="content"><div>Jump to content</div><div>From ArchWiki</div>
<div class="archwiki-template-box archwiki-template-related-articles">Related articles<ul><li>Pacman/Rosetta</li></ul></div>
<p>The pacman package manager is one of the major distinguishing features of Arch Linux. It combines a simple
binary package format with an easy-to-use Arch build system.</p>
<h2 id="Cleaning">Cleaning the package cache</h2>
<p>pacman stores its downloaded packages in /var/cache/pacman/pkg/ and does not remove the old or uninstalled
versions automatically. To clean it, run:</p><pre>$ paccache -r</pre>
<div id="catlinks">Categories: Package manager</div></main></body></html>"""


class Formats(unittest.TestCase):
    def test_tldr_page(self):
        p = src.tldr_page(TLDR_TAR, "en", "common")
        self.assertEqual(p.name, "tar")
        self.assertEqual(p.description, "Archiving utility. Often combined with a compression method, such as gzip or bzip2.")
        self.assertEqual(p.examples[0], ("create an archive and write it to a file:",
                                         "tar cf path/to/target.tar path/to/file1 path/to/file2 ..."))
        self.assertEqual(p.examples[1][1], "tar xf path/to/source.tar.ext -C path/to/directory")
        doc = src.tldr_doc(p)
        self.assertIn("create an archive and write it to a file:\ntar cf", doc.text)
        self.assertTrue(doc.url.startswith("https://github.com/tldr-pages/tldr/blob/main/pages/common/tar"))

    def test_archwiki_page(self):
        doc = src.archwiki_page("Pacman.html", ARCHWIKI_PAGE)
        self.assertEqual((doc.url, doc.title), ("https://wiki.archlinux.org/title/Pacman", "pacman"))
        self.assertTrue(doc.text.startswith("The pacman package manager"))
        self.assertIn("§ Cleaning the package cache", doc.text)
        self.assertIn("$ paccache -r", doc.text)
        self.assertNotIn("Categories", doc.text)
        self.assertNotIn("Rosetta", doc.text)
        self.assertIsNone(src.archwiki_page("01973e61502a6abccee11d2419aae5e4.html", ARCHWIKI_PAGE))
        self.assertIsNone(src.archwiki_page("Main_page.html", ARCHWIKI_PAGE.replace("pacman - ", "Main page - ")))

    def test_markdown(self):
        md = ("---\ntitle: Post Install\n---\nimport { Tabs } from 'x';\n\n## Updating\n\n:::tip\nSee the "
              "[FAQ](/faq) first.\n:::\n\n<Tabs>\n\n```bash\nsudo pacman -Syu\n```\n\n### Mirrors\n\n- Run **rate-mirrors**.\n")
        text = src.markdown_text(md)
        self.assertIn("§ Updating", text)
        self.assertIn("§ Updating › Mirrors", text)
        self.assertIn("See the FAQ first.", text)
        self.assertIn("sudo pacman -Syu", text)
        self.assertIn("Run rate-mirrors.", text)
        self.assertNotIn("import", text)
        self.assertNotIn("<Tabs>", text)

    def test_asciidoc(self):
        adoc = ("= Installing Steam\n:page-tags: games\n\n== Enabling RPM Fusion\n\nSee xref:rpmfusion.adoc[RPM Fusion] "
                "for `dnf`.\n\n[source,bash]\n----\nsudo dnf install steam\n----\n")
        title, text = src.asciidoc_text(adoc)
        self.assertEqual(title, "Installing Steam")
        self.assertIn("§ Enabling RPM Fusion", text)
        self.assertIn("See RPM Fusion for dnf.", text)
        self.assertIn("sudo dnf install steam", text)
        self.assertNotIn("page-tags", text)

    def test_user_shell(self):
        with mock.patch.dict(os.environ, {"SHELL": "/usr/bin/fish"}):
            self.assertEqual(src.user_shell(), "fish")
        with mock.patch.dict(os.environ, {"SHELL": "/bin/sh"}):
            self.assertEqual(src.user_shell(), "")


class Storage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.dir.name})
        self.env.start()
        library._HAVE.clear()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()
        library._HAVE.clear()

    def test_store_search_replace(self):
        page = src.tldr_page(TLDR_TAR, "en", "common")
        aw = library.Source("archwiki", "ArchWiki", "wiki.archlinux.org", 19,
                            lambda p: iter([src.archwiki_page("Pacman.html", ARCHWIKI_PAGE),
                                            src.Doc("https://wiki.archlinux.org/title/Steam", "Steam",
                                                    "Steam is a digital distribution platform for games." * 3)]))
        tl = library.Source("tldr", "tldr", src.TLDR_DOMAIN, 3, lambda p: iter([page, src.tldr_doc(page)]))
        res = library.update([aw, tl])
        self.assertTrue(all(r.ok for r in res), [r.message for r in res])
        self.assertTrue(library.has("wiki.archlinux.org") and library.has(src.TLDR_DOMAIN))
        items, titles = library.search("wiki.archlinux.org", "clean pacman package cache")
        self.assertEqual(list(titles.values())[0], "pacman")
        self.assertTrue(any("paccache -r" in c for _, c in items))
        self.assertEqual(library.search(src.TLDR_DOMAIN, "clean pacman package cache")[0], [])  # other site's words
        self.assertEqual(library.tldr("tar").examples[0][1][:6], "tar cf")
        # up to date: nothing is fetched again; forced: replaced, not duplicated
        self.assertIn(library.update([aw])[0].message, ("already up to date", "уже свежая"))
        library.update([aw], force=True)
        self.assertEqual(library.installed()["archwiki"]["pages"], 2)
        self.assertEqual(library.search("wiki.archlinux.org", "steam games")[0],
                         [("https://wiki.archlinux.org/title/Steam", "Steam is a digital distribution platform for games." * 3)])

    def test_failed_source_keeps_the_rest(self):
        def broken(p):
            raise src.SourceError("zstd")
        res = library.update([library.Source("archwiki", "ArchWiki", "wiki.archlinux.org", 19, broken)])
        self.assertFalse(res[0].ok)
        self.assertFalse(library.has("wiki.archlinux.org"))


class TrustedPaths(unittest.TestCase):
    def test_domain_with_path(self):
        d = [src.TLDR_DOMAIN, "wiki.archlinux.org", "www.gnu.org/software/bash"]
        self.assertTrue(trusted.is_trusted("https://github.com/tldr-pages/tldr/blob/main/pages/common/tar.md", d))
        self.assertFalse(trusted.is_trusted("https://github.com/someone/else", d))
        self.assertTrue(trusted.is_trusted("https://www.gnu.org/software/bash/manual/bash.html#Intro", d))
        self.assertFalse(trusted.is_trusted("https://www.gnu.org/software/tar/", d))
        self.assertTrue(trusted.is_trusted("https://wiki.archlinux.org/title/Pacman", d))


if __name__ == "__main__":
    unittest.main()
