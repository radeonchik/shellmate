"""What broke after an update: the last update session, the errors that appeared after it, and
which updated packages they point to. Everything here is code reading logs; nothing is guessed by
the model."""

import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path

from shellmate import system
from shellmate.i18n import t
from shellmate.pkglog import Change, Txn

SESSION_GAP = timedelta(hours=2)  # paru/yay run the repos and the AUR as separate transactions

# subsystem: words in a request, package patterns, journal sources / message words that belong to it
SUBSYSTEMS = {
    "sound": (("звук", "sound", "audio", "аудио", "микрофон", "mic", "наушник", "колонк", "pipewire"),
              ("pipewire*", "wireplumber*", "alsa*", "sof-firmware", "pulseaudio*", "lib32-pipewire*", "linux", "linux-*"),
              ("pipewire", "wireplumber", "pulseaudio", "snd_", "sof-", "alsa")),
    "graphics": (("видео", "экран", "монитор", "графи", "graphics", "display", "screen", "gpu", "видеокарт", "nvidia",
                  "amd", "мерца", "flicker", "black screen", "чёрный", "черный", "игр", "game"),
                 ("mesa*", "lib32-mesa*", "nvidia*", "lib32-nvidia*", "vulkan-*", "lib32-vulkan-*", "xorg-*", "wayland*",
                  "linux", "linux-*", "kwin*", "mutter*", "hyprland*", "gamescope", "egl-*", "libdrm*"),
                 ("nvidia", "nvrm", "amdgpu", "radeon", "i915", "xe ", "drm", "kwin", "mutter", "hyprland", "xwayland", "gnome-shell")),
    "network": (("интернет", "сеть", "wifi", "wi-fi", "вайфай", "network", "internet", "ethernet", "dns", "роутер"),
                ("networkmanager*", "iwd", "wpa_supplicant", "linux-firmware*", "linux", "linux-*", "dhcpcd", "systemd-resolvconf",
                 "network-manager*", "openresolv"),
                ("networkmanager", "iwd", "wpa_supplicant", "iwlwifi", "ath1", "rtw", "r8169", "dhcp", "resolved")),
    "bluetooth": (("bluetooth", "блютуз", "блютус"),
                  ("bluez*", "linux-firmware*", "linux", "linux-*"),
                  ("bluetooth", "bluez", "btusb", "hci")),
    "boot": (("загруз", "вход", "login", "boot", "sddm", "gdm", "логин", "не запускается"),
             ("sddm*", "gdm*", "lightdm*", "systemd*", "grub*", "linux", "linux-*", "mkinitcpio*", "dracut*", "plymouth*",
              "limine*", "refind*", "systemd-boot*"),
             ("sddm", "gdm", "lightdm", "systemd-logind", "grub", "mkinitcpio", "plymouth")),
    "desktop": (("рабочий стол", "plasma", "kde", "gnome", "hyprland", "панел", "desktop", "окна", "windows"),
                ("plasma*", "kwin*", "kde-*", "gnome-*", "mutter*", "hyprland*", "waybar*", "qt6-*", "qt5-*", "gtk*", "xdg-desktop-portal*"),
                ("plasmashell", "kwin", "gnome-shell", "hyprland", "waybar", "xdg-desktop-portal")),
}
# shown first in the summary: what usually matters when something breaks
IMPORTANT = ("linux", "linux-*", "nvidia*", "mesa", "systemd", "glibc", "pipewire", "wireplumber", "plasma-desktop",
             "plasma-workspace", "kwin", "gnome-shell", "mutter", "hyprland", "sddm", "gdm", "networkmanager",
             "network-manager", "bluez", "linux-firmware", "xorg-server", "xwayland", "grub", "mkinitcpio", "dracut",
             "linux-image-*", "kernel", "kernel-core")


def subsystem_of(text: str) -> str:
    low = text.lower()
    return next((name for name, (words, _, _) in SUBSYSTEMS.items() if any(w in low for w in words)), "")


def sessions(txns: list[Txn], gap: timedelta = SESSION_GAP) -> list[Txn]:
    """Transactions close in time merged into one update session (a later change of a package wins)."""
    out: list[Txn] = []
    for tx in txns:
        if out and tx.time - out[-1].time < gap:
            merged = {c.name: c for c in out[-1].changes}
            for c in tx.changes:
                prev = merged.get(c.name)
                merged[c.name] = Change(c.name, prev.old if prev and prev.old else c.old, c.new, c.action)
            out[-1] = Txn(out[-1].time, list(merged.values()))
        else:
            out.append(Txn(tx.time, list(tx.changes)))
    return out


def last_update(txns: list[Txn]) -> Txn | None:
    """The last session that upgraded something (a single fresh install isn't an "update")."""
    return next((s for s in reversed(sessions(txns)) if s.upgrades), None)


def matches(name: str, patterns) -> bool:
    return any(fnmatch(name, p) for p in patterns)


def important(changes: list[Change]) -> list[Change]:
    """Kernels, drivers, the desktop and the like first (the order of IMPORTANT); headers last."""
    def rank(c: Change) -> tuple[bool, int]:
        return bool(NOT_SUSPECT.search(c.name)), next((i for i, p in enumerate(IMPORTANT) if fnmatch(c.name, p)), len(IMPORTANT))
    return sorted(changes, key=rank)


@dataclass
class Suspect:
    error: system.LogError
    changes: list[Change] = field(default_factory=list)  # updated packages it points to
    strong: bool = False  # the program that logged it belongs to an updated package


SIGN_PACKAGES = {
    "nvrm": ("nvidia-utils", "nvidia*", "*-nvidia*", "lib32-nvidia*"), "nvidia": ("nvidia-utils", "nvidia*", "*-nvidia*"),
    "amdgpu": ("mesa", "linux-firmware*", "vulkan-radeon"), "radeon": ("mesa", "linux-firmware*"),
    "i915": ("mesa", "vulkan-intel", "linux-firmware*"), "iwlwifi": ("linux-firmware*",), "btusb": ("linux-firmware*", "bluez*"),
    "firmware": ("linux-firmware*",),
}
NOT_SUSPECT = re.compile(r"-(?:headers|docs?|dbg|debug)$")
# a kernel oops logs its stack trace line by line: "? do_syscall_64+0xe4/0x3e0" is not an error of its own
TRACE = re.compile(r"^\?\s|\+0x[0-9a-f]+/0x[0-9a-f]+")


def new_errors(before: list[system.LogError], after: list[system.LogError]) -> list[system.LogError]:
    seen = {system.error_key(e.source, e.message) for e in before}
    return [e for e in after if system.error_key(e.source, e.message) not in seen]


def suspects(errors: list[system.LogError], changes: list[Change], owner=None) -> list[Suspect]:
    """For each error, the updated packages it most likely comes from: the package owning the program
    that logged it, else the packages of the subsystem its source or message belongs to."""
    by_name = {c.name: c for c in changes}
    out = []
    for e in errors:
        s = Suspect(e)
        path = shutil.which(e.source) if e.source not in ("kernel", "?") else None
        pkg = owner(path) if owner and path else None
        if pkg and pkg in by_name:
            s.changes, s.strong = [by_name[pkg]], True
        else:
            text = f"{e.source} {e.message}".lower()
            for sign, pkgs in SIGN_PACKAGES.items():  # a driver named in the message points at its package
                if sign in text:
                    s.changes += [c for c in changes if matches(c.name, pkgs) and c not in s.changes and not NOT_SUSPECT.search(c.name)]
            for _, (_, pkgs, signs) in SUBSYSTEMS.items():
                if any(sig in text for sig in signs):
                    # the subsystem's own packages before the kernel: bluez before linux for a bluetooth error
                    hits = [c for c in changes if matches(c.name, pkgs) and c not in s.changes and not NOT_SUSPECT.search(c.name)]
                    s.changes += sorted(hits, key=lambda c: next(i for i, p in enumerate(pkgs) if fnmatch(c.name, p)))
        out.append(s)
    return out


def reboot_notes(update: Txn, boots: list[tuple[datetime, datetime]], running: str | None = None,
                 modules: Path = Path("/usr/lib/modules")) -> list[str]:
    """What only a reboot fixes: the running kernel's modules removed by the update, the NVIDIA module
    older than its user-space driver, or no reboot since kernel/driver packages changed."""
    notes = []
    running = running or os.uname().release
    if modules.is_dir() and not (modules / running).exists():
        notes.append(t(f"работает ядро {running}, а его модули обновление уже удалило — USB, звук или Wi-Fi могут "
                       "отвалиться, пока не перезагрузишься",
                       f"the running kernel {running} lost its modules in the update — USB, sound or Wi-Fi may fail "
                       "until you reboot"))
    loaded = _read("/sys/module/nvidia/version")
    utils = next((c.new for c in update.changes if c.name in ("nvidia-utils", "nvidia-driver", "xorg-x11-drv-nvidia")), "")
    if loaded and utils and not utils.startswith(loaded):
        notes.append(t(f"загружен драйвер NVIDIA {loaded}, а установлен {utils} — нужна перезагрузка",
                       f"NVIDIA driver {loaded} is loaded but {utils} is installed — a reboot is needed"))
    core = [c for c in update.upgrades if matches(c.name, ("linux", "linux-*", "nvidia*", "systemd", "glibc", "mesa",
                                                           "linux-image-*", "kernel*"))]
    booted = boots[-1][0] if boots else None
    if core and booted and booted < update.time and not notes:
        notes.append(t(f"после обновления ({', '.join(c.name for c in core[:4])}) компьютер не перезагружался — "
                       "часть изменений заработает только после перезагрузки",
                       f"the computer hasn't rebooted since the update ({', '.join(c.name for c in core[:4])}) — "
                       "some changes only take effect after a reboot"))
    return notes


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


@dataclass
class Report:
    update: Txn
    rebooted: bool
    journal_covers: bool  # the journal still has entries from before the update
    errors: list[Suspect]  # new since the update (or, without old entries, since boot)
    notes: list[str]


def analyze(txns: list[Txn], subsystem: str = "", owner=None) -> Report | None:
    update = last_update(txns)
    if not update:
        return None
    boots = system.boots()
    covers = bool(boots) and boots[0][0] < update.time
    since = f"@{int(update.time.timestamp())}"
    if covers:
        window = max(timedelta(days=1), min(datetime.now().astimezone() - update.time, timedelta(days=3)))
        before = system.journal_errors("--since", f"@{int((update.time - window).timestamp())}", "--until", since, limit=200)
        after = new_errors(before, system.journal_errors("--since", since, limit=200))
    else:  # the journal doesn't reach back: what's broken now, compared with nothing
        after = system.boot_errors(limit=40)
    after = [e for e in after if not TRACE.search(e.message)]
    found = suspects(after, update.changes, owner)
    if subsystem in SUBSYSTEMS:
        _, pkgs, signs = SUBSYSTEMS[subsystem]
        found = [s for s in found if any(sig in f"{s.error.source} {s.error.message}".lower() for sig in signs)]
    found.sort(key=lambda s: (not s.strong, not s.changes, -s.error.count))
    return Report(update, bool(boots) and boots[-1][0] > update.time, covers, found[:10], reboot_notes(update, boots))


def subsystem_changes(update: Txn, subsystem: str) -> list[Change]:
    """The update's packages of that subsystem, its own ones before the kernel."""
    if subsystem not in SUBSYSTEMS:
        return []
    pkgs = SUBSYSTEMS[subsystem][1]
    hits = [c for c in update.changes if matches(c.name, pkgs) and not NOT_SUSPECT.search(c.name)]
    return sorted(hits, key=lambda c: next(i for i, p in enumerate(pkgs) if fnmatch(c.name, p)))


def version_arrow(c: Change) -> str:
    if c.action == "removed":
        return t(f"{c.name} {c.old} (удалён)", f"{c.name} {c.old} (removed)")
    if not c.old:
        return f"{c.name} {c.new}" + (t(" (новый)", " (new)") if c.action == "installed" else "")
    return f"{c.name} {_short(c.old)} → {_short(c.new)}"


def _short(v: str) -> str:
    return re.sub(r"^\d+:", "", v)  # drop the epoch
