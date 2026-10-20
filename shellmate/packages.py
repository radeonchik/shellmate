"""Package search and command building on top of the distribution's backend
(pacman / apt / dnf, see shellmate.backends), plus Flatpak and Snap which work everywhere.
Pure deterministic code — no LLM here."""

import re
import shutil
from difflib import SequenceMatcher

from shellmate import backends
from shellmate.backends.base import ANSI, Pkg, run_quiet  # noqa: F401  (re-exported for other modules)


def backend() -> backends.Backend:
    return backends.current()


def sources_label(use_flatpak: bool = True) -> str:
    extra = ", Flatpak" if use_flatpak and shutil.which("flatpak") else ""
    return backend().sources_label + extra


# --- Flatpak / Snap ----------------------------------------------------------------------------


def parse_flatpak(out: str) -> list[Pkg]:
    res = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and "." in parts[0]:
            res.append(Pkg("flatpak", parts[0], parts[2], parts[3] if len(parts) > 3 else "", title=parts[1]))
    return res


def parse_snap(out: str, installed: bool = False) -> list[Pkg]:
    """`snap find` / `snap list` tables: columns separated by 2+ spaces, header first."""
    lines = out.splitlines()
    if not lines or not lines[0].startswith("Name"):
        return []
    header = re.split(r"\s{2,}", lines[0].strip())
    res = []
    for line in lines[1:]:
        cols = dict(zip(header, re.split(r"\s{2,}", line.strip())))
        if cols.get("Name"):
            res.append(Pkg("snap", cols["Name"], cols.get("Version", ""), cols.get("Summary", ""), installed=installed))
    return res


def _extra_search(term: str, use_flatpak: bool) -> list[Pkg]:
    found = []
    if use_flatpak and shutil.which("flatpak"):
        found += parse_flatpak(run_quiet(["flatpak", "search", "--columns=application,name,version,description", term]))
    if shutil.which("snap"):
        found += parse_snap(run_quiet(["snap", "find", term], timeout=20))
    return found


def _extra_installed(want: str, use_flatpak: bool) -> list[Pkg]:
    found = []
    if use_flatpak and shutil.which("flatpak"):
        for p in parse_flatpak(run_quiet(["flatpak", "list", "--app", "--columns=application,name,version,description"])):
            if want in p.name.lower() or want in p.title.lower():
                p.installed = True
                found.append(p)
    if shutil.which("snap"):
        found += [p for p in parse_snap(run_quiet(["snap", "list"]), installed=True) if want in p.name.lower()]
    return found


# --- search ------------------------------------------------------------------------------------


def _search_once(term: str, use_flatpak: bool) -> list[Pkg]:
    return backend().search(term) + _extra_search(term, use_flatpak)


def _rank(p: Pkg, want: str) -> tuple:
    title = p.title.lower().replace(" ", "-")
    if p.name == want or title == want:
        match = 0
    elif p.name.startswith(want) or title.startswith(want):
        match = 1
    elif want in p.name or want in title:
        match = 2
    else:
        match = 3
    origin = 0 if p.is_native and p.source != "aur" else 1 if p.source == "aur" else 2
    return (match, origin, -p.votes, p.name)


def search(want: str, use_flatpak: bool = True, limit: int = 15) -> list[Pkg]:
    """Search the distro repos (+ AUR) + Flatpak + Snap. If the exact guess finds nothing,
    retry with its separate words."""
    found = _search_once(want, use_flatpak)
    if not found:
        words = [w for w in re.split(r"[-_\s]+", want) if len(w) > 2]
        if len(words) > 1:
            found = _search_once(" ".join(words), use_flatpak)
    seen, uniq = set(), []
    for p in found:  # first occurrence wins = the package manager's repo priority
        key = (p.source if not p.is_native else "", p.name)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    key = want.lower().replace(" ", "-")
    return sorted(uniq, key=lambda p: _rank(p, key))[:limit]


def installed_matching(want: str, use_flatpak: bool = True) -> list[Pkg]:
    key = want.lower()
    found = backend().installed(want) + _extra_installed(key, use_flatpak)
    # pacman -Qs also matches descriptions ("obs" -> libxmlb); prefer name matches
    by_name = [p for p in found if key in p.name.lower() or key in p.title.lower()]
    return sorted(by_name or found, key=lambda p: _rank(p, key))


def installed_version(name: str) -> str | None:
    return backend().installed_version(name)


# --- commands ----------------------------------------------------------------------------------


def install_commands(pkgs: list[Pkg]) -> list[list[str]]:
    native = [p for p in pkgs if p.is_native]
    flat = [p.name for p in pkgs if p.is_flatpak]
    snap = [p.name for p in pkgs if p.is_snap]
    cmds = [backend().install_cmd(native)] if native else []
    if flat:
        cmds.append(["flatpak", "install", "flathub", *flat])
    if snap:
        cmds.append(["sudo", "snap", "install", *snap])
    return cmds


def remove_commands(pkgs: list[Pkg]) -> list[list[str]]:
    native = [p.name for p in pkgs if p.is_native]
    flat = [p.name for p in pkgs if p.is_flatpak]
    snap = [p.name for p in pkgs if p.is_snap]
    cmds = [backend().remove_cmd(native)] if native else []
    if flat:
        cmds.append(["flatpak", "uninstall", *flat])
    if snap:
        cmds.append(["sudo", "snap", "remove", *snap])
    return cmds


def update_commands(use_flatpak: bool = True) -> list[list[str]]:
    cmds = backend().upgrade_cmds()
    if use_flatpak and shutil.which("flatpak"):
        cmds.append(["flatpak", "update"])
    if shutil.which("snap"):
        cmds.append(["sudo", "snap", "refresh"])
    return cmds


# --- downgrade / snapshots -------------------------------------------------------------------


def cached_versions(name: str) -> list[tuple[str, str]]:
    return backend().versions(name)


def vercmp(a: str, b: str) -> int:
    return backend().vercmp(a, b)


def snapshot_plan() -> tuple[str, list[str] | None]:
    """How a system snapshot gets made before an update: (explanation, command or None)."""
    auto = backend().auto_snapshot()
    if auto:
        return auto, None
    if shutil.which("snapper") and "root" in run_quiet(["snapper", "list-configs"]):
        return "сначала сделаю снимок системы (snapper)", [
            "sudo", "snapper", "-c", "root", "create", "-d", "before update (shellmate)"]
    if shutil.which("timeshift"):
        return "сначала сделаю снимок системы (timeshift)", [
            "sudo", "timeshift", "--create", "--comments", "before update (shellmate)"]
    return "снимков системы нет: если обновление что-то сломает, откатывать придётся пакеты по одному", None


# --- fallback for names the model garbled -----------------------------------------------------

TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "j", "k", "l", "m", "n", "o",
                     "p", "r", "s", "t", "u", "f", "h", "c", "ch", "sh", "sch", "", "y", "", "e", "yu",
                     "ya", "i", "i", "e", "g"]))

# verbs and filler of the request itself, not program names
REQUEST_WORDS = ("постав", "устан", "накат", "скача", "скачи", "загруз", "удал", "снес", "пожал",
                 "хочу", "нужн", "можеш", "прогр", "прилож", "мне", "плиз", "пжлст", "встанов")


def translit(word: str) -> str:
    return "".join(TRANSLIT.get(ch, ch) for ch in word.lower())


def spelled_words(query: str) -> list[str]:
    """Transliterated program names the user typed in Cyrillic ("обсидиан" -> "obsidian")."""
    return [translit(w) for w in re.findall(r"[а-яёіїєґ]{4,}", query.lower()) if not w.startswith(REQUEST_WORDS)]


def merge_with_spelling(found: list[Pkg], query: str, guess: str, use_flatpak: bool = True,
                        limit: int = 15) -> list[Pkg]:
    """The model may pick a near neighbour ("обсидиан" -> obs-studio). If what the user actually
    spelled matches another package clearly better, put that one first; keep the rest."""
    words = spelled_words(query)
    if not words or any(SequenceMatcher(None, guess, w).ratio() > 0.8 for w in words):
        return found
    alt = fuzzy_search(query, guess, use_flatpak, limit)
    best = lambda p: max(SequenceMatcher(None, p.name, w).ratio() for w in words)  # noqa: E731
    if alt and (not found or best(alt[0]) > best(found[0]) + 0.15):
        names = {(p.source, p.name) for p in alt}
        return (alt + [p for p in found if (p.source, p.name) not in names])[:limit]
    return found


def fuzzy_search(query: str, guess: str, use_flatpak: bool = True, limit: int = 15) -> list[Pkg]:
    """When the model's package name finds nothing ("кденлайв" -> "cdk-live"): search by the
    first letters of each transliterated word of the request, rank by spelling similarity."""
    words = spelled_words(query)
    found: dict[tuple[str, str], Pkg] = {}
    for w in words:
        for p in _search_once(w[:4], use_flatpak):
            found.setdefault((p.source, p.name), p)

    def similarity(p: Pkg) -> float:
        return max(SequenceMatcher(None, p.name, t).ratio() for t in [guess, *words])

    ranked = sorted(found.values(), key=lambda p: (-similarity(p), -p.votes))
    return [p for p in ranked if similarity(p) >= 0.5][:limit]
