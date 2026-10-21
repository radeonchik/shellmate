"""Disk usage, cleanup candidates and big files. Deterministic — the model only routes here."""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from shellmate import backends
from shellmate.backends.base import Cleanup, du, run_quiet
from shellmate.i18n import t

MIN_CLEANUP = 50 * 1024**2  # don't bother the user with less than 50 MB
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}


def human(n: float) -> str:
    units = t("Б КБ МБ ГБ ТБ", "B KB MB GB TB").split()
    for unit in units[:-1]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == units[0] else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} {units[-1]}"


def _parse_size(num: str, unit: str) -> int:
    return int(float(num) * UNITS.get(unit, 1))


@dataclass
class Mount:
    target: str
    fstype: str
    size: int
    used: int
    avail: int

    @property
    def pct(self) -> int:
        return round(100 * self.used / self.size) if self.size else 0


def mounts() -> list[Mount]:
    out = run_quiet(["df", "-B1", "--output=source,fstype,size,used,avail,target",
                "-x", "tmpfs", "-x", "devtmpfs", "-x", "efivarfs", "-x", "overlay", "-x", "squashfs"])
    seen, res = set(), []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 5)
        if len(parts) < 6 or not parts[2].isdigit():
            continue
        source, fstype, size, used, avail, target = parts
        if source in seen:  # btrfs subvolumes show the same device many times
            continue
        seen.add(source)
        res.append(Mount(target, fstype, int(size), int(used), int(avail)))
    return res


def _du(path: Path) -> int:
    return du(path)


def cleanup_candidates() -> list[Cleanup]:
    """Package-manager caches and leftovers (from the distro backend) + logs, trash, Flatpak."""
    home = Path.home()
    res = backends.current().cleanup()
    m = re.search(r"take up ([\d.]+)(\w)", run_quiet(["journalctl", "--disk-usage"]))
    if m:
        journal = _parse_size(m.group(1), {"K": "KiB", "M": "MiB", "G": "GiB"}.get(m.group(2), "B"))
        if journal > 300 * 1024**2:
            res.append(Cleanup(t("системные логи (оставлю последние 100 МБ)", "system logs (keeping the last 100 MB)"), journal - 100 * 1024**2,
                               ["sudo", "journalctl", "--vacuum-size=100M"]))
    trash = home / ".local" / "share" / "Trash"
    if trash.exists() and shutil.which("gio"):
        res.append(Cleanup(t("корзина", "trash"), _du(trash), ["gio", "trash", "--empty"]))
    if shutil.which("flatpak"):
        res.append(Cleanup(t("неиспользуемые рантаймы Flatpak", "unused Flatpak runtimes"), None,
                           ["flatpak", "uninstall", "--unused"]))
    useful = [c for c in res if c.size is None or c.keep or c.size >= MIN_CLEANUP]
    return sorted(useful, key=lambda c: -(c.size or 0))


@dataclass
class Entry:
    path: str
    size: int
    is_dir: bool


def biggest(path: Path, files: int = 15, dirs: int = 10, min_file: str = "+100M") -> list[Entry]:
    """Largest direct subfolders and largest files anywhere below `path` (same filesystem)."""
    res = []
    for line in run_quiet(["du", "-x", "-d1", "-b", str(path)], timeout=600).splitlines():
        size, _, p = line.partition("\t")
        if size.isdigit() and p != str(path):
            res.append(Entry(p, int(size), True))
    res = sorted(res, key=lambda e: -e.size)[:dirs]
    found = []
    for line in run_quiet(["find", str(path), "-xdev", "-type", "f", "-size", min_file,
                      "-printf", r"%s\t%p\n"], timeout=600).splitlines():
        size, _, p = line.partition("\t")
        if size.isdigit():
            found.append(Entry(p, int(size), False))
    return res + sorted(found, key=lambda e: -e.size)[:files]


def resolve_path(text: str) -> Path:
    """Model gives things like "~/Downloads"; fall back to home if it doesn't exist."""
    if text:
        p = Path(text).expanduser()
        if p.is_dir():
            return p
        # common folder names in any language/case: "загрузки" -> ~/Загрузки
        for child in Path.home().iterdir():
            if child.is_dir() and child.name.lower() == Path(text).name.lower():
                return child
    return Path.home()
