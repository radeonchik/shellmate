"""Where the offline library's documents come from and how each format becomes text.

Every fetcher yields Doc (a page: its online URL, title and text with "§ Section" lines, the same
shape rag.html_to_text gives web pages) or TldrPage (kept structured for explaining commands).
Nothing here needs sudo or a package manager: archives are downloaded and read in memory."""

import io
import json
import os
import pwd
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from shellmate import rag, web

Progress = Callable[[str], None]


@dataclass
class Doc:
    url: str
    title: str
    text: str


@dataclass
class TldrPage:
    name: str  # "tar", "git-commit"
    lang: str
    platform: str  # common | linux
    description: str
    examples: list[tuple[str, str]] = field(default_factory=list)  # (what it does, the command)


class SourceError(Exception):
    pass


def agent() -> dict[str, str]:
    """An honest User-Agent: some doc sites refuse browser-looking clients that don't act like one."""
    from shellmate import updates
    return {"User-Agent": f"shellmate/{updates.version()} (https://github.com/radeonchik/shellmate; terminal assistant)"}


def fetch(url: str, timeout: float = 20) -> str:
    try:
        return web.get(url, timeout, headers=agent())[1]
    except OSError as e:
        raise SourceError(f"{url}: {e}") from e


def download(url: str, progress: Progress | None = None, label: str = "", timeout: float = 60) -> bytes:
    """The whole file in memory, reporting megabytes as they arrive."""
    req = urllib.request.Request(url, headers=agent())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = int(r.headers.get("Content-Length") or 0)
            buf = io.BytesIO()
            while chunk := r.read(1 << 18):
                buf.write(chunk)
                if progress:
                    done = buf.tell() / 1e6
                    progress(f"{label} {done:.0f}" + (f"/{total / 1e6:.0f}" if total else "") + " MB")
            return buf.getvalue()
    except OSError as e:
        raise SourceError(f"{url}: {e}") from e


def get_json(url: str, timeout: float = 20) -> dict:
    try:
        return json.loads(web.get(url, timeout, headers={**agent(), "Accept": "application/json"})[1])
    except (OSError, ValueError) as e:
        raise SourceError(f"{url}: {e}") from e


# --- text from markup ------------------------------------------------------------------------------

def markdown_text(md: str) -> str:
    """Markdown / MDX -> text with "§ Section" lines; code blocks stay as lines (they're the commands)."""
    md = re.sub(r"\A---\n.*?\n---\n", "", md, flags=re.S)  # front matter
    out, path, in_code = [], {}, False
    for line in md.splitlines():
        if line.strip().startswith(("```", "~~~")):
            in_code = not in_code
            out.append("")
            continue
        if in_code:
            out.append(line.strip())
            continue
        if re.match(r"\s*(?:import|export)\s", line) or re.match(r"\s*</?[A-Z]\w*[^>]*/?>\s*$", line):
            continue  # MDX components and imports
        m = re.match(r"(#{1,4})\s+(.*?)\s*#*$", line)
        if m:
            level, title = len(m.group(1)), _inline(m.group(2))
            path = {k: v for k, v in path.items() if k < level}
            path[level] = title
            shown = [v for k, v in sorted(path.items()) if k > 1] or [title]
            out += ["", f"§ {' › '.join(shown)}", ""]
            continue
        line = re.sub(r"^\s*:::\s*\w*.*$", "", line)  # admonition markers (:::tip)
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"^\s*\d+\.\s+", "", line)
        out.append(_inline(line).strip())
    return rag.normalize("\n".join(out))


def _inline(s: str) -> str:
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)  # images
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)  # links keep their text
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    return s.replace("`", "")


def asciidoc_text(adoc: str) -> tuple[str, str]:
    """AsciiDoc (Fedora Quick Docs) -> (title, text)."""
    title, out, path, in_block = "", [], {}, False
    for line in adoc.splitlines():
        if re.match(r"^(----|\.\.\.\.|====|\*\*\*\*|\+\+\+\+)\s*$", line):
            in_block = not in_block
            out.append("")
            continue
        if in_block:
            out.append(line.strip())
            continue
        if re.match(r"^(:[\w-]+:|include::|ifdef::|endif::|ifndef::|\[[^\]]*\]$|//)", line):
            continue
        m = re.match(r"^(=+)\s+(.*)$", line)
        if m:
            level, text = len(m.group(1)), _adoc_inline(m.group(2))
            if level == 1:
                title = text
                continue
            path = {k: v for k, v in path.items() if k < level}
            path[level] = text
            out += ["", f"§ {' › '.join(v for _, v in sorted(path.items()))}", ""]
            continue
        line = re.sub(r"^\s*[*.]+\s+", "", line)
        out.append(_adoc_inline(line).strip())
    return title, rag.normalize("\n".join(out))


def _adoc_inline(s: str) -> str:
    s = re.sub(r"(?:xref|link|https?)(?::[^\[\s]*)?\[([^\]]*)\]", r"\1", s)
    s = re.sub(r"(?:image|kbd|btn|menu):[^\[]*\[([^\]]*)\]", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    return re.sub(r"(\*|_)(\S.*?\S|\S)\1", r"\2", s)


def tldr_page(md: str, lang: str, platform: str) -> TldrPage | None:
    """tldr markdown: "# name", "> description" lines, then "- what:" + "`command`" pairs."""
    m = re.search(r"^#\s+(.+)$", md, re.M)
    if not m:
        return None
    desc = " ".join(re.sub(r"^>\s?", "", l) for l in md.splitlines()
                    if l.startswith(">") and not re.search(r"<https?://", l))
    examples, what = [], ""
    for line in md.splitlines():
        if line.startswith("- "):
            what = line[2:].strip()
        elif line.startswith("`") and what:
            examples.append((tldr_clean(what), tldr_clean(line.strip("`"))))
            what = ""
    return TldrPage(m.group(1).strip().replace(" ", "-"), lang, platform, tldr_clean(desc), examples)


def tldr_clean(s: str) -> str:
    """"[c]reate" -> "create", "{{[-C|--directory]}}" -> "-C", "{{path/to/file}}" -> "path/to/file"."""
    s = re.sub(r"\{\{\[([^|\]]+)\|[^\]]*\]\}\}", r"\1", s)
    s = re.sub(r"\{\{(.*?)\}\}", r"\1", s)
    s = re.sub(r"\[(\w{1,3})\](?=\w|\b)", r"\1", s)
    return s.replace("`", "")


def tldr_doc(p: TldrPage) -> Doc:
    """A tldr page as a searchable document: each example is "what it does:" + the command line,
    which rag.sentences keeps together as one quote."""
    body = "\n\n".join(f"{what.rstrip('.:')}:\n{cmd}" for what, cmd in p.examples)
    lang = "" if p.lang == "en" else f".{p.lang}"
    return Doc(f"https://{TLDR_DOMAIN}/tldr/blob/main/pages{lang}/{p.platform}/{p.name}.md", f"tldr: {p.name}",
               f"§ {p.name}\n\n{p.description}\n\n{body}")


# --- ArchWiki: the arch-wiki-docs package, from any distribution -----------------------------------

ARCH_MIRRORS = ["https://geo.mirror.pkgbuild.com", "https://mirrors.kernel.org/archlinux",
                "https://mirror.rackspace.com/archlinux"]
ARCHWIKI_LOCAL = Path("/usr/share/doc/arch-wiki/html/en")  # when the package is installed
ARCHWIKI_SKIP = re.compile(r"^(?:Main page|Table of contents|Category:|Help:|ArchWiki:|Special:)")


def archwiki_version() -> str:
    return get_json("https://archlinux.org/packages/extra/any/arch-wiki-docs/json/")["pkgver"]


def _mirrors() -> list[str]:
    """The user's own pacman mirrors first (fast and near), then well-known ones."""
    own = []
    try:
        for line in Path("/etc/pacman.d/mirrorlist").read_text().splitlines():
            m = re.match(r"\s*Server\s*=\s*(\S+)/\$repo/os/\$arch", line)
            if m:
                own.append(m.group(1))
    except OSError:
        pass
    return list(dict.fromkeys(own[:3] + ARCH_MIRRORS))


def _tar_members(data: bytes) -> Iterator[tuple[str, bytes]]:
    """(name, content) of every file in a .tar.zst: Python 3.14's zstd, else the zstd or bsdtar tool."""
    try:
        from compression import zstd  # Python 3.14+
        with tarfile.open(fileobj=zstd.ZstdFile(io.BytesIO(data)), mode="r|") as tar:
            yield from _read_tar(tar)
        return
    except ImportError:
        pass
    with tempfile.NamedTemporaryFile(suffix=".tar.zst") as f:
        f.write(data)
        f.flush()
        if shutil.which("zstd"):
            cmd = ["zstd", "-dc", f.name]
        elif shutil.which("bsdtar"):
            cmd = ["bsdtar", "-cf", "-", f"@{f.name}"]  # re-packs as a plain tar stream
        else:
            raise SourceError("zstd")
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                yield from _read_tar(tar)


def _read_tar(tar: tarfile.TarFile) -> Iterator[tuple[str, bytes]]:
    for m in tar:
        if m.isfile():
            f = tar.extractfile(m)
            if f:
                yield m.name, f.read()


def archwiki_page(rel: str, html: str) -> Doc | None:
    """One offline ArchWiki page; rel is its path under html/en ("Pacman/Tips_and_tricks.html")."""
    if re.match(r"[0-9a-f]{32}\.html$", rel):  # pages with non-ASCII titles: other languages' copies
        return None
    m = re.search(r"<title>(.*?) - ArchWiki</title>", html)
    title = m.group(1) if m else rel[:-5].replace("_", " ")
    if ARCHWIKI_SKIP.match(title):
        return None
    html = html.split('id="catlinks"')[0]
    # the "Related articles" box is a list of links, not content
    html = re.sub(r'<div[^>]*archwiki-template-related-articles.*?</div>', "", html, flags=re.S)
    text = rag.html_to_text(html)
    text = re.sub(r"\A(?:.*?Jump to content\s*)?(?:From ArchWiki\s*)?", "", text, count=1, flags=re.S)
    return Doc(f"https://wiki.archlinux.org/title/{rel[:-5]}", title, text) if len(text) > 200 else None


def archwiki(progress: Progress) -> Iterator[Doc]:
    if ARCHWIKI_LOCAL.is_dir():
        for p in sorted(ARCHWIKI_LOCAL.rglob("*.html")):
            doc = archwiki_page(str(p.relative_to(ARCHWIKI_LOCAL)), p.read_text(errors="replace"))
            if doc:
                yield doc
        return
    filename = get_json("https://archlinux.org/packages/extra/any/arch-wiki-docs/json/")["filename"]
    data, errors = b"", []
    for mirror in _mirrors():
        try:
            data = download(f"{mirror}/extra/os/x86_64/{filename}", progress, "ArchWiki")
            break
        except SourceError as e:
            errors.append(str(e))
    if not data:
        raise SourceError("; ".join(errors[-2:]))
    prefix = "usr/share/doc/arch-wiki/html/en/"
    for name, content in _tar_members(data):
        if name.startswith(prefix) and name.endswith(".html"):
            doc = archwiki_page(name[len(prefix):], content.decode(errors="replace"))
            if doc:
                yield doc


# --- tldr ------------------------------------------------------------------------------------------

TLDR_DOMAIN = "github.com/tldr-pages"  # a path, not just a host: trusted.is_trusted matches it as a prefix
TLDR_ZIP = "https://github.com/tldr-pages/tldr/releases/latest/download/tldr-pages{suffix}.zip"
TLDR_PLATFORMS = ("common", "linux")


def tldr_version() -> str:
    return get_json("https://api.github.com/repos/tldr-pages/tldr/releases/latest")["tag_name"]


def tldr(progress: Progress, langs: tuple[str, ...] = ("en",)) -> Iterator[TldrPage | Doc]:
    for lang in langs:
        data = download(TLDR_ZIP.format(suffix=f".{lang}"), progress, f"tldr ({lang})")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                platform, _, file = name.partition("/")
                if platform in TLDR_PLATFORMS and file.endswith(".md"):
                    page = tldr_page(z.read(name).decode(errors="replace"), lang, platform)
                    if page:
                        yield page
                        yield tldr_doc(page)


# --- shells ----------------------------------------------------------------------------------------

def user_shell() -> str:
    """bash | fish | zsh | '' — the login shell, which is what the user types commands into."""
    path = os.environ.get("SHELL") or ""
    if not path:
        try:
            path = pwd.getpwuid(os.getuid()).pw_shell
        except KeyError:
            path = ""
    name = Path(path).name
    return name if name in ("bash", "fish", "zsh") else ""


BASH_MANUAL = "https://www.gnu.org/software/bash/manual/bash.html"


def bash_manual(progress: Progress) -> Iterator[Doc]:
    html = download(BASH_MANUAL, progress, "Bash").decode(errors="replace")
    # one big page: split it at chapters so a quote links near its place
    parts = re.split(r'(?=<h2 class="chapter" id="([^"]+)">)', html)
    for i in range(1, len(parts) - 1, 2):
        anchor, body = parts[i], parts[i + 1]
        text = rag.html_to_text(body)
        title = text.split("\n", 1)[0].removeprefix("§ ").strip()
        yield Doc(f"{BASH_MANUAL}#{anchor}", f"Bash: {title}", text)


FISH_LOCAL = Path("/usr/share/doc/fish")
FISH_ONLINE = "https://fishshell.com/docs/current/"


def fish_manual(progress: Progress) -> Iterator[Doc]:
    """The HTML docs the fish package ships, or the same pages from fishshell.com."""
    pages: dict[str, str] = {}
    if (FISH_LOCAL / "index.html").exists():
        for p in FISH_LOCAL.rglob("*.html"):
            pages[str(p.relative_to(FISH_LOCAL))] = p.read_text(errors="replace")
    else:
        index = fetch(FISH_ONLINE + "commands.html")
        names = {"index.html", "language.html", "interactive.html", "faq.html", "fish_for_bash_users.html",
                 "tutorial.html", "prompt.html", "completions.html"}
        names |= set(re.findall(r'href="(cmds/[\w-]+\.html)"', index))
        for i, n in enumerate(sorted(names), 1):
            progress(f"fish {i}/{len(names)}")
            try:
                pages[n] = fetch(FISH_ONLINE + n)
            except SourceError:
                continue
    for rel, html in pages.items():
        if rel.startswith(("_", "genindex", "search")):
            continue
        m = re.search(r"<title>(.*?)(?: — fish-shell.*)?</title>", html)
        text = rag.html_to_text(html)
        if len(text) > 100:
            yield Doc(FISH_ONLINE + rel, f"fish: {m.group(1) if m else rel}", text)


ZSH_MANUAL = "https://zsh.sourceforge.io/Doc/Release/"


def zsh_manual(progress: Progress) -> Iterator[Doc]:
    toc = fetch(ZSH_MANUAL + "zsh_toc.html")
    names = list(dict.fromkeys(re.findall(r'href="([\w-]+\.html)', toc)))
    for i, n in enumerate(names, 1):
        progress(f"zsh {i}/{len(names)}")
        try:
            html = fetch(ZSH_MANUAL + n)
        except SourceError:
            continue
        m = re.search(r"<title>(.*?)</title>", html)
        yield Doc(ZSH_MANUAL + n, f"zsh: {(m.group(1) if m else n).split('(')[0].strip()}", rag.html_to_text(html))


# --- distributions ---------------------------------------------------------------------------------

def _tarball_files(url: str, progress: Progress, label: str, suffixes: tuple[str, ...]) -> Iterator[tuple[str, str]]:
    data = download(url, progress, label, timeout=120)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar:
            if m.isfile() and m.name.endswith(suffixes):
                f = tar.extractfile(m)
                if f:
                    yield m.name.split("/", 1)[1], f.read().decode(errors="replace")


LANG_DIR = re.compile(r"^[a-z]{2}(?:-[a-z]+)?/")


def cachyos_wiki(progress: Progress) -> Iterator[Doc]:
    for rel, md in _tarball_files("https://codeload.github.com/CachyOS/wiki/tar.gz/HEAD", progress,
                                  "CachyOS Wiki", (".md", ".mdx")):
        if not rel.startswith("src/content/docs/"):
            continue
        page = rel.removeprefix("src/content/docs/")
        if LANG_DIR.match(page):  # translations live in two-letter folders
            continue
        m = re.search(r"^title:\s*(.+)$", md, re.M)
        slug = re.sub(r"(?:/index)?\.mdx?$", "", page)
        yield Doc(f"https://wiki.cachyos.org/{slug}/".replace("//", "/").replace(":/", "://"),
                  f"CachyOS Wiki: {m.group(1).strip() if m else slug}", markdown_text(md))


def fedora_quick_docs(progress: Progress) -> Iterator[Doc]:
    for rel, adoc in _tarball_files("https://forge.fedoraproject.org/docs/quick-docs/archive/main.tar.gz",
                                    progress, "Fedora Quick Docs", (".adoc",)):
        if not rel.startswith("modules/ROOT/pages/") or "/_partials/" in rel:
            continue
        slug = rel.removeprefix("modules/ROOT/pages/").removesuffix(".adoc")
        title, text = asciidoc_text(adoc)
        yield Doc(f"https://docs.fedoraproject.org/en-US/quick-docs/{slug}/", f"Fedora Docs: {title or slug}", text)


def ubuntu_server_docs(progress: Progress) -> Iterator[Doc]:
    for rel, md in _tarball_files("https://codeload.github.com/canonical/ubuntu-server-documentation/tar.gz/HEAD",
                                  progress, "Ubuntu Server docs", (".md",)):
        if not rel.startswith("docs/") or rel.startswith("docs/contributing/"):
            continue
        slug = re.sub(r"(?:/index)?\.md$", "", rel.removeprefix("docs/"))
        m = re.search(r"^#\s+(.+)$", md, re.M)
        yield Doc(f"https://documentation.ubuntu.com/server/{slug}/", f"Ubuntu Server: {_inline(m.group(1)) if m else slug}",
                  markdown_text(md))


DEBIAN_REFERENCE = "https://www.debian.org/doc/manuals/debian-reference/"


def debian_reference(progress: Progress) -> Iterator[Doc]:
    """The plain-text edition, split at sections ("1.1. Console basics") into chapter pages."""
    text = download(DEBIAN_REFERENCE + "debian-reference.en.txt", progress, "Debian Reference").decode(errors="replace")
    start = text.find("\nChapter 1. ")
    start = text.find("\nChapter 1. ", start + 1) if text.count("\nChapter 1. ") > 1 else start
    body = text[start:]
    chapters = re.split(r"\n(?=Chapter \d+\. )", body)
    for ch in chapters:
        m = re.match(r"Chapter (\d+)\. (.+)", ch)
        if not m:
            continue
        num, title = int(m.group(1)), m.group(2).strip()
        # "1.2.3. Title" lines become sections
        ch = re.sub(r"\n(\d+(?:\.\d+)+)\. (.+)\n", lambda s: f"\n\n§ {s.group(2).strip()}\n\n", ch)
        ch = re.sub(r"\n[ \t]+", "\n", ch)
        yield Doc(f"{DEBIAN_REFERENCE}ch{num:02d}.en.html", f"Debian Reference: {title}", rag.normalize(ch))
