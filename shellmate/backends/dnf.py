"""Fedora and relatives (RHEL, Rocky, Alma, Nobara): dnf (4 or 5) / rpm.

dnf5 (Fedora 41+) changed the human-readable output, so queries use rpm and
`repoquery --queryformat` where possible and parse the rest leniently for both versions."""

import re
from datetime import datetime
from pathlib import Path

from shellmate.backends.base import Backend, Cleanup, Pkg, du, run_quiet

QF = "%{name}\t%{evr}\t%{summary}\n"
ARCHES = r"(?:x86_64|noarch|i686|aarch64|ppc64le|s390x|src)"
NEVRA = re.compile(rf"^(\S+?)-(?:\d+:)?([^-\s]+)-([^-\s]+)\.{ARCHES}\b")


def parse_qf(out: str, installed: set[str]) -> list[Pkg]:
    seen, res = set(), []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] and parts[0] not in seen:
            seen.add(parts[0])
            res.append(Pkg("dnf", parts[0], parts[1], parts[2] if len(parts) > 2 else "",
                           installed=parts[0] in installed))
    return res


def parse_search(out: str, installed: set[str]) -> list[Pkg]:
    """`dnf search`: dnf4 "name.arch : summary", dnf5 " name.arch\tsummary"."""
    seen, res = set(), []
    for line in out.splitlines():
        m = re.match(rf"^\s*([\w.+-]+?)\.{ARCHES}\s*(?::|\t)\s*(.*)$", line)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            res.append(Pkg("dnf", m.group(1), "", m.group(2).strip(), installed=m.group(1) in installed))
    return res


def parse_provides(out: str) -> list[str]:
    """`dnf provides /usr/bin/x`: lines "name-1.2-3.fc41.x86_64 : summary" (dnf4 and dnf5)."""
    return list(dict.fromkeys(m.group(1) for line in out.splitlines() if (m := NEVRA.match(line.strip()))))


def parse_list_versions(out: str, name: str) -> list[str]:
    """`dnf list --showduplicates name`: "name.arch  [epoch:]ver-rel  repo"."""
    vers = []
    for line in out.splitlines():
        m = re.match(rf"^{re.escape(name)}\.{ARCHES}\s+(\S+)\s+\S+", line.strip())
        if m and m.group(1) not in vers:
            vers.append(m.group(1))
    return vers


class Dnf(Backend):
    family = "fedora"
    tool = "dnf"
    tools_hint = "dnf / rpm / systemctl"
    other_tools = "apt, pacman or other distributions' tools"
    context_words = "fedora dnf"
    run_env = {"LC_ALL": "C.UTF-8"}

    @property
    def sources_label(self) -> str:
        return "репозитории dnf"

    def installed_names(self) -> set[str]:
        return set(run_quiet(["rpm", "-qa", "--qf", "%{NAME}\n"]).split())

    def search(self, term: str) -> list[Pkg]:
        installed = self.installed_names()
        if " " in term.strip():  # a description: full-text search
            return parse_search(run_quiet(["dnf", "search", "-q", *term.split()], timeout=120), installed)[:200]
        out = run_quiet(["dnf", "repoquery", "-q", "--qf", QF, f"*{term}*"], timeout=120)
        return parse_qf(out, installed)[:200]

    def installed(self, term: str) -> list[Pkg]:
        out = run_quiet(["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{SUMMARY}\n"])
        return [Pkg("local", p.name, p.version, p.desc, installed=True)
                for p in parse_qf(out, set()) if term.lower() in p.name.lower()]

    def installed_version(self, name: str) -> str | None:
        out = run_quiet(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}\n", name]).strip()
        return out.splitlines()[0] if out and "not installed" not in out else None

    def hold_hint(self, names: list[str]) -> str:
        return f"закрепи версию: sudo dnf versionlock add {' '.join(names)}  (нужен плагин versionlock)"

    def owner(self, path: str) -> tuple[str, str] | None:
        out = run_quiet(["rpm", "-qf", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\n", path]).strip()
        if not out or "not owned" in out or "\t" not in out:
            return None
        name, ver = out.splitlines()[0].split("\t", 1)
        return name, ver

    def info(self, name: str) -> list[tuple[str, str]]:
        out = run_quiet(["rpm", "-q", "--qf", "%{SUMMARY}\n%{VERSION}-%{RELEASE}\n%{SIZE}\n%{INSTALLTIME:date}\n", name])
        lines = out.splitlines()
        if len(lines) < 4 or "not installed" in out:
            return []
        rows = [("Описание", lines[0]), ("Версия", lines[1]),
                ("Размер", f"{int(lines[2]) / 1024**2:.1f} MiB" if lines[2].isdigit() else lines[2]),
                ("Дата", lines[3])]
        req = [line for line in run_quiet(["rpm", "-q", "--whatrequires", name]).splitlines()
               if line and "no package requires" not in line]
        if req:
            rows.append(("Нужен для", "  ".join(dict.fromkeys(NEVRA.match(r).group(1) if NEVRA.match(r) else r
                                                              for r in req))))
        return rows

    provides_hint = ""

    def provides(self, command: str) -> list[Pkg] | None:
        out = run_quiet(["dnf", "provides", "-q", f"/usr/bin/{command}"], timeout=120)
        return [Pkg("dnf", n, "") for n in parse_provides(out)]

    def versions(self, name: str) -> list[tuple[str, str]]:
        current = self.installed_version(name)
        out = run_quiet(["dnf", "list", "--showduplicates", "-q", name], timeout=120)
        vers = [v for v in parse_list_versions(out, name) if v.split(":")[-1] != current]
        vers.sort(key=lambda v: [int(x) if x.isdigit() else x for x in re.split(r"[.:-]", v)], reverse=True)
        return [(v, f"{name}-{v.split(':')[-1]}") for v in vers]

    def vercmp(self, a: str, b: str) -> int:
        out = run_quiet(["rpm", "--eval", f'%{{lua: print(rpm.vercmp("{a}", "{b}"))}}']).strip()
        return int(out) if out.lstrip("-").isdigit() else super().vercmp(a, b)

    def cleanup(self) -> list[Cleanup]:
        res = []
        size = sum(du(p) for p in (Path("/var/cache/dnf"), Path("/var/cache/libdnf5")) if p.is_dir())
        if size:
            res.append(Cleanup("скачанные пакеты в кэше dnf", size, ["sudo", "dnf", "clean", "packages"]))
        unneeded = run_quiet(["dnf", "repoquery", "--unneeded", "-q"], timeout=120).split()
        if unneeded:
            res.append(Cleanup(f"ненужные зависимости: {len(unneeded)} шт. — проверь список перед «y»", None,
                               ["sudo", "dnf", "autoremove"], keep=True))
        return res

    def last_upgrade(self) -> datetime | None:
        return None  # no news feeds to compare against on Fedora (yet)

    def auto_snapshot(self) -> str | None:
        for pkg in ("python3-dnf-plugin-snapper", "libdnf5-plugin-actions-snapper"):
            if self.installed_version(pkg):
                return "плагин snapper для dnf сам сделает снимок системы до и после обновления"
        return None

    def install_cmd(self, pkgs: list[Pkg]) -> list[str]:
        return ["sudo", "dnf", "install", *[p.name for p in pkgs]]

    def remove_cmd(self, names: list[str]) -> list[str]:
        return ["sudo", "dnf", "remove", *names]

    def upgrade_cmds(self) -> list[list[str]]:
        return [["sudo", "dnf", "upgrade", "--refresh"]]

    def downgrade_cmd(self, refs: list[str]) -> list[str]:
        return ["sudo", "dnf", "downgrade", *refs]
