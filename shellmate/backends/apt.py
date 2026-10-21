"""Debian and derivatives (Ubuntu, Mint, Pop!_OS): apt / dpkg."""

import re
import shutil
from datetime import datetime
from pathlib import Path

from shellmate.backends.base import Backend, Cleanup, Pkg, du, info_labels, run_quiet, run_rc
from shellmate.i18n import t

APT_HISTORY = Path("/var/log/apt/history.log")


def parse_search(out: str, installed: set[str]) -> list[Pkg]:
    """`apt-cache search` lines: "name - short description"."""
    res = []
    for line in out.splitlines():
        name, sep, desc = line.partition(" - ")
        if sep and name and " " not in name:
            res.append(Pkg("apt", name, "", desc.strip(), installed=name in installed))
    return res


def parse_dpkg_list(out: str) -> list[Pkg]:
    """dpkg-query -W -f='${binary:Package}\\t${Version}\\t${db:Status-Abbrev}\\t${binary:Summary}\\n'"""
    res = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].startswith("ii"):
            res.append(Pkg("local", parts[0].split(":")[0], parts[1], parts[3] if len(parts) > 3 else "",
                           installed=True))
    return res


def parse_madison(out: str) -> list[str]:
    """`apt-cache madison name` lines: " name | 1.2-3 | http://… bookworm/main amd64 Packages"."""
    vers = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3 and parts[1] and parts[1] not in vers:
            vers.append(parts[1])
    return vers


def parse_history(text: str) -> datetime | None:
    """Last apt run that upgraded something, from /var/log/apt/history.log."""
    last = None
    for block in text.split("\n\n"):
        m = re.search(r"^Start-Date: (\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})", block, re.M)
        if m and re.search(r"^Upgrade: ", block, re.M):
            last = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}").astimezone()
    return last


class Apt(Backend):
    family = "debian"
    tool = "apt"
    tools_hint = "apt / dpkg / systemctl"
    other_tools = "pacman, dnf or other distributions' tools"
    context_words = "debian ubuntu apt"
    run_env = {"LC_ALL": "C.UTF-8"}  # apt's messages in English, so errors.py recognises them

    @property
    def sources_label(self) -> str:
        return t("репозитории apt", "apt repositories") + (", Snap" if shutil.which("snap") else "")

    def installed_names(self) -> set[str]:
        out = run_quiet(["dpkg-query", "-W", "-f=${binary:Package}\t${db:Status-Abbrev}\n"])
        return {line.split("\t")[0].split(":")[0] for line in out.splitlines() if "\tii" in line}

    def search(self, term: str) -> list[Pkg]:
        # a single word is a name guess; several words describe what's wanted
        names_only = [] if " " in term.strip() else ["--names-only"]
        words = [re.sub(r"([+.*?^$()\[\]{}|\\])", r"\\\1", w) for w in term.split()]  # apt-cache takes regexes
        return parse_search(run_quiet(["apt-cache", "search", *names_only, *words]), self.installed_names())[:200]

    def installed(self, term: str) -> list[Pkg]:
        out = run_quiet(["dpkg-query", "-W",
                         "-f=${binary:Package}\t${Version}\t${db:Status-Abbrev}\t${binary:Summary}\n", f"*{term}*"])
        return parse_dpkg_list(out)

    def installed_version(self, name: str) -> str | None:
        out = run_quiet(["dpkg-query", "-W", "-f=${db:Status-Abbrev}\t${Version}", name])
        return out.split("\t", 1)[1].strip() if out.startswith("ii") and "\t" in out else None

    def hold_hint(self, names: list[str]) -> str:
        return t(f"закрепи версию: sudo apt-mark hold {' '.join(names)}  (снять: sudo apt-mark unhold …)",
                 f"pin the version: sudo apt-mark hold {' '.join(names)}  (undo: sudo apt-mark unhold …)")

    def owner(self, path: str) -> tuple[str, str] | None:
        real = str(Path(path).resolve())  # /bin -> /usr/bin on merged-usr systems
        for p in dict.fromkeys([path, real]):
            m = re.match(r"^([^:\s,]+)(?::\w+)?(?:, [^:]+)?: ", run_quiet(["dpkg", "-S", p]))
            if m:
                return m.group(1), self.installed_version(m.group(1)) or ""
        return None

    def info(self, name: str) -> list[tuple[str, str]]:
        out = run_quiet(["dpkg-query", "-W", "-f=${binary:Summary}\n${Version}\n${Installed-Size}\n", name])
        lines = out.splitlines()
        if len(lines) < 3:
            return []
        manual = name in run_quiet(["apt-mark", "showmanual", name]).split()
        lb = info_labels()
        rows = [(lb["desc"], lines[0]), (lb["version"], lines[1]),
                (lb["size"], f"{int(lines[2]) / 1024:.1f} MiB" if lines[2].isdigit() else lines[2]),
                (lb["reason"], lb["manual"] if manual else lb["dep"])]
        rdeps = run_quiet(["apt-cache", "rdepends", "--installed", "--no-recommends", "--no-suggests", name],
                          timeout=20).splitlines()
        needed = [r.strip().lstrip("|") for r in rdeps[2:] if r.strip()]
        if needed:
            rows.append((lb["needed"], "  ".join(dict.fromkeys(needed))))
        return rows

    @property
    def provides_hint(self) -> str:
        return t("нужен apt-file с базой файлов", "apt-file with its file database is needed")

    provides_setup = ["sh", "-c", "sudo apt install -y apt-file && sudo apt-file update"]
    provides_setup_text = "sudo apt install apt-file && sudo apt-file update"

    def provides(self, command: str) -> list[Pkg] | None:
        cnf = Path("/usr/lib/command-not-found")
        if cnf.exists():  # Ubuntu ships a database of "command -> package"
            out = run_quiet(["python3", str(cnf), "--ignore-installed", "--no-failure-msg", command], timeout=15)
            found = [Pkg("apt", n, "") for n in dict.fromkeys(re.findall(r"apt install (\S+)", out))]
            found += [Pkg("snap", n, "") for n in dict.fromkeys(re.findall(r"snap install (\S+)", out))]
            if found or out.strip():
                return found
        if not shutil.which("apt-file"):
            return None
        out = run_quiet(["apt-file", "search", "-x", f"/(usr/)?s?bin/{re.escape(command)}$"], timeout=30)
        if "cache is empty" in out.lower():
            return None
        names = dict.fromkeys(line.split(":")[0] for line in out.splitlines() if ":" in line)
        return [Pkg("apt", n, "") for n in names]

    def versions(self, name: str) -> list[tuple[str, str]]:
        current = self.installed_version(name)
        return [(v, f"{name}={v}") for v in parse_madison(run_quiet(["apt-cache", "madison", name]))
                if v != current]

    def vercmp(self, a: str, b: str) -> int:
        if run_rc(["dpkg", "--compare-versions", a, "lt", b]) == 0:
            return -1
        return 0 if run_rc(["dpkg", "--compare-versions", a, "eq", b]) == 0 else 1

    def cleanup(self) -> list[Cleanup]:
        res = []
        cache = Path("/var/cache/apt/archives")
        if cache.is_dir():
            res.append(Cleanup(t("скачанные установочные пакеты в кэше apt", "downloaded packages in the apt cache"), du(cache), ["sudo", "apt", "clean"]))
        sim = run_quiet(["apt-get", "-s", "autoremove"], timeout=60)
        unused = re.findall(r"^Remv (\S+)", sim, re.M)
        if unused:
            res.append(Cleanup(t(f"ненужные зависимости и старые ядра: {len(unused)} шт. — проверь список перед «y»",
                                 f"unneeded dependencies and old kernels: {len(unused)} — check the list before “y”"), None, ["sudo", "apt", "autoremove"], keep=True))
        return res

    def last_upgrade(self) -> datetime | None:
        try:
            return parse_history(APT_HISTORY.read_text(errors="replace"))
        except OSError:
            return None

    def auto_snapshot(self) -> str | None:
        for pkg in ("apt-btrfs-snapshot", "timeshift-autosnap-apt"):
            if self.installed_version(pkg):
                return t(f"{pkg} сам сделает снимок системы перед обновлением", f"{pkg} will take a system snapshot before the update")
        return None

    def install_cmd(self, pkgs: list[Pkg]) -> list[str]:
        return ["sudo", "apt", "install", *[p.name for p in pkgs]]

    def remove_cmd(self, names: list[str]) -> list[str]:
        return ["sudo", "apt", "remove", *names]

    def upgrade_cmds(self) -> list[list[str]]:
        return [["sudo", "apt", "update"], ["sudo", "apt", "upgrade"]]

    def downgrade_cmd(self, refs: list[str]) -> list[str]:
        return ["sudo", "apt", "install", "--allow-downgrades", *refs]
