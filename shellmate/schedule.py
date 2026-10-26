"""Tasks on a schedule: reminders, cleaning a folder of old files, emptying the trash, turning the
computer off later, or the user's own command. They are systemd user timers (no root): repeating ones
as unit files in ~/.config/systemd/user, one-off ones as transient timers (systemd-run).
The model only fills SCHEDULE_SCHEMA; the calendar spec and the command are built here."""

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from shellmate.i18n import plural, t

PREFIX = "shellmate-"
TASKS = ["reminder", "clean_folder", "empty_trash", "poweroff", "command"]
REPEATS = ["once", "hourly", "daily", "weekly", "monthly", "login"]
WEEKDAYS = ["", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS = ["", "today", "tomorrow"]

SCHEDULE_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "enum": TASKS},
        "repeat": {"type": "string", "enum": REPEATS},
        "time": {"type": "string"},
        "day": {"type": "string", "enum": DAYS},
        "weekday": {"type": "string", "enum": WEEKDAYS},
        "in_minutes": {"type": "integer"},
        "message": {"type": "string"},
        "folder": {"type": "string"},
        "days": {"type": "integer"},
        "command": {"type": "string"},
    },
    "required": ["task", "repeat", "time", "day", "weekday", "in_minutes", "message", "folder", "days", "command"],
}

SCHEDULE_SYSTEM = """You read a request to do something later or regularly and fill JSON. Never run anything.

task:
- reminder: show a reminder / notification ("напомни…", "remind me…").
- clean_folder: delete (move to trash) old files from a folder.
- empty_trash: empty the trash.
- poweroff: turn the computer off.
- command: run a shell command the user wrote out.

repeat: once (one time: "через 20 минут", "в 18:00", "завтра"), hourly, daily ("каждый день", "по утрам"),
weekly ("каждую неделю", "по пятницам"), monthly, login ("при входе", "при включении").
time: "HH:MM" in 24 hours ("в 9 вечера" -> "21:00", "в полночь" -> "00:00", "утром" -> "09:00"), "" if not said.
day: "tomorrow" for "завтра", "today" for "сегодня", else "".
weekday: mon..sun when a day of the week is named, else "".
in_minutes: for "через N минут/часов" the delay in minutes (1 hour = 60), else 0.
message: for a reminder, what to remind about, copied from the request, else "".
folder: for clean_folder the folder as a path (загрузки -> ~/Downloads, рабочий стол -> ~/Desktop), else "".
days: for clean_folder or empty_trash "older than N days" (неделя = 7, месяц = 30), else 0.
command: for a command task the command copied verbatim, else ""."""

EXAMPLES = [
    ("напомни через полчаса позвонить маме", {"task": "reminder", "repeat": "once", "time": "", "day": "", "weekday": "",
                                             "in_minutes": 30, "message": "позвонить маме", "folder": "", "days": 0, "command": ""}),
    ("каждую пятницу в 18:00 напоминай сдать отчёт", {"task": "reminder", "repeat": "weekly", "time": "18:00", "day": "",
                                                       "weekday": "fri", "in_minutes": 0, "message": "сдать отчёт",
                                                       "folder": "", "days": 0, "command": ""}),
    ("раз в неделю удаляй из загрузок файлы старше месяца", {"task": "clean_folder", "repeat": "weekly", "time": "", "day": "",
                                                              "weekday": "", "in_minutes": 0, "message": "",
                                                              "folder": "~/Downloads", "days": 30, "command": ""}),
    ("выключи компьютер в 23:30", {"task": "poweroff", "repeat": "once", "time": "23:30", "day": "", "weekday": "",
                                    "in_minutes": 0, "message": "", "folder": "", "days": 0, "command": ""}),
    ("каждый день в 3 ночи запускай ~/bin/sync.sh", {"task": "command", "repeat": "daily", "time": "03:00", "day": "",
                                                     "weekday": "", "in_minutes": 0, "message": "", "folder": "", "days": 0,
                                                     "command": "~/bin/sync.sh"}),
]

WEEKDAY_NAMES = {"mon": ("понедельникам", "Mondays"), "tue": ("вторникам", "Tuesdays"), "wed": ("средам", "Wednesdays"),
                 "thu": ("четвергам", "Thursdays"), "fri": ("пятницам", "Fridays"), "sat": ("субботам", "Saturdays"),
                 "sun": ("воскресеньям", "Sundays")}
FOLDERS = {"загрузк": "~/Downloads", "download": "~/Downloads", "рабоч": "~/Desktop", "desktop": "~/Desktop",
           "документ": "~/Documents", "document": "~/Documents", "картин": "~/Pictures", "фото": "~/Pictures",
           "скриншот": "~/Pictures/Screenshots", "screenshot": "~/Pictures/Screenshots", "видео": "~/Videos",
           "video": "~/Videos", "музык": "~/Music", "music": "~/Music", "tmp": "/tmp"}


@dataclass
class Spec:
    task: str
    repeat: str = "once"
    time: str = ""
    day: str = ""
    weekday: str = ""
    in_minutes: int = 0
    message: str = ""
    folder: str = ""
    days: int = 0
    command: str = ""

    @classmethod
    def from_json(cls, d: dict) -> "Spec":
        def num(k: str) -> int:
            try:
                return max(0, int(d.get(k) or 0))
            except (TypeError, ValueError):
                return 0
        pick = lambda k, allowed, default="": d.get(k) if d.get(k) in allowed else default  # noqa: E731
        return cls(pick("task", TASKS, "reminder"), pick("repeat", REPEATS, "once"), str(d.get("time") or "").strip(),
                   pick("day", DAYS), pick("weekday", WEEKDAYS), num("in_minutes"), str(d.get("message") or "").strip(),
                   str(d.get("folder") or "").strip(), num("days"), str(d.get("command") or "").strip())


@dataclass
class When:
    calendar: str = ""  # OnCalendar=
    after: int = 0  # seconds from now (one-off)
    login: bool = False
    label: str = ""

    @property
    def once(self) -> bool:
        return bool(self.after) or bool(re.match(r"\d{4}-", self.calendar))


@dataclass
class Task:
    unit: str  # the .timer
    description: str
    next: str
    transient: bool


def clock(text: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", text.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def when(spec: Spec, now: datetime | None = None, default_time: str = "10:00") -> When:
    """The timer's schedule; ValueError when the request has no usable time."""
    now = now or datetime.now()
    hm = clock(spec.time) if spec.time else None
    if spec.time and not hm:
        raise ValueError(t(f"не понял время «{spec.time}»", f"can't read the time “{spec.time}”"))
    hh, mm = hm or clock(default_time) or (10, 0)
    at = f"{hh:02d}:{mm:02d}"
    if spec.repeat == "once":
        if spec.in_minutes:
            at = (now + timedelta(minutes=spec.in_minutes)).strftime("%H:%M")
            return When(after=spec.in_minutes * 60, label=t(f"через {_duration(spec.in_minutes)} (в {at})",
                                                            f"in {_duration(spec.in_minutes)} (at {at})"))
        if not hm and not spec.weekday and spec.day != "tomorrow":
            raise ValueError(t("когда? Например: «в 18:00» или «через 20 минут»", "when? For example: “at 18:00” or “in 20 minutes”"))
        day = now.date()
        if spec.day == "tomorrow":
            day += timedelta(days=1)
        elif spec.weekday:
            ahead = (WEEKDAYS.index(spec.weekday) - 1 - now.weekday()) % 7
            day += timedelta(days=ahead or (7 if (hh, mm) <= (now.hour, now.minute) else 0))
        elif (hh, mm) <= (now.hour, now.minute):
            day += timedelta(days=1)  # "в 9:00" said at 10:00 means tomorrow
        return When(f"{day.isoformat()} {at}:00",
                    label=(t("сегодня", "today") if day == now.date() else t("завтра", "tomorrow") if day == now.date() + timedelta(days=1)
                           else day.strftime("%d.%m")) + t(f" в {at}", f" at {at}"))
    if spec.repeat == "hourly":
        return When("hourly", label=t("каждый час", "every hour"))
    if spec.repeat == "login":
        return When(login=True, label=t("при каждом входе в систему", "at every login"))
    if spec.repeat == "weekly":
        wd = spec.weekday or "mon"
        return When(f"{wd.capitalize()} *-*-* {at}:00", label=t(f"по {WEEKDAY_NAMES[wd][0]} в {at}", f"on {WEEKDAY_NAMES[wd][1]} at {at}"))
    if spec.repeat == "monthly":
        return When(f"*-*-01 {at}:00", label=t(f"1-го числа каждого месяца в {at}", f"on the 1st of every month at {at}"))
    return When(f"*-*-* {at}:00", label=t(f"каждый день в {at}", f"every day at {at}"))


def _duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    parts = ([plural(h, ("час", "часа", "часов"), ("hour", "hours"))] if h else []) + \
            ([plural(m, ("минуту", "минуты", "минут"), ("minute", "minutes"))] if m else [])
    return " ".join(parts)


def next_run(calendar: str) -> str:
    """systemd's own reading of the spec ('' when systemd-analyze is missing or rejects it)."""
    if not shutil.which("systemd-analyze"):
        return ""
    r = subprocess.run(["systemd-analyze", "calendar", calendar], capture_output=True, text=True)
    m = re.search(r"Next elapse:\s*(.+)", r.stdout)
    return m.group(1).strip() if r.returncode == 0 and m else ""


def safe_folder(folder: str, home: Path | None = None) -> Path:
    """A folder that may be cleaned: inside home, not home itself, not a hidden (settings) folder."""
    home = home or Path.home()
    text = folder.strip().strip("\"'«»")
    for word, path in FOLDERS.items():
        if text.lower().startswith(word):
            text = path
            break
    p = Path(text.replace("~", str(home), 1) if text.startswith("~") else text)
    if not p.is_absolute():
        p = home / p
    p = Path(str(p.resolve()))
    if p == Path("/tmp"):
        return p
    if p == home or home not in p.parents:
        raise ValueError(t(f"чистить по расписанию можно только папки внутри домашней, а не {p}",
                           f"only folders inside your home can be cleaned on a schedule, not {p}"))
    if any(part.startswith(".") for part in p.relative_to(home).parts):
        raise ValueError(t(f"{p} — папка настроек программ, её я чистить не буду", f"{p} holds program settings; I won't clean it"))
    if not p.is_dir():
        raise ValueError(t(f"папки {p} нет", f"there's no folder {p}"))
    return p


def action(spec: Spec, name: str, home: Path | None = None) -> tuple[list[str], str]:
    """(argv to run, what it does in words). ValueError when it can't be done safely."""
    home = home or Path.home()
    if spec.task == "reminder":
        if not spec.message:
            raise ValueError(t("о чём напомнить?", "remind about what?"))
        if not shutil.which("notify-send"):
            raise ValueError(t("нет notify-send — поставь пакет libnotify", "no notify-send — install libnotify"))
        return (["notify-send", "-u", "critical", "-a", name, t("Напоминание", "Reminder"), spec.message],
                t(f"напомнить: «{spec.message}»", f"remind: “{spec.message}”"))
    if spec.task == "clean_folder":
        folder = safe_folder(spec.folder or "~/Downloads", home)
        days = spec.days or 30
        return (["find", str(folder), "-mindepth", "1", "-maxdepth", "1", "-mtime", f"+{days}",
                 "-exec", "gio", "trash", "--", "{}", "+"],
                t(f"убрать в корзину файлы старше {days} дн. из {folder}", f"move files older than {days} days from {folder} to the trash"))
    if spec.task == "empty_trash":
        if spec.days and shutil.which("trash-empty"):
            return ["trash-empty", "-f", str(spec.days)], t(f"очистить корзину от файлов старше {spec.days} дн.",
                                                            f"empty files older than {spec.days} days from the trash")
        return ["gio", "trash", "--empty"], t("очистить корзину", "empty the trash")
    if spec.task == "poweroff":
        return ["systemctl", "poweroff"], t("выключить компьютер", "turn the computer off")
    if spec.task == "command":
        return command_argv(spec.command, home), t(f"запустить: {spec.command}", f"run: {spec.command}")
    raise ValueError(t("не понял, что делать", "didn't understand what to do"))


def command_argv(text: str, home: Path) -> list[str]:
    from shellmate import cmdcheck  # local: cmdcheck pulls in the library
    if not text:
        raise ValueError(t("какую команду запускать?", "which command to run?"))
    parsed = cmdcheck.parse(text)
    if parsed.error:
        raise ValueError(t(f"не могу разобрать команду: {parsed.error}", f"can't parse the command: {parsed.error}"))
    if cmdcheck.effects(parsed).root:
        raise ValueError(t("команды с sudo по расписанию не запускаю: пароль некому будет ввести",
                           "I don't schedule sudo commands: nobody will be there to type the password"))
    danger = [f.text for f in cmdcheck.risks(parsed, text) if f.level == "danger"]
    if danger:
        raise ValueError(t("опасная команда: ", "a dangerous command: ") + danger[0])
    return ["/bin/sh", "-c", text.replace("~/", f"{home}/") if text.startswith("~/") else text]


def unit_name(task: str, now: datetime | None = None) -> str:
    return f"{PREFIX}{task.replace('_', '-')}-{(now or datetime.now()):%Y%m%d-%H%M%S}"


def _exec_arg(a: str) -> str:
    a = a.replace("%", "%%").replace("$", "$$")
    if re.fullmatch(r"[\w@%+=:,./{}-]+", a):
        return a
    return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_files(name: str, description: str, argv: list[str], w: When) -> dict[str, str]:
    """{file name: contents} of a repeating timer and its service."""
    description = description.replace("%", "%%")  # systemd expands %-specifiers here too
    service = ("[Unit]\n" f"Description={description}\n\n" "[Service]\n" "Type=oneshot\n"
               f"ExecStart={' '.join(_exec_arg(a) for a in argv)}\n")
    trigger = "OnStartupSec=1min\n" if w.login else f"OnCalendar={w.calendar}\nPersistent=true\n"
    timer = ("[Unit]\n" f"Description={description}\n\n" "[Timer]\n" f"{trigger}"
             f"AccuracySec={'1s' if argv[:1] == ['notify-send'] else '1min'}\n\n" "[Install]\n" "WantedBy=timers.target\n")
    return {f"{name}.service": service, f"{name}.timer": timer}


def transient(name: str, description: str, argv: list[str], w: When) -> list[str]:
    """A one-off timer: systemd-run keeps it until it fires (or until the computer is turned off)."""
    trigger = [f"--on-active={w.after}s"] if w.after else [f"--on-calendar={w.calendar}"]
    return ["systemd-run", "--user", f"--unit={name}", f"--description={description.replace('%', '%%')}", *trigger,
            "--timer-property=AccuracySec=1s", "--", *argv]


def user_dir() -> Path:
    from os import environ
    return Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def _systemctl(*args: str) -> str:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def tasks() -> list[Task]:
    names = [line.split()[0] for line in _systemctl("list-units", "--all", "--plain", "--no-legend", f"{PREFIX}*.timer").splitlines()
             if line.strip()]
    on_disk = {p.name for p in user_dir().glob(f"{PREFIX}*.timer")} if user_dir().is_dir() else set()
    res = []
    for n in dict.fromkeys(names + sorted(on_disk)):
        props = dict(line.partition("=")[::2] for line in
                     _systemctl("show", n, "-p", "Description", "-p", "NextElapseUSecRealtime").splitlines())
        res.append(Task(n, props.get("Description", "").removeprefix("shellmate: "), props.get("NextElapseUSecRealtime", ""),
                        n not in on_disk))
    return res


def remove_commands(unit: str, transient_unit: bool, files: list[str] | None = None) -> list[list[str]]:
    if transient_unit:
        return [["systemctl", "--user", "stop", unit]]
    base = unit.removesuffix(".timer")
    paths = files or [str(user_dir() / f"{base}.timer"), str(user_dir() / f"{base}.service")]
    return [["systemctl", "--user", "disable", "--now", unit], ["rm", "-f", *paths], ["systemctl", "--user", "daemon-reload"]]
