"""Terminal UI kit: colors, the waiting animation, panels, key-value tables, menus, prompts.
Everything degrades to plain text when stdout isn't a terminal or NO_COLOR is set."""

import colorsys
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import unicodedata

from shellmate.i18n import t

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_TRUECOLOR = os.environ.get("COLORTERM", "") in ("truecolor", "24bit")
SGR = re.compile(r"\x1b\[[0-9;]*m")  # colors and styles (not cursor moves or links)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;[^\x1b]*\x1b\\")

THEME = {
    "accent": (94, 200, 235),  # cyan: the agent itself, neutral panels
    "accent2": (170, 120, 255),  # violet: gradient end, highlights
    "quote": (120, 215, 190),
    "source": (140, 140, 165),
    "ok": (110, 210, 130),
    "warn": (240, 190, 90),
    "err": (240, 100, 100),
    "off": (60, 62, 74),  # unlit spinner cells
}


# --- themes: the user picks the assistant's color; ok/warn/err stay fixed so meaning doesn't change

PRESETS = {  # name: (title, accent, accent2 = gradient end, quote)
    "classic": ("Классика — голубой → фиолетовый", (94, 200, 235), (170, 120, 255), (120, 215, 190)),
    "matrix": ("Матрица — зелёный", (80, 250, 120), (20, 170, 80), (160, 240, 175)),
    "sunset": ("Закат — оранжевый → розовый", (255, 170, 80), (255, 90, 150), (255, 205, 150)),
    "ocean": ("Океан — синий", (90, 175, 255), (50, 95, 230), (140, 205, 255)),
    "sakura": ("Сакура — розовый → сиреневый", (255, 150, 200), (195, 125, 255), (255, 195, 225)),
    "ember": ("Огонь — красный → жёлтый", (255, 95, 70), (255, 195, 60), (255, 175, 125)),
    "gold": ("Золото", (245, 205, 95), (205, 150, 45), (245, 225, 160)),
    "mono": ("Монохром — серый", (215, 215, 225), (135, 135, 150), (200, 200, 210)),
}
# words people use for colors -> preset (prefix match, any language)
PRESETS_EN = {"classic": "Classic — cyan → violet", "matrix": "Matrix — green", "sunset": "Sunset — orange → pink",
              "ocean": "Ocean — blue", "sakura": "Sakura — pink → lilac", "ember": "Ember — red → yellow",
              "gold": "Gold", "mono": "Mono — gray"}


def preset_label(key: str) -> str:
    return t(PRESETS[key][0], PRESETS_EN.get(key, key))


COLOR_WORDS = {"голуб": "classic", "циан": "classic", "cyan": "classic", "classic": "classic", "класси": "classic",
               "jarvis": "classic",  # the theme's name before the project was renamed
               "зел": "matrix", "green": "matrix", "матриц": "matrix", "matrix": "matrix",
               "оранж": "sunset", "orange": "sunset", "закат": "sunset", "sunset": "sunset",
               "син": "ocean", "blue": "ocean", "океан": "ocean", "ocean": "ocean",
               "роз": "sakura", "pink": "sakura", "сакур": "sakura", "sakura": "sakura",
               "фиолет": "sakura", "сирен": "sakura", "purple": "sakura", "violet": "sakura",
               "красн": "ember", "red": "ember", "огн": "ember", "fire": "ember", "ember": "ember",
               "жёлт": "gold", "желт": "gold", "yellow": "gold", "золот": "gold", "gold": "gold",
               "сер": "mono", "бел": "mono", "gray": "mono", "grey": "mono", "white": "mono",
               "моно": "mono", "mono": "mono", "ч/б": "mono"}
HEX = re.compile(r"#?([0-9a-f]{6})\b", re.I)


def _from_hex(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _custom(colors: list[tuple[int, int, int]]) -> tuple:
    """One color: gradient end is the same hue shifted by 40°; quotes get a lighter tint."""
    a = colors[0]
    if len(colors) > 1:
        b = colors[1]
    else:
        h, l, s = colorsys.rgb_to_hls(*(x / 255 for x in a))
        b = tuple(round(x * 255) for x in colorsys.hls_to_rgb((h + 40 / 360) % 1, l, s))
    return (t("свой цвет", "custom color"), a, b, tuple(round(x + (255 - x) * 0.45) for x in a))


def resolve_theme(spec: str) -> tuple | None:
    """Preset name, '#rrggbb', '#rrggbb,#rrggbb', or a color word ("зелёный", "pink")."""
    spec = (spec or "").strip().lower()
    if spec in PRESETS:
        return PRESETS[spec]
    hexes = HEX.findall(spec)
    if hexes:
        return _custom([_from_hex(h) for h in hexes[:2]])
    for word in re.findall(r"[\w/ё]+", spec):
        for prefix, name in COLOR_WORDS.items():
            if word.startswith(prefix):
                return PRESETS[name]
    return None


def theme_name(spec: str) -> str:
    """Canonical value to store in the config: preset name or the hex spec."""
    spec = (spec or "").strip().lower()
    if spec in PRESETS:
        return spec
    hexes = HEX.findall(spec)
    if hexes:
        return ",".join(f"#{h}" for h in hexes[:2])
    found = resolve_theme(spec)
    return next((k for k, v in PRESETS.items() if v == found), "classic")


def set_theme(spec: str) -> None:
    theme = resolve_theme(spec) or PRESETS["classic"]
    THEME.update(accent=theme[1], accent2=theme[2], quote=theme[3])


# --- colors -------------------------------------------------------------------------


def _c(code: str, s: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if _COLOR else s


def _rgb_code(r: int, g: int, b: int) -> str:
    if _TRUECOLOR:
        return f"38;2;{r};{g};{b}"
    return f"38;5;{16 + 36 * round(r / 51) + 6 * round(g / 51) + round(b / 51)}"


def rgb(color: tuple[int, int, int] | str, s: str) -> str:
    return _c(_rgb_code(*(THEME[color] if isinstance(color, str) else color)), s)


def bold(s): return _c("1", s)
def dim(s): return _c("2", s)
def italic(s): return _c("3", s)
def green(s): return rgb("ok", s)
def yellow(s): return rgb("warn", s)
def red(s): return rgb("err", s)
def accent(s): return rgb("accent", s)


def _mix(a: tuple, b: tuple, t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def gradient(text: str, start: str = "accent", end: str = "accent2") -> str:
    if not _COLOR:
        return text
    n = max(1, len(text) - 1)
    return "".join(rgb(_mix(THEME[start], THEME[end], i / n), ch) for i, ch in enumerate(text))


def swatch(name: str, spec: str) -> str:
    """How the agent looks in a theme, without switching to it: badge + a lit scanner."""
    _, a, b, _ = resolve_theme(spec) or PRESETS["classic"]
    n = max(1, len(name) - 1)
    label = "".join(_c("1;" + _rgb_code(*_mix(a, b, i / n)), ch) for i, ch in enumerate(name))
    scan = "".join(_c(_rgb_code(*_mix(a, b, i / 9)), "━") for i in range(10))
    return f"{_c(_rgb_code(*b), '◆')} {label}  {scan}"


def link(text: str, url: str) -> str:
    """Clickable in terminals that support OSC 8 (alacritty, kitty, foot…); plain text elsewhere."""
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\" if _COLOR else text


SUP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def sup(n: int) -> str:
    return str(n).translate(SUP)


# --- layout -------------------------------------------------------------------------


def width(s: str) -> int:
    """Visible width: ANSI codes are zero-wide, CJK/emoji are two cells."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 0 if unicodedata.combining(ch) else 1
               for ch in ANSI.sub("", s))


def cols() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def wrap(line: str, w: int) -> list[str]:
    """Word-wraps a possibly colored line; continuation lines hang under the text after a short
    marker ("¹ «…", "• …") or under the original indentation."""
    if width(line) <= w:
        return [line]
    plain = ANSI.sub("", line)
    indent = len(plain) - len(plain.lstrip(" "))
    first = plain.lstrip(" ").split(" ", 1)[0]
    # hang under the text after a marker like "¹", "•", "$" — not after an ordinary short word
    is_marker = width(first) <= 3 and not any(ch.isalnum() and ch.isascii() for ch in first) \
        and not first[:1].isalpha()
    hang = indent + (width(first) + 1 if is_marker else 0)
    out, cur = [], " " * indent
    active: list[str] = []  # styles switched on so far: a continuation line opens them again
    for word in line[indent:].split(" ") if line.startswith(" " * indent) else line.split(" "):
        if cur.strip() and width(cur) + 1 + width(word) > w:
            out.append(cur + ("\x1b[0m" if _COLOR else ""))
            cur = " " * hang + "".join(active) + word
        else:
            cur = f"{cur} {word}" if cur.strip() else cur + word
        for code in SGR.findall(word):
            if code in ("\x1b[0m", "\x1b[m"):
                active.clear()
            else:
                active.append(code)
    out.append(cur)
    return out


def panel(title: str, lines: list[str], color: str = "accent", icon: str = "") -> None:
    """╭─ icon Title
       │ line…
       ╰─            (left rail only: right borders break on emoji/wide glyphs)"""
    rail = rgb(color, "│")
    head = f"{icon} {title}" if icon else title
    print(rgb(color, "╭─ ") + bold(rgb(color, head)))
    inner = max(20, cols() - 3)
    for line in lines:
        for part in wrap(line, inner) if line else [""]:
            print(f"{rail} {part}" if part else rail)
    print(rgb(color, "╰─"))


def kv(title: str, rows: list[tuple[str, str]], color: str = "accent", icon: str = "") -> None:
    if not rows:
        return
    kw = max(width(k) for k, _ in rows)
    vw = max(20, cols() - 3 - kw - 2)
    lines = []
    for k, v in rows:
        parts = wrap(v, vw)
        lines.append(f"{dim(k + ' ' * (kw - width(k)))}  {parts[0]}")
        lines += [" " * (kw + 2) + p.lstrip(" ") for p in parts[1:]]
    panel(title, lines, color, icon)


def problem(what: str, fix: str = "", title: str = "") -> None:
    title = title or t("Что случилось", "What happened")
    lines = [what] + (["", f"{rgb('ok', '➜')} {fix}"] if fix else [])
    panel(title, lines, "err", "✖")


def bar(pct: int, w: int = 20) -> str:
    filled = round(w * pct / 100)
    color = "err" if pct >= 90 else "warn" if pct >= 75 else "ok"
    return rgb(color, "█" * filled) + rgb("off", "░" * (w - filled))


def render_markdown(text: str) -> str:
    """Small models love markdown; turn the common bits into terminal formatting."""
    text = re.sub(r"^```[\w-]*\s*$", "", text, flags=re.M)  # code fences: keep the code, drop the fence
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: bold(m.group(1)), text)
    text = re.sub(r"`([^`\n]+)`", lambda m: bold(m.group(1)), text)
    text = re.sub(r"^(\s*)[*-]\s+", r"\1• ", text, flags=re.M)
    return re.sub(r"^#{1,6}\s*", "", text, flags=re.M)


def prompt() -> str:
    """Input prompt for readline: escape codes wrapped in \\001…\\002 so line editing
    computes the width right."""
    if not _COLOR:
        return "❯ "
    return f"\001\x1b[{_rgb_code(*THEME['accent2'])}m\002❯\001\x1b[0m\002 "


def badge(name: str) -> str:
    return rgb("accent2", "◆ ") + bold(gradient(name))


def say(name: str, msg: str) -> None:
    """Agent line; multi-line messages hang under the text, not under the name."""
    pad = width(name) + 4
    lines = msg.split("\n")
    w = max(20, cols() - pad)
    out = []
    for line in lines:
        out += wrap(line, w) if line else [""]
    print(f"{badge(name)}  {out[0]}")
    for line in out[1:]:
        print(" " * pad + line.lstrip(" ") if line else "")


def banner(name: str, subtitle: str = "") -> None:
    spaced = " ".join(name.upper())
    print()
    print("  " + rgb("accent2", "◆ ") + bold(gradient(spaced)))
    if subtitle:
        print("  " + dim(subtitle))
    print()


# --- waiting animation ----------------------------------------------------------------


class Spinner:
    """KITT-style scanner: a light sweeps back and forth leaving a fading tail, the label says
    what's going on and a timer appears once it takes longer than a second. Draws on stderr,
    only on a terminal; do NOT print inside the `with` block — update the label instead."""

    CELLS = 10
    FPS = 16

    def __init__(self, text: str):
        self.text = text
        self.enabled = _COLOR and sys.stderr.isatty()
        self._stop = threading.Event()
        self._thread = None
        self._t0 = time.monotonic()

    def update(self, text: str) -> None:
        self.text = text

    def _frame(self, i: int) -> str:
        period = 2 * (self.CELLS - 1)
        step = i % period
        pos = step if step < self.CELLS else period - step
        direction = 1 if step < self.CELLS else -1
        cells = []
        for c in range(self.CELLS):
            behind = (pos - c) * direction  # distance behind the head along the motion
            glow = 1.0 if c == pos else max(0.0, 1 - behind / 4) if behind > 0 else 0.0
            hue = _mix(THEME["accent"], THEME["accent2"], c / (self.CELLS - 1))
            cells.append(rgb(_mix(THEME["off"], hue, glow), "━"))
        elapsed = time.monotonic() - self._t0
        timer = f" {dim(f'{elapsed:.0f}' + t('с', 's'))}" if elapsed >= 1 else ""
        label = self.text if width(self.text) < cols() - self.CELLS - 10 else self.text[: cols() - self.CELLS - 12] + "…"
        return f"\r\x1b[K {''.join(cells)} {dim(label)}{timer}"

    def _run(self) -> None:
        i = 0
        while not self._stop.wait(1 / self.FPS):
            sys.stderr.write(self._frame(i))
            sys.stderr.flush()
            i += 1

    def __enter__(self) -> "Spinner":
        if self.enabled:
            sys.stderr.write("\x1b[?25l")  # hide cursor
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._thread:
            self._stop.set()
            self._thread.join()
            sys.stderr.write("\r\x1b[K\x1b[?25h")
            sys.stderr.flush()


def working(text: str) -> Spinner:
    return Spinner(text)


# --- interaction ----------------------------------------------------------------------


def _hex(color: str) -> str:
    return "#%02x%02x%02x" % THEME[color]


def choose(lines: list[str], prompt: str) -> int | None:
    """Returns the index of the chosen line, or None if cancelled."""
    if not lines:
        return None
    if shutil.which("fzf"):
        data = "\n".join(f"{i}\t{line}" for i, line in enumerate(lines))
        colors = (f"border:{_hex('accent')},label:{_hex('accent')}:bold,prompt:{_hex('accent')},"
                  f"pointer:{_hex('accent2')},hl:{_hex('accent2')},hl+:{_hex('accent2')},"
                  f"header:{_hex('source')},fg+:bold,bg+:-1,gutter:-1")
        r = subprocess.run(
            ["fzf", "--height=~50%", "--reverse", "--no-sort", "--ansi", "--delimiter=\t",
             "--with-nth=2..", "--border=rounded", f"--border-label= {prompt.rstrip(' >')} ",
             "--border-label-pos=3", "--info=inline-right", "--pointer=▶", "--prompt=❯ ",
             "--header=" + t("Enter — выбрать · Esc — выйти", "Enter — choose · Esc — leave"), f"--color={colors}"],
            input=data, stdout=subprocess.PIPE, text=True)
        out = r.stdout.strip()
        return int(out.split("\t", 1)[0]) if r.returncode == 0 and out else None
    panel(prompt.rstrip(" >"), [f"{dim(str(i).rjust(2))}  {line}" for i, line in enumerate(lines, 1)])
    ans = input(t("  номер (Enter — отмена): ", "  number (Enter — cancel): ")).strip()
    return int(ans) - 1 if ans.isdigit() and 1 <= int(ans) <= len(lines) else None


def confirm(cmd: list[str], question: str = "") -> bool:
    question = question or t("Выполнить?", "Run it?")
    color = "warn" if cmd[:1] == ["sudo"] else "accent"  # root commands stand out
    print(rgb(color, "╭─ ") + bold(rgb(color, t("команда", "command"))) +
          (dim(t("  (с правами root)", "  (as root)")) if color == "warn" else ""))
    for part in wrap(f"{dim('$')} {bold(shlex.join(cmd))}", max(20, cols() - 3)):
        print(f"{rgb(color, '│')} {part}")
    try:
        ans = input(rgb(color, "╰─ ") + f"{question} {dim('[y/N]')} ")
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes", "д", "да")
