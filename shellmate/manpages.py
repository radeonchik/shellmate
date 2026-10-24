"""Man pages as a source: the description of one option or subcommand, verbatim.

The page is rendered by the system's own `man` (plain text, fixed width) and read like any other
document; nothing of the program being explained is ever run."""

import functools
import os
import re
import shutil
import subprocess

OVERSTRIKE = re.compile(r".\x08")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@functools.lru_cache(maxsize=64)
def page(name: str) -> str:
    """The rendered man page, '' when there is none (or no man at all)."""
    if not shutil.which("man") or not re.fullmatch(r"[\w.+-]+", name):
        return ""
    env = {**os.environ, "MANWIDTH": "160", "MANPAGER": "cat", "PAGER": "cat", "MAN_KEEP_FORMATTING": "",
           "GROFF_NO_SGR": "1", "LC_ALL": "C.UTF-8"}
    out = ""
    for cmd in (["man", "--nh", "--nj", name], ["man", name]):  # man-db: no hyphenation; mandoc: plain
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=8, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if r.returncode == 0 and r.stdout.strip():
            out = r.stdout
            break
    out = ANSI.sub("", OVERSTRIKE.sub("", out))
    return re.sub(r"(\w)[‐-]\n\s+(\w)", r"\1\2", out)  # words hyphenated across lines


def summary(text: str) -> str:
    """The NAME line: "ls - list directory contents" -> "list directory contents"."""
    m = re.search(r"^NAME\n\s+(.+?)(?:\n\n|\n[A-Z])", text, re.M | re.S)
    if not m:
        return ""
    line = re.sub(r"\s+", " ", m.group(1))
    return line.split(" - ", 1)[-1].split(" — ", 1)[-1].strip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _entry(lines: list[str], i: int, first_rest: str) -> str:
    """The paragraph(s) of an option/command entry that starts at line i."""
    base = _indent(lines[i])
    body = [first_rest] if first_rest else []
    for line in lines[i + 1:]:
        if not line.strip():
            if body:
                body.append("")
            continue
        if _indent(line) <= base:
            break
        body.append(line.strip())
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", "\n".join(body).strip())
    return re.sub(r"[ \t]+", " ", text.split("\n\n")[0]).strip()


OPTION_LINE = re.compile(r"^(\s{1,16})(-.*?)(?:\s{2,}(\S.*))?$")


def option(text: str, flag: str, section: str = "") -> str:
    """The description of `flag` ("-r", "--recursive"), optionally searched in a section first
    (pacman's -c means one thing under SYNC OPTIONS and another under REMOVE OPTIONS)."""
    lines = text.splitlines()
    starts = [0]
    if section:
        starts = [i for i, l in enumerate(lines) if l.strip().upper() == section.upper()] + [0]
    for start in starts:
        for i in range(start, len(lines)):
            m = OPTION_LINE.match(lines[i])
            if not m:
                continue
            names = re.findall(r"(?<![\w-])(--?[A-Za-z0-9][\w-]*)", m.group(2))
            if flag in names:
                desc = _entry(lines, i, m.group(3) or "")
                if desc:
                    return _short(desc)
    return ""


def command(text: str, word: str) -> str:
    """The entry of a subcommand inside the program's page: systemctl's "enable UNIT…"."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^\s{{3,12}}{re.escape(word)}(?:\s|$)", line) and not line.strip().startswith("-"):
            rest = re.split(r"\s{2,}", line.strip(), maxsplit=1)
            desc = _entry(lines, i, rest[1] if len(rest) > 1 else "")
            if desc:
                return _short(desc)
    return ""


def _short(desc: str, limit: int = 220) -> str:
    """The first sentences, up to ~limit characters: a quote, not the whole manual."""
    sents = re.split(r"(?<=[.!?])\s+(?=[A-Z])", desc)
    out = ""
    for s in sents:
        if out and len(out) + len(s) > limit:
            break
        out = f"{out} {s}".strip()
    return out[:limit + 80]


# pacman-like programs: an operation picks the section its options are described in
OPERATION_SECTIONS = {
    "pacman": {"-S": "SYNC OPTIONS (APPLY TO -S)", "-R": "REMOVE OPTIONS (APPLY TO -R)", "-Q": "QUERY OPTIONS (APPLY TO -Q)",
               "-U": "UPGRADE OPTIONS (APPLY TO -S AND -U)", "-F": "FILE OPTIONS (APPLY TO -F)", "-D": "DATABASE OPTIONS (APPLY TO -D)"},
}


def section_for(prog: str, flags: list[str]) -> str:
    ops = OPERATION_SECTIONS.get(prog, {})
    return next((ops[f] for f in flags if f in ops), "")
