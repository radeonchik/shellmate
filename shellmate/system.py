"""System state readers: failed units, boot errors, services, processes, hardware, network.
Everything here only READS; commands that change something are built separately and
always go through the user's confirmation."""

import json
import os
import re
import shutil
import socket
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from shellmate.packages import run_quiet

# --- health -------------------------------------------------------------------


@dataclass
class Unit:
    name: str
    desc: str
    state: str  # e.g. "active running", "failed", "inactive dead", "disabled"
    user: bool  # --user unit (pipewire etc.) vs system unit

    def cmd(self, action: str) -> list[str]:
        return ["systemctl", "--user", action, self.name] if self.user else \
            ["sudo", "systemctl", action, self.name]

    def status(self, lines: int = 20) -> str:
        scope = ["--user"] if self.user else []
        return run_quiet(["systemctl", *scope, "status", self.name, "--no-pager", "-n", str(lines)])


def _list_units(user: bool, *extra: str) -> list[Unit]:
    scope = ["--user"] if user else []
    out = run_quiet(["systemctl", *scope, "list-units", "--no-legend", "--plain", *extra])
    res = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4:
            res.append(Unit(parts[0], parts[4] if len(parts) > 4 else "",
                            f"{parts[2]} {parts[3]}", user))
    return res


def failed_units() -> list[Unit]:
    return _list_units(False, "--failed") + _list_units(True, "--failed")


@dataclass
class LogError:
    source: str
    message: str
    count: int


def boot_errors(limit: int = 12) -> list[LogError]:
    """Errors since boot, de-duplicated (numbers/hex ignored) and most frequent first."""
    out = run_quiet(["journalctl", "-b", "-p", "err", "--no-pager", "-o", "json",
                     "--output-fields=SYSLOG_IDENTIFIER,MESSAGE"])
    groups: dict[tuple[str, str], LogError] = {}
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = e.get("MESSAGE") or ""
        if isinstance(msg, list):  # non-UTF8 messages come as byte arrays
            msg = bytes(msg).decode(errors="replace")
        src = e.get("SYSLOG_IDENTIFIER") or "?"
        key = (src, re.sub(r"0x[0-9a-f]+|\d+", "#", msg.lower()))
        if key in groups:
            groups[key].count += 1
        else:
            groups[key] = LogError(src, msg.strip(), 1)
    return sorted(groups.values(), key=lambda g: -g.count)[:limit]


# --- services -----------------------------------------------------------------

# fallbacks when the model's keyword doesn't match any unit
ALIASES = {
    "sound": ["pipewire", "wireplumber", "pulseaudio"], "audio": ["pipewire", "wireplumber"],
    "звук": ["pipewire", "wireplumber"], "wifi": ["NetworkManager", "iwd", "wpa_supplicant"],
    "internet": ["NetworkManager", "systemd-networkd", "systemd-resolved"],
    "network": ["NetworkManager", "systemd-networkd"], "printer": ["cups"],
    "ssh": ["sshd"], "time": ["systemd-timesyncd", "chronyd", "ntpd"],
}


def find_units(keyword: str) -> list[Unit]:
    units: dict[tuple[str, bool], Unit] = {}
    for user in (False, True):
        for u in _list_units(user, "--all", "--type=service"):
            units[(u.name, user)] = u
        # installed but never started units don't show up in list-units
        scope = ["--user"] if user else []
        for line in run_quiet(["systemctl", *scope, "list-unit-files", "--type=service",
                               "--no-legend", "--plain"]).splitlines():
            parts = line.split()
            if parts and (parts[0], user) not in units and "@" not in parts[0]:
                units[(parts[0], user)] = Unit(parts[0], "", parts[1] if len(parts) > 1 else "", user)

    def matching(word: str) -> list[Unit]:
        w = word.lower()
        return [u for u in units.values() if w in u.name.lower() or w in u.desc.lower()]

    found = matching(keyword) if keyword else []
    for alias in ALIASES.get(keyword.lower(), []):
        found += matching(alias)
    # failed first, then running, then the rest
    order = {"failed": 0, "active": 1}
    uniq = {(u.name, u.user): u for u in found}.values()
    return sorted(uniq, key=lambda u: (order.get(u.state.split()[0], 2), u.name))


# --- processes ----------------------------------------------------------------

CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


@dataclass
class ProcGroup:
    name: str
    pids: list[int] = field(default_factory=list)
    cpu: float = 0.0  # percent of ONE core (can exceed 100)
    rss: int = 0
    foreign: bool = False  # has processes of other users (needs sudo to kill)


def _snapshot() -> dict[int, tuple[str, int, int, int]]:
    res = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
            uid = d.stat().st_uid
        except OSError:
            continue
        name = stat[stat.find("(") + 1:stat.rfind(")")]
        f = stat[stat.rfind(")") + 2:].split()
        res[int(d.name)] = (name, int(f[11]) + int(f[12]), int(f[21]) * PAGE, uid)
    return res


def processes(interval: float = 0.8) -> list[ProcGroup]:
    """CPU is measured over `interval` (ps shows lifetime averages, which lie)."""
    a = _snapshot()
    time.sleep(interval)
    b = _snapshot()
    me, my_uid = os.getpid(), os.getuid()
    groups: dict[str, ProcGroup] = {}
    for pid, (name, ticks, rss, uid) in b.items():
        if pid == me or rss == 0:  # rss 0 = kernel thread
            continue
        g = groups.setdefault(name, ProcGroup(name))
        g.pids.append(pid)
        g.rss += rss
        g.cpu += max(0, ticks - a.get(pid, (name, ticks))[1]) / CLK / interval * 100
        g.foreign |= uid != my_uid
    return list(groups.values())


def kill_command(g: ProcGroup) -> list[str]:
    cmd = ["kill", *map(str, g.pids)]
    return ["sudo", *cmd] if g.foreign else cmd


def meminfo() -> dict[str, int]:
    res = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        res[k] = int(v.split()[0]) * 1024
    return res


# --- sysinfo ------------------------------------------------------------------


def _read(path: Path | str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def sysinfo() -> list[tuple[str, str]]:
    rows = []
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', _read("/etc/os-release"), re.M))
    rows.append(("Система", f"{osr.get('PRETTY_NAME', 'Linux')}, ядро {os.uname().release}"))
    up = float(_read("/proc/uptime").split()[0] or 0)
    rows.append(("Работает", f"{int(up // 86400)} д {int(up % 86400 // 3600)} ч {int(up % 3600 // 60)} мин"))
    cpu = re.search(r"model name\s*:\s*(.+)", _read("/proc/cpuinfo"))
    load = os.getloadavg()
    rows.append(("Процессор", f"{cpu.group(1) if cpu else '?'} · {os.cpu_count()} потоков · "
                              f"нагрузка {load[0]:.1f}"))
    mem = meminfo()
    used = mem["MemTotal"] - mem["MemAvailable"]
    rows.append(("Память", f"{used / 1024**3:.1f} из {mem['MemTotal'] / 1024**3:.1f} ГБ занято"
                           + (f", swap {(mem['SwapTotal'] - mem['SwapFree']) / 1024**3:.1f} ГБ"
                              if mem.get("SwapTotal") else "")))
    for gpu in gpus():
        rows.append(("Видеокарта", gpu))
    temps = temperatures()
    if temps:
        rows.append(("Температура", ", ".join(f"{k} {v:.0f}°C" for k, v in temps)))
    bat = battery()
    if bat:
        rows.append(("Батарея", bat))
    return rows


def gpus() -> list[str]:
    res = []
    for line in run_quiet(["lspci", "-D"]).splitlines():
        if not re.search(r"VGA|3D controller|Display controller", line):
            continue
        addr = line.split()[0]
        name = re.search(r"\[([^\]]+)\][^\[]*$", line)
        name = name.group(1) if name else line.split(": ", 1)[-1]
        dev = Path("/sys/bus/pci/devices") / addr
        driver = (dev / "driver").resolve().name if (dev / "driver").exists() else "нет драйвера"
        extra = ""
        if driver == "nvidia":
            ver = re.search(r"Module\s+.*?(\d+\.\d+(?:\.\d+)?)", _read("/proc/driver/nvidia/version"))
            extra = f" {ver.group(1)}" if ver else ""
            # nvidia-smi would wake a sleeping laptop dGPU, so only ask when it's already on
            if _read(dev / "power" / "runtime_status") == "suspended":
                extra += ", сейчас спит (экономит батарею)"
            elif shutil.which("nvidia-smi"):
                q = run_quiet(["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,memory.used,"
                               "memory.total", "--format=csv,noheader,nounits"]).split(",")
                if len(q) == 4:
                    extra += f", {q[0].strip()}°C, загрузка {q[1].strip()}%, VRAM {q[2].strip()}/{q[3].strip()} МБ"
        res.append(f"{name} · драйвер {driver}{extra}")
    return res


def temperatures() -> list[tuple[str, float]]:
    res = []
    wanted = {"coretemp": "CPU", "k10temp": "CPU", "zenpower": "CPU", "nvme": "SSD", "amdgpu": "GPU"}
    for h in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        label = wanted.get(_read(h / "name"))
        if not label or any(k == label for k, _ in res):
            continue
        vals = [int(_read(f)) / 1000 for f in h.glob("temp*_input") if _read(f).lstrip("-").isdigit()]
        if vals:
            res.append((label, max(vals)))
    return res


def battery() -> str:
    for b in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        cap, status = _read(b / "capacity"), _read(b / "status")
        statuses = {"Charging": "заряжается", "Discharging": "разряжается", "Full": "полная",
                    "Not charging": "не заряжается"}
        power = _read(b / "power_now")
        watts = f", {int(power) / 1e6:.1f} Вт" if power.isdigit() and int(power) else ""
        health = ""
        full, design = _read(b / "energy_full"), _read(b / "energy_full_design")
        if full.isdigit() and design.isdigit() and int(design):
            health = f", износ {100 - round(100 * int(full) / int(design))}%"
        return f"{cap}% ({statuses.get(status, status)}{watts}{health})"
    return ""


# --- network ------------------------------------------------------------------


def addresses() -> list[tuple[str, str]]:
    res = []
    for line in run_quiet(["ip", "-br", "-4", "addr", "show", "up"]).splitlines():
        parts = line.split()
        if parts and parts[0] != "lo" and len(parts) >= 3:
            res.append((parts[0], parts[2].split("/")[0]))
    return res


def gateway() -> str:
    m = re.search(r"default via (\S+) dev (\S+)", run_quiet(["ip", "route", "show", "default"]))
    return f"{m.group(1)} ({m.group(2)})" if m else ""


def wifi() -> str:
    if shutil.which("nmcli"):
        for line in run_quiet(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"]).splitlines():
            parts = line.split(":")
            if parts[0] == "yes" and len(parts) >= 3:
                return f"{parts[1]} (сигнал {parts[2]}%)"
    return ""


def check_internet() -> tuple[bool, bool, float | None]:
    """(dns works, tcp to the internet works, latency in ms)."""
    try:
        socket.getaddrinfo("archlinux.org", 443)
        dns = True
    except OSError:
        dns = False
    t = time.perf_counter()
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        return dns, True, (time.perf_counter() - t) * 1000
    except OSError:
        return dns, False, None


def public_ip() -> str:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            return r.read().decode().strip()
    except OSError:
        return ""


def port_users(port: int) -> list[str]:
    """Who listens on a port. Without root, other users' process names are hidden."""
    out = run_quiet(["ss", "-ltnupH", f"sport = :{port}"])
    res = []
    for line in out.splitlines():
        parts = line.split()
        procs = re.findall(r'\("([^"]+)",pid=(\d+)', line)
        who = ", ".join(f"{n} (pid {p})" for n, p in procs) or "процесс другого пользователя (нужен sudo)"
        res.append(f"{parts[0]} {parts[4] if len(parts) > 4 else ''} ← {who}")
    return res
