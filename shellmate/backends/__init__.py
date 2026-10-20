"""Which distribution we run on and its package manager.

Arch family → pacman (+ paru/yay), Debian family (Ubuntu, Mint, Pop!_OS) → apt,
Fedora family (RHEL, Rocky, Alma, Nobara) → dnf. Flatpak and Snap are handled on top of
any of them in shellmate.packages."""

import re
import shutil
from functools import lru_cache
from pathlib import Path

from shellmate.backends.base import Backend, Cleanup, Distro, Pkg, du, run_quiet, run_rc  # noqa: F401

FAMILIES = {"arch": ("arch", "cachyos", "endeavouros", "manjaro", "garuda", "artix"),
            "debian": ("debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin", "kali", "raspbian"),
            "fedora": ("fedora", "rhel", "centos", "rocky", "almalinux", "nobara", "ultramarine")}


def parse_os_release(text: str) -> Distro:
    kv = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', text, re.M))
    ids = [kv.get("ID", "").lower(), *kv.get("ID_LIKE", "").lower().split()]
    family = next((f for f, members in FAMILIES.items() if any(i in members for i in ids)), "unknown")
    return Distro(ids[0] or "linux", kv.get("PRETTY_NAME") or kv.get("NAME") or "Linux", family, ids)


@lru_cache(maxsize=1)
def distro() -> Distro:
    try:
        d = parse_os_release(Path("/etc/os-release").read_text())
    except OSError:
        d = Distro("linux", "Linux", "unknown", [])
    if d.family == "unknown":  # unknown name: go by the package manager that's actually there
        for fam, tool in (("arch", "pacman"), ("debian", "apt-get"), ("fedora", "dnf")):
            if shutil.which(tool):
                d.family = fam
                break
    return d


@lru_cache(maxsize=1)
def current() -> Backend:
    d = distro()
    if d.family == "arch":
        from shellmate.backends.pacman import Pacman
        return Pacman(d)
    if d.family == "debian":
        from shellmate.backends.apt import Apt
        return Apt(d)
    if d.family == "fedora":
        from shellmate.backends.dnf import Dnf
        return Dnf(d)
    return Backend(d)
