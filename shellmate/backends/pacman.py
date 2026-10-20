"""Arch Linux and derivatives (CachyOS, EndeavourOS, Manjaro): pacman + an AUR helper."""

import re
import shutil
from datetime import datetime
from functools import cmp_to_key
from pathlib import Path

from shellmate.backends.base import Backend, Cleanup, Pkg, du, run_quiet

HEADER = re.compile(r"^(\S+)/(\S+) (\S+)(.*)$")
PKG_CACHE = Path("/var/cache/pacman/pkg")
PACMAN_LOG = Path("/var/log/pacman.log")


def parse_pacman(out: str) -> list[Pkg]:
    """Parses `pacman/paru/yay -Ss` and `pacman -Qs` output."""
    res: list[Pkg] = []
    cur = None
    for line in out.splitlines():
        if line[:1].isspace():
            if cur:
                cur.desc = line.strip()
            continue
        m = HEADER.match(line)
        if not m:
            cur = None
            continue
        source, name, version, rest = m.groups()
        votes = re.search(r"\[\+(\d+)", rest)
        cur = Pkg(source, name, version,
                  installed=source == "local" or "[installed" in rest.lower(),
                  votes=int(votes.group(1)) if votes else 0)
        res.append(cur)
    return res


class Pacman(Backend):
    family = "arch"
    tool = "pacman"
    tools_hint = "pacman / paru / systemctl"
    other_tools = "apt, dnf or other distributions' tools"
    context_words = "arch pacman"

    @property
    def helper(self) -> str | None:
        return next((h for h in ("paru", "yay") if shutil.which(h)), None)

    @property
    def sources_label(self) -> str:
        return "репозитории, AUR" if self.helper else "репозитории"

    def search(self, term: str) -> list[Pkg]:
        return parse_pacman(run_quiet([self.helper or "pacman", "-Ss", term]))

    def installed(self, term: str) -> list[Pkg]:
        return parse_pacman(run_quiet(["pacman", "-Qs", term]))

    def installed_version(self, name: str) -> str | None:
        out = run_quiet(["pacman", "-Q", name]).split()
        return out[1] if len(out) == 2 else None

    def installed_names(self) -> set[str]:
        return set(run_quiet(["pacman", "-Qq"]).split())

    def hold_hint(self, names: list[str]) -> str:
        return f"добавь в /etc/pacman.conf строку: IgnorePkg = {' '.join(names)}  (и убери, когда исправят)"

    def owner(self, path: str) -> tuple[str, str] | None:
        m = re.search(r"is owned by (\S+) (\S+)", run_quiet(["pacman", "-Qo", path]))
        return (m.group(1), m.group(2)) if m else None

    def info(self, name: str) -> list[tuple[str, str]]:
        info = dict(re.findall(r"^([A-Za-z ]+?)\s*: (.*)$", run_quiet(["pacman", "-Qi", name]), re.M))
        labels = [("Description", "Описание"), ("Version", "Версия"), ("Installed Size", "Размер"),
                  ("Install Reason", "Установлен"), ("Install Date", "Дата"), ("Required By", "Нужен для")]
        reasons = {"Explicitly installed": "вручную",
                   "Installed as a dependency for another package": "как зависимость"}
        return [(ru, reasons.get(info[en], info[en])) for en, ru in labels if info.get(en)]

    provides_hint = "нужна база файлов pacman"
    provides_setup = ["sudo", "pacman", "-Fy"]

    def provides(self, command: str) -> list[Pkg] | None:
        if not any(Path("/var/lib/pacman/sync").glob("*.files")):
            return None
        found = re.findall(r"^(\S+)/(\S+) (\S+)", run_quiet(["pacman", "-F", f"usr/bin/{command}"]), re.M)
        return [Pkg(repo, name, ver) for repo, name, ver in found]

    def versions(self, name: str) -> list[tuple[str, str]]:
        """Package files still in the pacman / AUR-helper caches, newest first."""
        # name-VERSION-REL-ARCH.pkg.tar.*: exactly 3 dash-separated parts after the name,
        # so "nvidia" doesn't match "nvidia-utils-…"
        pat = re.compile(rf"^{re.escape(name)}-([^-]+-[^-]+)-[^-]+\.pkg\.tar\.(?:zst|xz)$")
        dirs = [PKG_CACHE]
        if self.helper:
            dirs.append(Path.home() / ".cache" / self.helper / "clone" / name)
        found: dict[str, str] = {}
        for d in dirs:
            for f in d.glob(f"{name}-*.pkg.tar.*") if d.is_dir() else ():
                m = pat.match(f.name)
                if m:
                    found.setdefault(m.group(1), str(f))
        return sorted(found.items(), key=cmp_to_key(lambda a, b: self.vercmp(b[0], a[0])))

    def vercmp(self, a: str, b: str) -> int:
        out = run_quiet(["vercmp", a, b]).strip()
        return int(out) if out.lstrip("-").isdigit() else 0

    def cleanup(self) -> list[Cleanup]:
        res = []
        if shutil.which("paccache"):
            def saved(args: str) -> int:
                m = re.search(r"disk space saved: ([\d.]+) (\w+)", run_quiet(["paccache", f"-d{args}"]))
                units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
                return int(float(m.group(1)) * units.get(m.group(2), 1)) if m else 0
            res.append(Cleanup("старые версии пакетов в кэше pacman (оставлю по одной)", saved("k1"),
                               ["sudo", "paccache", "-rk1"]))
            res.append(Cleanup("кэш уже удалённых пакетов", saved("uk0"), ["sudo", "paccache", "-ruk0"]))
        if self.helper:
            res.append(Cleanup(f"кэш сборок AUR ({self.helper})", du(Path.home() / ".cache" / self.helper),
                               [self.helper, "-Sc"]))
        orphans = run_quiet(["pacman", "-Qdtq"]).split()
        if orphans:
            info = run_quiet(["pacman", "-Qi", *orphans])
            units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
            size = sum(int(float(n) * units.get(u, 1))
                       for n, u in re.findall(r"Installed Size\s*:\s*([\d.]+) (\w+)", info))
            res.append(Cleanup(f"пакеты-сироты: {len(orphans)} шт. — зависимости, которые больше никому "
                               f"не нужны; проверь список перед «y»", size,
                               ["sudo", "pacman", "-Rns", *orphans], keep=True))
        return res

    def last_upgrade(self) -> datetime | None:
        try:
            with PACMAN_LOG.open(errors="replace") as f:
                stamps = [m.group(1) for line in f
                          if "starting full system upgrade" in line and (m := re.match(r"\[([^\]]+)\]", line))]
        except OSError:
            return None
        try:
            return datetime.fromisoformat(stamps[-1].replace("Z", "+00:00")) if stamps else None
        except ValueError:
            return None

    def news_feeds(self) -> list[tuple[str, str]]:
        feeds = [("Arch Linux", "https://archlinux.org/feeds/news/")]
        if "cachyos" in self.distro.ids:
            feeds.append(("CachyOS", "https://cachyos.org/rss.xml"))
        return feeds

    def auto_snapshot(self) -> str | None:
        return "snap-pac сам сделает снимок системы до и после обновления" if self.installed_version("snap-pac") else None

    def ollama_install(self, gpu: str) -> str:
        pkg = {"cuda": "ollama-cuda", "rocm": "ollama-rocm"}.get(gpu, "ollama")
        return f"sudo pacman -S {pkg} && sudo systemctl enable --now ollama"

    def install_cmd(self, pkgs: list[Pkg]) -> list[str]:
        if not self.helper and any(p.source == "aur" for p in pkgs):
            raise RuntimeError("для пакетов из AUR нужен paru или yay")
        # repo/name pins the exact source the user picked (same name can exist in repo and AUR)
        targets = [f"{p.source}/{p.name}" if p.source not in ("", "local", "repo") else p.name for p in pkgs]
        return [self.helper, "-S", *targets] if self.helper else ["sudo", "pacman", "-S", *targets]

    def remove_cmd(self, names: list[str]) -> list[str]:
        return ["sudo", "pacman", "-Rns", *names]

    def upgrade_cmds(self) -> list[list[str]]:
        return [[self.helper, "-Syu"] if self.helper else ["sudo", "pacman", "-Syu"]]

    def downgrade_cmd(self, refs: list[str]) -> list[str]:
        return ["sudo", "pacman", "-U", *refs]
