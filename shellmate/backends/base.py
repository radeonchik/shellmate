"""What every distribution backend provides. Backends only READ the system and BUILD commands;
running them (always after the user's confirmation) is the caller's job."""

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class Pkg:
    source: str  # repo ("extra", "main"…), "aur", "flatpak", "snap" or "local" (installed)
    name: str  # for flatpak: the application id
    version: str
    desc: str = ""
    installed: bool = False
    votes: int = 0
    title: str = ""  # flatpak human-readable name

    @property
    def is_flatpak(self) -> bool:
        return self.source == "flatpak"

    @property
    def is_snap(self) -> bool:
        return self.source == "snap"

    @property
    def is_native(self) -> bool:
        return not (self.is_flatpak or self.is_snap)


@dataclass
class Cleanup:
    title: str
    size: int | None  # None = can't estimate cheaply
    cmd: list[str]
    keep: bool = False  # show even when small


@dataclass
class Distro:
    id: str  # "cachyos", "ubuntu", "fedora"…
    name: str  # human name for prompts: "CachyOS", "Ubuntu 24.04 LTS"
    family: str  # "arch" | "debian" | "fedora" | "unknown"
    ids: list[str] = field(default_factory=list)  # ID + ID_LIKE


def run_quiet(cmd: list[str], timeout: float = 60) -> str:
    """Runs a read-only query in the C locale (stable output to parse); '' on any failure."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return ANSI.sub("", out)


def run_rc(cmd: list[str], timeout: float = 30) -> int:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout,
                              env={**os.environ, "LC_ALL": "C"}).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 127


def du(path: Path) -> int:
    out = run_quiet(["du", "-sb", str(path)])
    return int(out.split()[0]) if out[:1].isdigit() else 0


class Backend:
    """Native package manager of a distribution family."""
    family = "unknown"
    tool = ""  # "pacman", "apt", "dnf"
    sources_label = "репозитории"  # for the capabilities list: "репозитории, AUR"
    tools_hint = ""  # for prompts: "pacman / paru / systemctl"
    other_tools = ""  # for prompts: tools of OTHER distros the model must not suggest
    context_words = ""  # what a question is about unless it names another distro (evidence.py)
    run_env: dict[str, str] = {}  # extra environment for commands we run (stable error messages)

    def __init__(self, distro: Distro):
        self.distro = distro

    # --- queries ---------------------------------------------------------------------
    def search(self, term: str) -> list[Pkg]:
        return []

    def installed(self, term: str) -> list[Pkg]:
        return []

    def installed_version(self, name: str) -> str | None:
        return None

    def installed_names(self) -> set[str]:
        return set()

    def hold_hint(self, names: list[str]) -> str:
        """How to keep a downgraded package from being upgraded again."""
        return ""

    def owner(self, path: str) -> tuple[str, str] | None:
        """(package, version) that owns a file."""
        return None

    def info(self, name: str) -> list[tuple[str, str]]:
        return []

    def provides(self, command: str) -> list[Pkg] | None:
        """Packages that ship a command; None = the lookup database isn't available."""
        return None

    provides_hint = ""  # how to enable provides() when it returns None
    provides_setup: list[str] | None = None  # the command for that, offered to run

    @property
    def provides_setup_text(self) -> str:
        return " ".join(self.provides_setup or [])

    def versions(self, name: str) -> list[tuple[str, str]]:
        """Other installable versions, newest first: (version, reference for downgrade_cmd)."""
        return []

    def vercmp(self, a: str, b: str) -> int:
        return (a > b) - (a < b)

    def cleanup(self) -> list[Cleanup]:
        return []

    def last_upgrade(self) -> datetime | None:
        return None

    def news_feeds(self) -> list[tuple[str, str]]:
        return []

    def auto_snapshot(self) -> str | None:
        """A note if snapshots are taken automatically on every package transaction."""
        return None

    def ollama_install(self, gpu: str) -> str:
        return "curl -fsSL https://ollama.com/install.sh | sh"

    # --- commands ------------------------------------------------------------------
    def install_cmd(self, pkgs: list[Pkg]) -> list[str]:
        raise NotImplementedError

    def remove_cmd(self, names: list[str]) -> list[str]:
        raise NotImplementedError

    def upgrade_cmds(self) -> list[list[str]]:
        raise NotImplementedError

    def downgrade_cmd(self, refs: list[str]) -> list[str]:
        raise NotImplementedError
