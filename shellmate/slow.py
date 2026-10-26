"""Why the computer is slow, hot, or drains the battery: measurements read by code (/proc, /sys,
pressure stall info), turned into findings by plain rules. The model takes no part in it."""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from shellmate.i18n import t
from shellmate.system import ProcGroup, meminfo, processes, temperatures

# programs that load the machine for a while on their own: worth naming, not killing
BACKGROUND = {
    "pacman": ("обновление или установка пакетов", "packages being updated or installed"),
    "paru": ("обновление или установка пакетов", "packages being updated or installed"),
    "yay": ("обновление или установка пакетов", "packages being updated or installed"),
    "makepkg": ("сборка пакета из AUR", "an AUR package being built"),
    "cc1plus": ("компиляция программы (C++)", "a program being compiled (C++)"),
    "cc1": ("компиляция программы (C)", "a program being compiled (C)"),
    "rustc": ("компиляция программы (Rust)", "a program being compiled (Rust)"),
    "dkms": ("сборка модуля ядра (драйвер)", "a kernel module (driver) being built"),
    "mkinitcpio": ("сборка initramfs после обновления ядра", "initramfs being rebuilt after a kernel update"),
    "dracut": ("сборка initramfs после обновления ядра", "initramfs being rebuilt after a kernel update"),
    "apt": ("обновление или установка пакетов", "packages being updated or installed"),
    "dpkg": ("обновление или установка пакетов", "packages being updated or installed"),
    "unattended-upgr": ("автоматическое обновление", "automatic updates"),
    "dnf": ("обновление или установка пакетов", "packages being updated or installed"),
    "packagekitd": ("проверка обновлений (PackageKit)", "checking for updates (PackageKit)"),
    "baloo_file": ("индексация файлов KDE (Baloo)", "KDE file indexing (Baloo)"),
    "baloo_file_extractor": ("индексация файлов KDE (Baloo)", "KDE file indexing (Baloo)"),
    "tracker-miner-f": ("индексация файлов GNOME (Tracker)", "GNOME file indexing (Tracker)"),
    "localsearch-3": ("индексация файлов GNOME", "GNOME file indexing"),
    "updatedb": ("обновление базы locate", "the locate database being updated"),
    "btrfs": ("обслуживание btrfs (scrub/balance)", "btrfs maintenance (scrub/balance)"),
    "fstrim": ("TRIM диска", "disk TRIM"),
    "flatpak": ("обновление Flatpak", "a Flatpak update"),
    "snapd": ("служба Snap", "the Snap service"),
    "fwupd": ("проверка прошивок", "firmware checks"),
    "ollama": ("языковая модель (Ollama)", "a language model (Ollama)"),
    "ollama_llama_se": ("языковая модель (Ollama)", "a language model (Ollama)"),
    "llama-server": ("языковая модель (Ollama)", "a language model (Ollama)"),
}


# compositors and display servers: they hold the GPU that drives the screen
DESKTOPS = {"hyprland", "kwin_wayland", "kwin_x11", "xorg", "x", "gnome-shell", "mutter", "sway", "niri", "labwc",
            "wayfire", "river", "cosmic-comp", "xwayland", "plasmashell"}


@dataclass
class Sample:
    groups: list[ProcGroup]
    cpus: int
    mem: dict[str, int]
    psi: dict[str, float] = field(default_factory=dict)  # "cpu", "memory", "memory_full", "io", "io_full": avg10 %
    temps: list[tuple[str, float]] = field(default_factory=list)
    freq: tuple[float, float] = (0.0, 0.0)  # average current MHz, max MHz
    profile: str = ""  # power-saver / balanced / performance
    battery: dict = field(default_factory=dict)  # status, capacity, watts
    dgpu: str = ""  # "active" / "suspended" / "" (no hybrid NVIDIA)
    dgpu_users: list[str] = field(default_factory=list)
    root_used: float = 0.0  # percent
    gpu_temp: float = 0.0


@dataclass
class Finding:
    level: str  # bad / warn / info / ok
    text: str
    hint: str = ""
    fix: list[str] = field(default_factory=list)  # a command that helps, offered with confirmation
    kill: str = ""  # a process group worth closing


# --- measuring ---------------------------------------------------------------------------------


def _read(path: Path | str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def pressure(root: Path = Path("/proc/pressure")) -> dict[str, float]:
    """Pressure stall information: the share of the last 10 s that tasks waited for CPU, memory, disk."""
    res = {}
    for res_name in ("cpu", "memory", "io"):
        for line in _read(root / res_name).splitlines():
            m = re.match(r"(some|full) avg10=([\d.]+)", line)
            if m:
                res[res_name if m.group(1) == "some" else f"{res_name}_full"] = float(m.group(2))
    return res


def frequency(root: Path = Path("/sys/devices/system/cpu")) -> tuple[float, float]:
    cur, top = [], []
    for c in root.glob("cpu[0-9]*/cpufreq"):
        a, b = _read(c / "scaling_cur_freq"), _read(c / "cpuinfo_max_freq")
        if a.isdigit() and b.isdigit():
            cur.append(int(a) / 1000)
            top.append(int(b) / 1000)
    return (sum(cur) / len(cur), max(top)) if cur else (0.0, 0.0)


def power_profile() -> str:
    if shutil.which("powerprofilesctl"):
        try:
            return subprocess.run(["powerprofilesctl", "get"], capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"low-power": "power-saver", "quiet": "power-saver"}.get(
        _read("/sys/firmware/acpi/platform_profile"), _read("/sys/firmware/acpi/platform_profile"))


def battery_state() -> dict:
    for b in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        power = _read(b / "power_now")
        if not power.isdigit():  # some report current and voltage instead
            cur, volt = _read(b / "current_now"), _read(b / "voltage_now")
            power = str(int(cur) * int(volt) // 10**6) if cur.isdigit() and volt.isdigit() else "0"
        return {"status": _read(b / "status"), "capacity": _read(b / "capacity"), "watts": int(power) / 1e6}
    return {}


def nvidia_dgpu() -> str:
    """Runtime power state of an NVIDIA GPU on a hybrid laptop ('' when there's none)."""
    for dev in Path("/sys/bus/pci/devices").glob("*"):
        if _read(dev / "vendor") == "0x10de" and _read(dev / "class").startswith("0x03"):
            return _read(dev / "power" / "runtime_status") or "active"
    return ""


def dgpu_users() -> list[str]:
    """Own processes holding /dev/nvidia* open — what keeps the discrete GPU awake."""
    names = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            fds = list((d / "fd").iterdir())
            if any(os.readlink(f).startswith("/dev/nvidia") and os.readlink(f)[11:12].isdigit() for f in fds):
                names.append(_read(d / "comm"))
        except OSError:
            continue
    return sorted(set(n for n in names if n))


def nvidia_temp() -> float:
    if not shutil.which("nvidia-smi"):
        return 0.0
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.split()
        return float(out[0]) if out else 0.0
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return 0.0


def measure(focus: str = "") -> Sample:
    groups = processes(1.0)
    st = os.statvfs("/")
    dgpu = nvidia_dgpu()
    return Sample(groups, os.cpu_count() or 1, meminfo(), pressure(), temperatures(), frequency(), power_profile(),
                  battery_state(), dgpu, dgpu_users() if dgpu == "active" else [],
                  100 * (1 - st.f_bavail / st.f_blocks) if st.f_blocks else 0.0,
                  nvidia_temp() if dgpu == "active" else 0.0)  # nvidia-smi would wake a sleeping GPU


# --- rules -------------------------------------------------------------------------------------


def _gb(n: int) -> str:
    return f"{n / 1024**3:.1f} " + t("ГБ", "GB")


def diagnose(s: Sample, focus: str = "") -> list[Finding]:
    """focus: "" (slow), "heat" or "battery"."""
    f: list[Finding] = []
    by_cpu = sorted(s.groups, key=lambda g: -g.cpu)
    total_cpu = sum(g.cpu for g in s.groups) / s.cpus
    top = by_cpu[0] if by_cpu else None
    on_battery = s.battery.get("status") == "Discharging"

    busy, busy_cpu = [], 0.0  # temporary background work
    for g in s.groups:
        what = BACKGROUND.get(g.name)
        if what and (g.cpu >= 15 or g.name in ("pacman", "makepkg", "apt", "dnf", "dkms")) and t(*what) not in busy:
            busy.append(t(*what))
            busy_cpu = max(busy_cpu, g.cpu)

    # processor
    if top and top.cpu >= 85 and top.name not in BACKGROUND:
        f.append(Finding("warn" if total_cpu < 80 else "bad",
                         t(f"{top.name} грузит процессор: {top.cpu:.0f}% одного ядра",
                           f"{top.name} keeps the CPU busy: {top.cpu:.0f}% of one core"),
                         t("если программа сейчас ничего не делает — она зависла или занята в фоне; можно закрыть",
                           "if it isn't doing anything for you right now, it's stuck or busy in the background; it can be closed"),
                         kill=top.name))
    elif total_cpu >= 80:
        names = ", ".join(f"{g.name} {g.cpu:.0f}%" for g in by_cpu[:3])
        f.append(Finding("bad", t(f"процессор загружен на {total_cpu:.0f}%: {names}", f"the CPU is {total_cpu:.0f}% busy: {names}")))
    if s.psi.get("cpu", 0) >= 40 and total_cpu < 80:
        f.append(Finding("warn", t(f"программы ждут процессор {s.psi['cpu']:.0f}% времени",
                                   f"programs wait for the CPU {s.psi['cpu']:.0f}% of the time")))

    # memory
    total, avail = s.mem.get("MemTotal", 0), s.mem.get("MemAvailable", 0)
    swap = s.mem.get("SwapTotal", 0) - s.mem.get("SwapFree", 0)
    by_mem = sorted(s.groups, key=lambda g: -g.rss)
    heavy = ", ".join(f"{g.name} {_gb(g.rss)}" for g in by_mem[:3])
    if total and avail / total < 0.1:
        f.append(Finding("bad", t(f"память почти кончилась: свободно {_gb(avail)} из {_gb(total)}",
                                  f"memory is almost gone: {_gb(avail)} of {_gb(total)} free"),
                         t(f"больше всех занимают: {heavy}", f"the biggest users: {heavy}"), kill=by_mem[0].name if by_mem else ""))
    elif s.psi.get("memory", 0) >= 10 or (swap > 1024**3 and s.psi.get("memory", 0) >= 3):
        f.append(Finding("warn", t(f"не хватает памяти: система перекладывает данные в swap ({_gb(swap)})",
                                   f"short on memory: the system moves data to swap ({_gb(swap)})"),
                         t(f"больше всех занимают: {heavy}", f"the biggest users: {heavy}")))

    # disk
    if s.psi.get("io_full", 0) >= 10 or s.psi.get("io", 0) >= 30:
        f.append(Finding("warn", t(f"диск не успевает: программы ждут его {s.psi.get('io', 0):.0f}% времени",
                                   f"the disk can't keep up: programs wait for it {s.psi.get('io', 0):.0f}% of the time"),
                         t("так бывает при обновлении, индексации или копировании больших файлов",
                           "it happens during updates, indexing or copying big files")))
    if s.root_used >= 95:
        f.append(Finding("bad", t(f"системный диск заполнен на {s.root_used:.0f}%", f"the system disk is {s.root_used:.0f}% full"),
                         t("при почти полном диске всё начинает тормозить; спроси «что занимает место»",
                           "a nearly full disk slows everything down; ask “what takes up space”")))

    # heat
    cpu_temp = max((v for k, v in s.temps if k == "CPU"), default=0.0)
    cur, top_mhz = s.freq
    throttled = top_mhz and cur < 0.5 * top_mhz and total_cpu >= 50
    if cpu_temp >= 90 or (cpu_temp >= 85 and throttled):
        f.append(Finding("bad", t(f"процессор перегрет: {cpu_temp:.0f}°C", f"the CPU is overheating: {cpu_temp:.0f}°C") +
                         (t(f", частота сброшена до {cur:.0f} из {top_mhz:.0f} МГц", f", clock dropped to {cur:.0f} of {top_mhz:.0f} MHz")
                          if throttled else ""),
                         t("проверь, не забиты ли пылью решётки, и не стоит ли ноутбук на мягком",
                           "check that the vents aren't clogged with dust and the laptop isn't on something soft")))
    elif cpu_temp >= 80 or (focus == "heat" and cpu_temp >= 70):
        f.append(Finding("warn", t(f"процессор горячий: {cpu_temp:.0f}°C", f"the CPU is hot: {cpu_temp:.0f}°C") +
                         (t(f" — его греет {top.name}", f" — {top.name} heats it") if top and top.cpu >= 50 else "")))
    if s.gpu_temp >= 85:
        f.append(Finding("warn", t(f"видеокарта горячая: {s.gpu_temp:.0f}°C", f"the GPU is hot: {s.gpu_temp:.0f}°C")))

    # power
    if s.profile == "power-saver" and focus != "battery":
        f.append(Finding("warn", t("включён режим энергосбережения — компьютер специально работает медленнее",
                                   "power saver mode is on — the computer is slowed down on purpose"),
                         fix=["powerprofilesctl", "set", "balanced"] if shutil.which("powerprofilesctl") else []))
    if s.dgpu == "active" and (on_battery or focus == "battery"):
        desktop = [u for u in s.dgpu_users if u.lower() in DESKTOPS]
        apps = [u for u in s.dgpu_users if u not in desktop]
        who = ", ".join((apps or desktop)[:4])
        f.append(Finding("warn", t("дискретная видеокарта NVIDIA не спит и тратит батарею", "the NVIDIA GPU is awake and drains the battery") +
                         (t(f": её держат {who}", f": held by {who}") if who else ""),
                         t("закрой эти программы или запускай их только от сети", "close these programs or run them only on AC power")
                         if apps else
                         t("её держит сам рабочий стол: так бывает с внешним монитором, подключённым к NVIDIA, или в режиме "
                           "«только дискретная» в BIOS; иначе укажи рабочему столу встроенную видеокарту "
                           "(в Hyprland — переменная AQ_DRM_DEVICES)",
                           "the desktop itself holds it: that happens with an external monitor wired to the NVIDIA GPU or "
                           "the BIOS set to “discrete only”; otherwise point the desktop at the integrated GPU "
                           "(in Hyprland, the AQ_DRM_DEVICES variable)") if desktop else ""))
    if focus == "battery":
        w = s.battery.get("watts", 0)
        if on_battery and w >= 20:
            f.append(Finding("warn", t(f"расход {w:.0f} Вт — для ноутбука это много", f"drawing {w:.0f} W — a lot for a laptop"),
                             t(f"больше всех работает: {', '.join(g.name for g in by_cpu[:3])}",
                               f"busiest: {', '.join(g.name for g in by_cpu[:3])}")))
        elif on_battery and w:
            f.append(Finding("ok", t(f"расход {w:.1f} Вт — это нормально", f"drawing {w:.1f} W — that's normal")))
        elif not s.battery:
            f.append(Finding("info", t("батареи нет — это не ноутбук", "there's no battery — not a laptop")))
        else:
            f.append(Finding("info", t("сейчас ноутбук от сети: расход можно оценить только от батареи",
                                       "on AC power now: the drain can only be measured on battery")))
        if s.profile in ("balanced", "performance") and on_battery:
            f.append(Finding("info", t("режим питания: " + s.profile + " — «энергосбережение» продлит работу",
                                       "power mode: " + s.profile + " — “power saver” lasts longer"),
                             fix=["powerprofilesctl", "set", "power-saver"] if shutil.which("powerprofilesctl") else []))

    if busy:
        f.append(Finding("warn" if busy_cpu >= 100 else "info", t("сейчас идёт: ", "running now: ") + ", ".join(busy),
                         t("это временно — после окончания станет легче", "it's temporary — it gets lighter when it's done")))
    if not any(x.level in ("bad", "warn") for x in f):
        f.insert(0, Finding("ok", t("сейчас всё спокойно", "everything is calm right now"),
                            {"": t("если тормозит время от времени — спроси меня в тот момент, когда тормозит",
                                   "if it slows down now and then — ask me at the moment it happens"),
                             "heat": t(f"процессор {cpu_temp:.0f}°C; если греется под нагрузкой — спроси меня в тот момент",
                                       f"the CPU is at {cpu_temp:.0f}°C; if it heats up under load, ask me at that moment")
                             if cpu_temp else ""}.get(focus, "")))
    return f
