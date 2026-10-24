"""The offline library: documentation downloaded once (during setup or with --library update) and
searched locally — no network, no search credits, milliseconds instead of seconds.

What is downloaded depends on the system: ArchWiki (the best general Linux reference, on every
distribution — evidence.off_topic drops its pacman-only lines elsewhere), tldr examples, the manual of
the user's shell, and the distribution's own docs. One index for all assistant names, in
$XDG_DATA_HOME/shellmate/library.sqlite.

trusted.gather asks library.search first for every favored site the library has; man pages are read
straight from the system (manpages.py)."""

import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from shellmate import rag
from shellmate import library_sources as src
from shellmate.i18n import t

MAX_AGE = 30 * 86400  # sources without a version to compare are refreshed after this


def db_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "shellmate" / "library.sqlite"


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    domain: str  # the trusted.Site it answers for
    size_mb: float  # download size, for the setup's question (0: already on this computer)
    fetch: Callable[[src.Progress], Iterator]
    version: Callable[[], str] = lambda: ""  # cheap remote check; "" = refresh by age


def _shell_source(shell: str) -> Source | None:
    if shell == "bash":
        return Source("bash", t("руководство bash", "the bash manual"), "www.gnu.org/software/bash", 1.0, src.bash_manual)
    if shell == "fish":
        local = (src.FISH_LOCAL / "index.html").exists()
        return Source("fish", t("руководство fish", "the fish manual"), "fishshell.com", 0 if local else 3, src.fish_manual)
    if shell == "zsh":
        return Source("zsh", t("руководство zsh", "the zsh manual"), "zsh.sourceforge.io", 2, src.zsh_manual)
    return None


def wanted(lang: str = "") -> list[Source]:
    """The sources for this system: Linux basics, commands, the user's shell, the distribution's docs."""
    from shellmate import backends
    d = backends.distro()
    langs = ("en", "ru") if (lang or t("ru", "en")) == "ru" else ("en",)
    out = [
        Source("archwiki", "ArchWiki", "wiki.archlinux.org", 0 if src.ARCHWIKI_LOCAL.is_dir() else 19,
               src.archwiki, src.archwiki_version),
        Source("tldr", t("tldr: примеры команд", "tldr: command examples"), src.TLDR_DOMAIN, 3.4 + 0.6 * (len(langs) - 1),
               lambda p: src.tldr(p, langs), lambda: src.tldr_version() + "+".join(langs)),
    ]
    shell = _shell_source(src.user_shell())
    if shell:
        out.append(shell)
    if "cachyos" in d.ids:
        out.append(Source("cachyos", "CachyOS Wiki", "wiki.cachyos.org", 47, src.cachyos_wiki))
    if d.family == "debian":
        out.append(Source("debian-reference", "Debian Reference", "www.debian.org", 0.9, src.debian_reference))
    if "ubuntu" in d.ids:
        out.append(Source("ubuntu-server", "Ubuntu Server docs", "documentation.ubuntu.com", 3.5, src.ubuntu_server_docs))
    if d.family == "fedora":
        out.append(Source("fedora", "Fedora Quick Docs", "docs.fedoraproject.org", 6, src.fedora_quick_docs))
    return out


# --- storage ---------------------------------------------------------------------------------------

def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, name TEXT, domain TEXT, version TEXT,
                                            fetched REAL, pages INTEGER, chunks INTEGER);
        CREATE TABLE IF NOT EXISTS pages (id INTEGER PRIMARY KEY, source TEXT, domain TEXT, url TEXT, title TEXT);
        CREATE INDEX IF NOT EXISTS pages_source ON pages(source);
        CREATE TABLE IF NOT EXISTS chunk (id INTEGER PRIMARY KEY, page INTEGER, title TEXT, text TEXT);
        CREATE INDEX IF NOT EXISTS chunk_page ON chunk(page);
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, text, content='chunk', content_rowid='id',
                                                          tokenize='unicode61 remove_diacritics 2');
        CREATE TABLE IF NOT EXISTS tldr (name TEXT, lang TEXT, platform TEXT, description TEXT, examples TEXT,
                                         PRIMARY KEY (name, lang));
    """)
    return db


_HAVE: dict[str, bool] = {}


def has(domain: str) -> bool:
    """Whether the library holds pages of this site (cached for the process)."""
    if not _HAVE:
        if not db_path().exists():
            return False
        db = connect()
        _HAVE.update({d: n > 0 for d, n in db.execute("SELECT domain, pages FROM sources")})
        db.close()
    return _HAVE.get(domain, False)


def installed() -> dict[str, dict]:
    if not db_path().exists():
        return {}
    db = connect()
    rows = db.execute("SELECT id, name, domain, version, fetched, pages, chunks FROM sources").fetchall()
    db.close()
    return {r[0]: dict(zip(("id", "name", "domain", "version", "fetched", "pages", "chunks"), r)) for r in rows}


def _drop(db: sqlite3.Connection, source: str) -> None:
    ids = "SELECT c.id FROM chunk c JOIN pages p ON c.page = p.id WHERE p.source = ?"
    # external-content FTS: rows are removed by repeating their values with the 'delete' command
    db.execute(f"INSERT INTO fts(fts, rowid, title, text) SELECT 'delete', id, title, text FROM chunk WHERE id IN ({ids})",
               (source,))
    db.execute(f"DELETE FROM chunk WHERE id IN ({ids})", (source,))
    db.execute("DELETE FROM pages WHERE source = ?", (source,))
    if source == "tldr":
        db.execute("DELETE FROM tldr")


def store(db: sqlite3.Connection, s: Source, items, version: str) -> tuple[int, int]:
    """Replaces the source's pages with these Docs / TldrPages; returns (pages, chunks)."""
    _drop(db, s.id)
    pages = chunks = 0
    for item in items:
        if isinstance(item, src.TldrPage):
            db.execute("INSERT OR REPLACE INTO tldr VALUES (?, ?, ?, ?, ?)",
                       (item.name, item.lang, item.platform, item.description, json.dumps(item.examples, ensure_ascii=False)))
            continue
        parts = rag.chunks(item.text)
        if not parts:
            continue
        page = db.execute("INSERT INTO pages (source, domain, url, title) VALUES (?, ?, ?, ?)",
                          (s.id, s.domain, item.url, item.title)).lastrowid
        for c in parts:
            cid = db.execute("INSERT INTO chunk (page, title, text) VALUES (?, ?, ?)", (page, item.title, c)).lastrowid
            db.execute("INSERT INTO fts(rowid, title, text) VALUES (?, ?, ?)", (cid, item.title, c))
        pages, chunks = pages + 1, chunks + len(parts)
    db.execute("INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)",
               (s.id, s.name, s.domain, version, time.time(), pages, chunks))
    return pages, chunks


@dataclass
class Result:
    source: Source
    ok: bool
    message: str  # pages and chunks, "up to date", or what went wrong


def update(sources: list[Source], force: bool = False, progress: Callable[[str], None] | None = None) -> list[Result]:
    have = installed()
    out = []
    db = connect()
    try:
        for s in sources:
            note = progress or (lambda _: None)
            note(s.name)
            try:
                version = s.version()
            except src.SourceError:
                version = ""
            old = have.get(s.id)
            fresh = old and (version and old["version"] == version or
                             not version and time.time() - old["fetched"] < MAX_AGE)
            if fresh and not force:
                out.append(Result(s, True, t("уже свежая", "already up to date")))
                continue
            try:
                pages, chunks = store(db, s, s.fetch(note), version)
                db.commit()
            except (src.SourceError, OSError, ValueError) as e:
                db.rollback()
                hint = t("нужен zstd: поставь пакет zstd", "zstd is needed: install the zstd package") \
                    if str(e) == "zstd" else str(e)[:200]
                out.append(Result(s, False, hint))
                continue
            out.append(Result(s, True, t(f"{pages} страниц", f"{pages} pages")))
    finally:
        db.close()
        _HAVE.clear()
    return out


# --- lookups ---------------------------------------------------------------------------------------

# tldr pages named like everyday words: an exact match on them says nothing ("swap file" is not `file`)
COMMON = set("""file service time date test install update make find link info help type open more less top watch
which who yes read last login sort split join cut head tail print history alias man mount kill sleep wait true
false users groups shell env path set default change create logs log how what""".split())


def search(domain: str, query: str, pages: int = 2) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """The pages of this site that best match the query, as (url, chunk) pairs of their whole text —
    the same shape trusted.gather gets from downloading pages, so the ranking after it is unchanged."""
    words = list(dict.fromkeys(rag.terms(query)))
    if not words or not has(domain):
        return [], {}
    match = " OR ".join(f'"{w}"*' for w in words)
    # a page named exactly like a word of the question comes first: "tar" for "extract a tar.gz archive"
    raw = [w for w in re.findall(r"[a-z0-9][a-z0-9+._-]*", query.lower()) if len(w) > 1]
    names = raw + [f"{a} {b}" for a, b in zip(raw, raw[1:])] + [f"{a}-{b}" for a, b in zip(raw, raw[1:])]
    names = [n.split(".")[0] if n.endswith((".gz", ".xz", ".zst", ".bz2")) else n for n in names]
    db = connect()
    try:
        exact = db.execute(f"""SELECT id, url, title FROM pages WHERE domain = ? AND
                               (lower(title) IN ({",".join("?" * len(names))}) OR
                                lower(title) IN ({",".join("?" * len(names))}))""",
                           (domain, *names, *[f"tldr: {n}" for n in names])).fetchall() if names else []
        # the best chunks decide the pages; the title counts more ("pacman" page beats a mention of pacman)
        rows = db.execute("""SELECT p.id, p.url, p.title FROM
                               (SELECT rowid, bm25(fts, 6.0, 1.0) AS s FROM fts WHERE fts MATCH ? ORDER BY s LIMIT 2000) h
                             JOIN chunk c ON c.id = h.rowid JOIN pages p ON p.id = c.page
                             WHERE p.domain = ? ORDER BY h.s""", (match, domain)).fetchall()
        # the most specific name: "journalctl" before "service", "git-commit" before "git"
        exact = sorted((r for r in exact if r[2].lower().removeprefix("tldr: ") not in COMMON),
                       key=lambda r: -len(r[2]))
        best = list(dict.fromkeys(exact[:1] + rows))[:pages]
        items, titles = [], {}
        for pid, url, title in best:
            titles[url] = title
            items += [(url, text) for (text,) in db.execute("SELECT text FROM chunk WHERE page = ? ORDER BY id", (pid,))]
    except sqlite3.OperationalError:
        return [], {}
    finally:
        db.close()
    return items, titles


def tldr(name: str, lang: str = "en") -> src.TldrPage | None:
    """The tldr page of a command ("tar", "git-commit"), in the language if translated."""
    if not db_path().exists():
        return None
    db = connect()
    try:
        rows = db.execute("SELECT name, lang, platform, description, examples FROM tldr WHERE name = ? "
                          "ORDER BY lang = ? DESC, lang = 'en' DESC, platform = 'linux' DESC", (name, lang)).fetchall()
    finally:
        db.close()
    if not rows:
        return None
    n, lg, platform, desc, ex = rows[0]
    return src.TldrPage(n, lg, platform, desc, [tuple(e) for e in json.loads(ex)])


def size_mb() -> float:
    p = db_path()
    return p.stat().st_size / 1e6 if p.exists() else 0.0
