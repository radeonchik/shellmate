"""Version, self-update and a once-a-day "new version available" hint from GitHub releases."""

import json
import re
import shutil
import sys
import time
import urllib.request
from importlib import metadata
from pathlib import Path

from shellmate import state
from shellmate.i18n import t

DIST = "shellmate"
CHECK_EVERY = 86400


def version() -> str:
    try:
        return metadata.version(DIST)
    except metadata.PackageNotFoundError:
        return "0+local"


def repo() -> str | None:
    """"owner/name" from the Repository URL in pyproject.toml, None while it's a placeholder."""
    try:
        urls = metadata.metadata(DIST).get_all("Project-URL") or []
    except metadata.PackageNotFoundError:
        return None
    for u in urls:
        m = re.search(r"github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", u)
        if m and not m.group(1).startswith("OWNER/"):
            return m.group(1)
    return None


def _parse(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def source_checkout() -> Path | None:
    """The git working copy when installed in editable mode (the developer's own setup)."""
    root = Path(__file__).resolve().parent.parent
    return root if (root / ".git").exists() and (root / "pyproject.toml").exists() else None


def update_command() -> tuple[list[str] | None, str]:
    """(command, how it was installed)."""
    src = source_checkout()
    if src:
        return ["git", "-C", str(src), "pull", "--ff-only"], t(f"рабочая копия git ({src})", f"git working copy ({src})")
    prefix = sys.prefix.replace("\\", "/")
    if "/uv/tools/" in prefix and shutil.which("uv"):
        return ["uv", "tool", "upgrade", DIST], "uv tool"
    if "/pipx/venvs/" in prefix and shutil.which("pipx"):
        return ["pipx", "upgrade", DIST], "pipx"
    return None, t("непонятно как (не uv, не pipx, не git)", "unclear how (not uv, pipx or git)")


def latest_release(name: str, force: bool = False) -> str | None:
    """Newest release tag on GitHub, checked at most once a day (cached in the state dir)."""
    r = repo()
    if not r:
        return None
    cache = state.state_dir(name) / "update-check.json"
    try:
        data = json.loads(cache.read_text())
    except (OSError, ValueError):
        data = {}
    if force or time.time() - data.get("time", 0) > CHECK_EVERY:
        try:
            req = urllib.request.Request(f"https://api.github.com/repos/{r}/releases/latest",
                                         headers={"Accept": "application/vnd.github+json", "User-Agent": DIST})
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = {"time": time.time(), "tag": json.load(resp).get("tag_name", "")}
        except (OSError, ValueError):
            data = {"time": time.time(), "tag": data.get("tag", "")}  # offline: don't retry every run
        cache.write_text(json.dumps(data))
    return data.get("tag") or None


def newer_available(name: str) -> str | None:
    tag = latest_release(name)
    return tag if tag and _parse(tag) > _parse(version()) else None


def make_alias(new: str) -> tuple[bool, str]:
    """~/.local/bin/<new> -> this executable: the same assistant under another name."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,30}", new):
        return False, t("имя: латиница в нижнем регистре, цифры, - и _", "name: lowercase latin letters, digits, - and _")
    target = shutil.which(Path(sys.argv[0]).name) or str(Path(sys.argv[0]).resolve())
    link = Path.home() / ".local" / "bin" / new
    existing = shutil.which(new)
    if existing and Path(existing).resolve() != Path(target).resolve():
        return False, t(f"команда «{new}» уже есть в системе ({existing})", f"the command “{new}” already exists ({existing})")
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    return True, str(link)
