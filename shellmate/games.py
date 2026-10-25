"""Will a game run here: Steam and GOG lookups, ProtonDB (the summary and, optionally, the monthly
dump of user reports, compared on similar hardware), Steam Deck status, anti-cheat support, and a
check of what this computer needs for games. Code and public data only; the model isn't asked.

ProtonDB report data: https://github.com/bdefore/protondb-data, Open Database License (ODbL)."""

import io
import json
import os
import re
import sqlite3
import tarfile
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from shellmate import web
from shellmate import library_sources as src
from shellmate.i18n import t

CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "shellmate" / "games"


def _cached_json(name: str, url: str, max_age: float, timeout: float = 15):
    path = CACHE / name
    if path.exists() and time.time() - path.stat().st_mtime < max_age:
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    try:
        body = web.get(url, timeout, headers={**src.agent(), "Accept": "application/json"})[1]
        data = json.loads(body)
    except (OSError, ValueError):
        return json.loads(path.read_text()) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return data


# --- stores ----------------------------------------------------------------------------------------

@dataclass
class Game:
    store: str  # steam | gog
    id: str
    name: str
    linux: bool  # a native Linux version
    url: str = ""


def steam_search(term: str, n: int = 6) -> list[Game]:
    q = urllib.parse.urlencode({"term": term, "l": "english", "cc": "US"})
    data = _cached_json(f"steam-{_slug(term)}.json", f"https://store.steampowered.com/api/storesearch/?{q}", 86400) or {}
    return [Game("steam", str(i["id"]), i["name"], bool((i.get("platforms") or {}).get("linux")),
                 f"https://store.steampowered.com/app/{i['id']}/") for i in data.get("items", [])[:n] if i.get("type") == "app"]


def gog_search(term: str, n: int = 4) -> list[Game]:
    q = urllib.parse.urlencode({"query": f"like:{term}", "limit": n, "productType": "in:game,pack"})
    data = _cached_json(f"gog-{_slug(term)}.json", f"https://catalog.gog.com/v1/catalog?{q}", 86400) or {}
    return [Game("gog", str(p["id"]), p["title"], "linux" in (p.get("operatingSystems") or []), p.get("storeLink", ""))
            for p in data.get("products", [])[:n]]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def same_name(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", re.sub(r"[™®©]|\b(?:goty|game of the year|edition|remastered)\b", "", s.lower()))  # noqa: E731
    return norm(a) == norm(b)


# --- compatibility data ----------------------------------------------------------------------------

TIERS = {"platinum": 5, "gold": 4, "silver": 3, "bronze": 2, "borked": 0, "native": 6, "pending": -1}


def protondb_summary(appid: str) -> dict | None:
    """{"tier": "gold", "trendingTier": …, "total": 2106, "confidence": …} or None (no reports)."""
    return _cached_json(f"protondb-{appid}.json", f"https://www.protondb.com/api/v1/reports/summaries/{appid}.json", 86400)


DECK = {0: "", 1: "unsupported", 2: "playable", 3: "verified"}


def deck_status(appid: str) -> str:
    data = _cached_json(f"deck-{appid}.json",
                        f"https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport?nAppID={appid}", 86400)
    try:
        return DECK.get(int(data["results"]["resolved_category"]), "")
    except (TypeError, KeyError, ValueError):
        return ""


AWACY = "https://raw.githubusercontent.com/AreWeAntiCheatYet/AreWeAntiCheatYet/HEAD/games.json"


@dataclass
class AntiCheat:
    status: str  # Supported | Running | Planned | Broken | Denied
    anticheats: list[str]
    notes: list[str] = field(default_factory=list)


def anticheat(appid: str = "", name: str = "") -> AntiCheat | None:
    games = _cached_json("awacy.json", AWACY, 7 * 86400, timeout=30) or []
    for g in games:
        if appid and str((g.get("storeIds") or {}).get("steam", "")) == appid or name and same_name(g.get("name", ""), name):
            return AntiCheat(g.get("status", ""), g.get("anticheats") or [], [n[0] for n in g.get("notes") or [] if n])
    return None


def lutris_slug(name: str) -> str:
    data = _cached_json(f"lutris-{_slug(name)}.json", "https://lutris.net/api/games?" + urllib.parse.urlencode({"search": name}), 86400)
    for g in (data or {}).get("results", []):
        if same_name(g.get("name", ""), name):
            return g.get("slug", "")
    return ""


# --- hardware classes: what "similar hardware" means -----------------------------------------------

RADV = {"navi10": "rdna1", "navi12": "rdna1", "navi14": "rdna1", "navi21": "rdna2", "navi22": "rdna2", "navi23": "rdna2",
        "navi24": "rdna2", "navi31": "rdna3", "navi32": "rdna3", "navi33": "rdna3", "navi44": "rdna4", "navi48": "rdna4",
        "polaris": "polaris", "ellesmere": "polaris", "baffin": "polaris", "lexa": "polaris", "vega": "vega",
        "renoir": "igpu", "cezanne": "igpu", "lucienne": "igpu", "rembrandt": "igpu", "phoenix": "igpu",
        "raphael": "igpu", "mendocino": "igpu", "strix": "igpu", "hawk": "igpu", "picasso": "igpu", "raven": "igpu"}


def gpu_class(gpu: str) -> str:
    """"NVIDIA GeForce RTX 4090" -> "nvidia rtx40", "AMD Radeon RX 6700 XT (RADV NAVI22)" -> "amd rdna2"."""
    s = gpu.lower()
    if re.search(r"custom gpu 0405|custom gpu 0932|vangogh|sephiroth", s):
        return "steamdeck"
    if re.search(r"nvidia|geforce|quadro|\brtx|\bgtx|nvrm", s):
        m = re.search(r"rtx\s*a?(\d{2})\d{2}", s)
        if m:
            return f"nvidia rtx{m.group(1)}"
        m = re.search(r"gtx\s*(\d{3,4})", s)
        if m:
            n = m.group(1)
            return f"nvidia gtx{n[:2] if len(n) == 4 else n[0]}"
        return "nvidia other"
    if re.search(r"\bamd\b|radeon|radv|\bnavi|vega|polaris", s):
        for code, cls in RADV.items():
            if code in s:
                return f"amd {cls}"
        m = re.search(r"rx\s*(\d)\d{3}", s)
        if m:
            return {"5": "amd rdna1", "6": "amd rdna2", "7": "amd rdna3", "9": "amd rdna4"}.get(m.group(1), "amd other")
        if re.search(r"rx\s*[45]\d0\b|pro\s*5\d\dx?\b", s):
            return "amd polaris"
        return "amd igpu" if re.search(r"radeon(?:\(tm\))? graphics|vega \d+ graphics", s) else "amd other"
    if "intel" in s:
        return "intel arc" if re.search(r"\barc\b", s) else "intel igpu"
    return ""


def class_label(cls: str) -> str:
    """"nvidia rtx50" -> "NVIDIA RTX 50", "amd rdna3" -> "AMD RDNA 3", "intel igpu" -> "Intel integrated"."""
    special = {"steamdeck": "Steam Deck", "intel arc": "Intel Arc", "intel igpu": t("встроенная Intel", "Intel integrated"),
               "amd igpu": t("встроенная AMD", "AMD integrated")}
    if cls in special:
        return special[cls]
    vendor, _, gen = cls.partition(" ")
    gen = re.sub(r"^(rtx|gtx|rdna)(\d+)$", lambda m: f"{m.group(1).upper()} {m.group(2)}", gen)
    gen = "" if gen == "other" else gen.capitalize() if gen.islower() else gen
    return f"{ {'nvidia': 'NVIDIA', 'amd': 'AMD', 'intel': 'Intel'}.get(vendor, vendor)} {gen}".strip()


# --- the ProtonDB dump: user reports, kept compact ---------------------------------------------------

def db_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "shellmate" / "protondb.sqlite"


def connect() -> sqlite3.Connection:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path())
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS reports (appid INTEGER, ts INTEGER, gclass TEXT, gpu TEXT, works INTEGER,
                                            oob INTEGER, proton TEXT, launch TEXT, note TEXT);
        CREATE INDEX IF NOT EXISTS reports_app ON reports(appid);
    """)
    return db


def dump_info() -> tuple[str, float] | None:
    """(dump name, fetched at) of the downloaded reports, or None."""
    if not db_path().exists():
        return None
    db = connect()
    rows = dict(db.execute("SELECT key, value FROM meta").fetchall())
    db.close()
    return (rows["dump"], float(rows["fetched"])) if "dump" in rows else None


MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def latest_dump() -> tuple[str, str]:
    """(name, download URL) of the newest monthly export."""
    files = src.get_json("https://api.github.com/repos/bdefore/protondb-data/contents/reports")

    def when(name: str) -> tuple[int, int, int]:
        m = re.match(r"reports_([a-z]{3})(\d+)_(\d{4})", name)
        return (int(m.group(3)), MONTHS.get(m.group(1), 0), int(m.group(2))) if m else (0, 0, 0)
    best = max((f for f in files if f["name"].endswith(".tar.gz")), key=lambda f: when(f["name"]))
    return best["name"], best["download_url"]


def proton_name(r: dict) -> str:
    custom = (r.get("customProtonVersion") or "").strip()
    if custom:
        m = re.match(r"(GE-Proton\d+|Proton[- ]?\d+|proton-cachyos|Proton-?GE)", custom, re.I)
        return m.group(1) if m else custom[:24]
    v = (r.get("protonVersion") or "").strip()
    if not v or v.lower() == "default":
        return "GE-Proton" if r.get("variant") == "ge" else ""
    if v.lower().startswith("experimental"):
        return "Proton Experimental"
    m = re.match(r"(\d+)\.", v)
    return f"Proton {m.group(1)}" if m else v[:24]


def _note(r: dict) -> str:
    notes = r.get("notes") or {}
    parts = [r.get("concludingNotes") or "", notes.get("verdict") or "", notes.get("extra") or ""]
    text = " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return re.sub(r"\s+", " ", text)[:400]


def reports_from(fileobj, since: int):
    """Report rows from the export's JSON array, read as a stream (the file is ~500 MB)."""
    dec = json.JSONDecoder()
    buf, pos, started = "", 0, False
    while True:
        chunk = fileobj.read(1 << 20)
        buf = buf[pos:] + chunk.decode(errors="replace")
        pos = 0
        if not started:
            if "[" not in buf:
                if not chunk:
                    return
                continue
            pos, started = buf.index("[") + 1, True
        while True:
            while pos < len(buf) and buf[pos] in " \n\t\r,":
                pos += 1
            if pos >= len(buf) or buf[pos] == "]":
                break
            try:
                obj, end = dec.raw_decode(buf, pos)
            except json.JSONDecodeError:
                break
            pos = end
            ts = int(obj.get("timestamp") or 0)
            app = ((obj.get("app") or {}).get("steam") or {}).get("appId")
            if ts < since or not app:
                continue
            r = obj.get("responses") or {}
            gpu = (obj.get("systemInfo") or {}).get("gpu") or ""
            yield (int(app), ts, gpu_class(gpu), gpu[:60], int(r.get("verdict") == "yes"), int(r.get("verdictOob") == "yes"),
                   proton_name(r), (r.get("launchOptions") or "").strip()[:200], _note(r))
        if not chunk:
            return


def update_dump(progress=None, years: int = 3) -> int:
    """Downloads the newest export and keeps the last `years` of reports. Returns the row count."""
    name, url = latest_dump()
    data = src.download(url, progress, "ProtonDB", timeout=180)
    since = int(time.time() - years * 365 * 86400)
    db = connect()
    db.execute("DELETE FROM reports")
    n = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar:
            f = tar.extractfile(m) if m.isfile() and m.name.endswith(".json") else None
            if not f:
                continue
            batch = []
            for row in reports_from(f, since):
                batch.append(row)
                if len(batch) >= 5000:
                    db.executemany("INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
                    n += len(batch)
                    batch.clear()
                    if progress:
                        progress(t(f"ProtonDB: {n} отчётов", f"ProtonDB: {n} reports"))
            db.executemany("INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
            n += len(batch)
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [("dump", name), ("fetched", str(time.time()))])
    db.commit()
    db.execute("VACUUM")
    db.close()
    return n


@dataclass
class Similar:
    label: str  # the hardware class compared ("NVIDIA RTX 50", or "NVIDIA" when too few)
    total: int
    works: int
    proton: list[tuple[str, int]]  # most used in working reports
    launch: list[tuple[str, int]]  # launch options used more than once
    notes: list[tuple[str, str, str]]  # (date, gpu, note) — recent, verbatim
    recent: int = 0  # reports in the last year


def similar(appid: str, my_class: str) -> Similar | None:
    """Reports for this game from the same GPU generation (or vendor, if that has too few)."""
    if not db_path().exists() or not my_class:
        return None
    db = connect()
    rows = db.execute("SELECT ts, gclass, gpu, works, proton, launch, note FROM reports WHERE appid = ? ORDER BY ts DESC",
                      (int(appid),)).fetchall()
    db.close()
    vendor = my_class.split(" ")[0]
    picked, label = [r for r in rows if r[1] == my_class], class_label(my_class)
    if len(picked) < 5:
        picked, label = [r for r in rows if r[1].split(" ")[0] == vendor], class_label(vendor)
    if not picked:
        return None
    ok = [r for r in picked if r[3]]
    proton = Counter(r[4] for r in ok if r[4]).most_common(3)
    launch = [(k, v) for k, v in Counter(re.sub(r"\s+", " ", r[5]) for r in picked if r[5]).most_common(3) if v > 1]
    notes = [(time.strftime("%Y-%m", time.gmtime(r[0])), r[2], r[6]) for r in picked if len(r[6]) >= 40][:3]
    year = time.time() - 365 * 86400
    return Similar(label, len(picked), len(ok), proton, launch, notes, sum(r[0] > year for r in picked))


# --- is this computer ready for games ----------------------------------------------------------------

@dataclass
class Check:
    ok: bool
    what: str
    fix: list[str] = field(default_factory=list)  # packages that fix it
    hint: str = ""  # when it isn't a package (enable multilib, …)
    optional: bool = False


GAMING_PACKAGES = {
    "arch": {"steam": ["steam"], "gamemode": ["gamemode", "lib32-gamemode"], "mangohud": ["mangohud", "lib32-mangohud"],
             "gamescope": ["gamescope"], "amd": ["vulkan-radeon", "lib32-vulkan-radeon", "lib32-mesa"],
             "intel": ["vulkan-intel", "lib32-vulkan-intel", "lib32-mesa"], "nvidia": ["lib32-nvidia-utils"]},
    "debian": {"steam": ["steam-installer"], "gamemode": ["gamemode"], "mangohud": ["mangohud"], "gamescope": ["gamescope"],
               "amd": ["mesa-vulkan-drivers", "mesa-vulkan-drivers:i386"], "intel": ["mesa-vulkan-drivers", "mesa-vulkan-drivers:i386"],
               "nvidia": []},
    "fedora": {"steam": ["steam"], "gamemode": ["gamemode"], "mangohud": ["mangohud"], "gamescope": ["gamescope"],
               "amd": ["mesa-vulkan-drivers", "mesa-vulkan-drivers.i686"], "intel": ["mesa-vulkan-drivers", "mesa-vulkan-drivers.i686"],
               "nvidia": []},
}


def readiness(family: str, gpus: list[str], have, flatpaks: set[str], icd: Path = Path("/usr/share/vulkan/icd.d"),
              pacman_conf: Path = Path("/etc/pacman.conf"), foreign_arch: str = "", max_map: int = 0) -> list[Check]:
    """have(command) -> bool. Every input is passed in, so this is testable without a system."""
    pk = GAMING_PACKAGES.get(family, GAMING_PACKAGES["arch"])
    out = []
    vendors = {gpu_class(g).split(" ")[0] for g in gpus} - {""}
    icds = {p.name for p in icd.glob("*.json")} if icd.is_dir() else set()
    for v in sorted(vendors):
        name = {"nvidia": "nvidia_icd", "amd": "radeon_icd", "intel": "intel_icd", "steamdeck": "radeon_icd"}.get(v, "")
        ok = any(i.startswith(name) for i in icds)
        out.append(Check(ok, t(f"Vulkan для видеокарты {v.upper() if v != 'intel' else 'Intel'}", f"Vulkan for the {v.upper() if v != 'intel' else 'Intel'} GPU"),
                         [] if ok else pk.get("amd" if v == "steamdeck" else v, [])[:1]))
    if family == "arch":
        multilib = pacman_conf.exists() and re.search(r"^\[multilib\]", pacman_conf.read_text(errors="replace"), re.M)
        out.append(Check(bool(multilib), t("32-битные библиотеки (репозиторий multilib) — нужны Steam и Wine",
                                           "32-bit libraries (the multilib repository) — Steam and Wine need them"),
                         hint="" if multilib else t("раскомментируй [multilib] и строку Include под ним в /etc/pacman.conf, затем sudo pacman -Syu",
                                                    "uncomment [multilib] and the Include line under it in /etc/pacman.conf, then sudo pacman -Syu")))
        if multilib:
            for v in sorted(vendors):
                lib32 = [p for p in pk.get("amd" if v == "steamdeck" else v, []) if p.startswith("lib32-")]
                if lib32:
                    out.append(Check(all(have(f"pkg:{p}") for p in lib32), t(f"32-битный драйвер {v}", f"32-bit {v} driver"),
                                     [p for p in lib32 if not have(f"pkg:{p}")]))
    elif family == "debian":
        ok = "i386" in foreign_arch.split()
        out.append(Check(ok, t("32-битная архитектура i386 — нужна Steam", "the i386 architecture — Steam needs it"),
                         hint="" if ok else "sudo dpkg --add-architecture i386 && sudo apt update"))
    steam = have("steam") or "com.valvesoftware.Steam" in flatpaks
    out.append(Check(steam, "Steam", [] if steam else pk["steam"]))
    for cmd, label, key, optional in (("gamemoderun", t("GameMode — ускоряет игры на время запуска", "GameMode — speeds games up while they run"), "gamemode", False),
                                      ("mangohud", t("MangoHud — FPS и загрузка поверх игры", "MangoHud — FPS and load over the game"), "mangohud", True),
                                      ("gamescope", t("gamescope — масштабирование и HDR для игр", "gamescope — scaling and HDR for games"), "gamescope", True)):
        ok = have(cmd)
        out.append(Check(ok, label, [] if ok else pk[key], optional=optional))
    launchers = [n for n, c, f in (("Heroic", "heroic", "com.heroicgameslauncher.hgl"), ("Lutris", "lutris", "net.lutris.Lutris"))
                 if have(c) or f in flatpaks]
    out.append(Check(bool(launchers), t("лаунчер для GOG/Epic: ", "a GOG/Epic launcher: ") + (", ".join(launchers) or "Heroic"),
                     hint="" if launchers else "flatpak install flathub com.heroicgameslauncher.hgl", optional=True))
    if max_map:
        ok = max_map >= 1048576
        out.append(Check(ok, f"vm.max_map_count = {max_map}" + ("" if ok else t(" (некоторым играм мало)", " (too low for some games)")),
                         hint="" if ok else "echo 'vm.max_map_count = 1048576' | sudo tee /etc/sysctl.d/80-gamecompatibility.conf && sudo sysctl --system"))
    return out
