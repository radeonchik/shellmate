"""Local documents as a knowledge source: an incremental SQLite FTS5 index of the folders the
user added. Re-indexing only touches files whose mtime/size changed."""

import os
import re
import sqlite3
from pathlib import Path

from shellmate import rag

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", ".godot",
             "target", "build", "dist", ".idea"}
MAX_FILE = 50 * 1024**2


def db_path(name: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / name / "docs.sqlite"


def connect(name: str) -> sqlite3.Connection:
    path = db_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER,"
               " chunks INTEGER)")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path UNINDEXED, text,"
               " tokenize='unicode61 remove_diacritics 2')")
    return db


def _walk(folder: Path):
    if folder.is_file():  # a single file added as a source
        if folder.suffix.lower() in rag.DOC_EXT:
            yield folder
        return
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in rag.DOC_EXT:
                yield p


def reindex(name: str, folders: list[str], progress=None) -> tuple[int, int, list[str]]:
    """Returns (files indexed now, files total, files with no extractable text, e.g. scanned PDFs)."""
    db = connect(name)
    known = {p: (m, s) for p, m, s in db.execute("SELECT path, mtime, size FROM files")}
    seen, todo = set(), []
    for folder in folders:
        for p in _walk(Path(folder).expanduser()):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > MAX_FILE:
                continue
            key = str(p)
            seen.add(key)
            if known.get(key) != (st.st_mtime, st.st_size):
                todo.append((p, st))
    empty = []
    for i, (p, st) in enumerate(todo, 1):
        if progress:
            progress(i, len(todo), p)
        parts = rag.chunks(rag.extract_text(p))
        db.execute("DELETE FROM chunks WHERE path = ?", (str(p),))
        db.executemany("INSERT INTO chunks (path, text) VALUES (?, ?)", [(str(p), c) for c in parts])
        db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                   (str(p), st.st_mtime, st.st_size, len(parts)))
        if not parts and p.suffix.lower() == ".pdf":
            empty.append(str(p))
        if i % 50 == 0:
            db.commit()
    for gone in set(known) - seen:  # deleted files or removed folders
        db.execute("DELETE FROM chunks WHERE path = ?", (gone,))
        db.execute("DELETE FROM files WHERE path = ?", (gone,))
    db.commit()
    db.close()
    return len(todo), len(seen), empty


def search(name: str, query: str, k: int = 6) -> list[tuple[str, str]]:
    words = list(dict.fromkeys(rag.terms(query)))
    if not words:
        return []
    # prefix search on 6-char stems: OR so partial matches still rank, bm25 orders them
    match = " OR ".join(f'"{w}"*' for w in words)
    db = connect(name)
    try:
        rows = db.execute("SELECT path, text FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks)"
                          " LIMIT ?", (match, k)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    # "my timetable" names the file, not its text: files whose name has a word of the question come first
    named = [p for (p,) in db.execute("SELECT path FROM files")
             if set(rag.terms(re.sub(r"[_.-]+", " ", Path(p).stem))) & set(words)]
    for p in named[:3]:
        rows = db.execute("SELECT path, text FROM chunks WHERE path = ? LIMIT 3", (p,)).fetchall() + rows
    db.close()
    return list(dict.fromkeys(rows))[:k]


def stats(name: str) -> tuple[int, int]:
    db = connect(name)
    files, chunks = db.execute("SELECT count(*), coalesce(sum(chunks), 0) FROM files").fetchone()
    db.close()
    return files, chunks


def short(path: str) -> str:
    return re.sub(rf"^{re.escape(str(Path.home()))}", "~", path)
