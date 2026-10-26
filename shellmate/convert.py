"""Converting and compressing files: a Plan (what the router understood) -> ready argv for ffmpeg,
ImageMagick and bsdtar/7z. The model never writes these commands; it only fills CONVERT_SCHEMA.
Outputs go next to the source and never overwrite anything."""

import glob
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from shellmate.i18n import t

VIDEO = {"mp4", "mkv", "webm", "mov", "avi", "m4v", "flv", "wmv", "mpg", "mpeg", "ts", "3gp", "ogv"}
AUDIO = {"mp3", "flac", "ogg", "opus", "wav", "m4a", "aac", "wma", "aiff", "aif", "alac"}
IMAGE = {"jpg", "jpeg", "png", "webp", "avif", "heic", "heif", "gif", "bmp", "tif", "tiff", "ico", "jxl"}
ARCHIVE = {"zip", "7z", "rar", "tar", "gz", "tgz", "bz2", "tbz2", "xz", "txz", "zst", "iso", "cab", "lz4"}
PACK = ["zip", "tar.gz", "tar.xz", "tar.zst", "7z"]  # what we create (bsdtar -a picks by suffix)

OPERATIONS = ["convert", "compress", "trim", "extract_audio", "remove_audio", "to_gif", "resize",
              "strip_metadata", "extract", "archive"]
QUALITY = ["", "low", "medium", "high"]

CONVERT_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": OPERATIONS},
        "inputs": {"type": "array", "items": {"type": "string"}},
        "target": {"type": "string"},
        "max_size_mb": {"type": "number"},
        "width": {"type": "integer"},
        "start": {"type": "string"},
        "end": {"type": "string"},
        "quality": {"type": "string", "enum": QUALITY},
    },
    "required": ["operation", "inputs", "target", "max_size_mb", "width", "start", "end", "quality"],
}

CONVERT_SYSTEM = """You read a request to convert, compress, cut or pack files and fill JSON. Never run anything.

operation:
- convert: change the format (video -> another video format, image -> another image format, audio -> audio).
- compress: make files smaller (optionally to a size in MB) keeping the format.
- trim: cut a piece of a video or audio file (start / end times).
- extract_audio: take the sound out of a video (into mp3 etc.).
- remove_audio: make a video silent.
- to_gif: make a gif from a video.
- resize: change the width of images or a video.
- strip_metadata: remove EXIF / GPS / metadata.
- extract: unpack an archive.
- archive: pack files or folders into an archive.

inputs: the files, folders or patterns the user named, copied as written ("видео.mp4", "~/Downloads/*.heic",
"все heic в загрузках" -> "~/Downloads/*.heic", "фотки в этой папке" -> "*.jpg"). Folders: загрузки -> ~/Downloads,
документы -> ~/Documents, картинки/фото -> ~/Pictures, видео -> ~/Videos, музыка -> ~/Music, рабочий стол -> ~/Desktop.
Empty list when the user named no file.
target: the wanted format as an extension without a dot (mp4, mp3, jpg, png, webp, zip, tar.gz, 7z...), "" if not said.
max_size_mb: the wanted size in megabytes (1 GB = 1024), 0 if not said.
width: wanted width in pixels (720p -> 1280, 1080p -> 1920, "в два раза меньше" -> 0), 0 if not said.
start, end: times as m:ss or h:mm:ss ("с 10 секунды" -> "0:10", "первые 30 секунд" -> start "0:00" end "0:30"), "" if not said.
quality: low / medium / high when the user asked for strong compression, normal or best quality, else ""."""

EXAMPLES = [
    ("сожми video.mp4 до 25 мб", {"operation": "compress", "inputs": ["video.mp4"], "target": "", "max_size_mb": 25,
                                  "width": 0, "start": "", "end": "", "quality": ""}),
    ("переделай все png на рабочем столе в webp", {"operation": "convert", "inputs": ["~/Desktop/*.png"], "target": "webp",
                                                   "max_size_mb": 0, "width": 0, "start": "", "end": "", "quality": ""}),
    ("вырежи из clip.mkv кусок с 1:05 по 1:40", {"operation": "trim", "inputs": ["clip.mkv"], "target": "", "max_size_mb": 0,
                                                 "width": 0, "start": "1:05", "end": "1:40", "quality": ""}),
    ("распакуй архив", {"operation": "extract", "inputs": [], "target": "", "max_size_mb": 0, "width": 0, "start": "",
                        "end": "", "quality": ""}),
    ("упакуй папку notes в 7z", {"operation": "archive", "inputs": ["notes"], "target": "7z", "max_size_mb": 0,
                                 "width": 0, "start": "", "end": "", "quality": ""}),
]

# what ships a tool, per family (Fedora's own ffmpeg build is ffmpeg-free)
TOOLS = {
    "ffmpeg": {"arch": "ffmpeg", "debian": "ffmpeg", "fedora": "ffmpeg-free"},
    "magick": {"arch": "imagemagick", "debian": "imagemagick", "fedora": "ImageMagick"},
    "bsdtar": {"arch": "libarchive", "debian": "libarchive-tools", "fedora": "bsdtar"},
    "7z": {"arch": "7zip", "debian": "7zip", "fedora": "p7zip-plugins"},
}

DIRS = {"загрузк": "~/Downloads", "download": "~/Downloads", "документ": "~/Documents", "document": "~/Documents",
        "картинк": "~/Pictures", "изображен": "~/Pictures", "фото": "~/Pictures", "picture": "~/Pictures",
        "видео": "~/Videos", "video": "~/Videos", "музык": "~/Music", "music": "~/Music",
        "рабоч": "~/Desktop", "desktop": "~/Desktop"}


@dataclass
class Plan:
    operation: str
    inputs: list[str] = field(default_factory=list)
    target: str = ""
    max_size_mb: float = 0
    width: int = 0
    start: str = ""
    end: str = ""
    quality: str = ""

    @classmethod
    def from_json(cls, data: dict) -> "Plan":
        op = data.get("operation") if data.get("operation") in OPERATIONS else "convert"
        target = str(data.get("target") or "").lower().strip().lstrip(".")
        target = {"jpeg": "jpg", "tif": "tiff", "tgz": "tar.gz", "gz": "tar.gz", "zstd": "tar.zst",
                  "matroska": "mkv", "gifka": "gif"}.get(target, target)
        try:
            size = max(0.0, float(data.get("max_size_mb") or 0))
            width = max(0, int(data.get("width") or 0))
        except (TypeError, ValueError):
            size, width = 0.0, 0
        quality = data.get("quality") if data.get("quality") in QUALITY else ""
        inputs = [str(x).strip() for x in data.get("inputs") or [] if str(x).strip()]
        plan = cls(op, inputs, target, size, width, str(data.get("start") or "").strip(),
                   str(data.get("end") or "").strip(), quality)
        if plan.operation == "convert" and plan.target == "gif":
            plan.operation = "to_gif"
        return plan


@dataclass
class Step:
    argv: list[list[str]]  # usually one command; mkdir + unpack for archives
    source: str
    output: str


def kind(path: str) -> str:
    name = Path(path).name.lower()
    if re.search(r"\.tar\.(gz|xz|zst|bz2|lz4)$", name):
        return "archive"
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return ("video" if ext in VIDEO else "audio" if ext in AUDIO else "image" if ext in IMAGE
            else "archive" if ext in ARCHIVE else "")


def wanted_kinds(plan: Plan) -> set[str]:
    """Which files the operation applies to (for picking files when none were named)."""
    op, tgt = plan.operation, plan.target
    if op == "extract":
        return {"archive"}
    if op == "archive":
        return set()
    if op in ("extract_audio", "remove_audio", "to_gif"):
        return {"video"}
    if op == "trim":
        return {"video", "audio"}
    if op in ("resize", "strip_metadata"):
        return {"image", "video"}
    if tgt in IMAGE:
        return {"image"}
    if tgt in AUDIO:
        return {"audio", "video"}
    if tgt in VIDEO:
        return {"video"}
    return {"video", "audio", "image"}


def _stem(path: Path) -> str:
    name = path.name
    m = re.match(r"(.+?)\.tar\.(gz|xz|zst|bz2|lz4)$", name, re.I)
    return m.group(1) if m else path.stem


def free_path(path: Path, taken: set[str] | None = None) -> Path:
    """`name.ext`, or `name (1).ext`, `name (2).ext` … — never an existing file."""
    taken = taken or set()
    suffix = "".join(path.suffixes[-2:]) if re.search(r"\.tar\.\w+$", path.name) else path.suffix
    base = path.name[: len(path.name) - len(suffix)] if suffix else path.name
    cand, n = path, 1
    while cand.exists() or str(cand) in taken:
        cand = path.with_name(f"{base} ({n}){suffix}")
        n += 1
    return cand


def seconds(text: str) -> float | None:
    """'1:05' -> 65, '01:02:03' -> 3723, '90' -> 90, '1.5' -> 1.5; None when unreadable."""
    text = text.strip().replace(",", ".")
    if not text:
        return None
    if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?", text):
        return None
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return total


def _clock(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def video_kbps(size_mb: float, duration: float, audio_kbps: int = 128) -> int:
    """Video bitrate that fits `size_mb` (a few % left for the container)."""
    total = size_mb * 8 * 1024 * 1024 / duration / 1000 * 0.95
    return int(total - audio_kbps)


def probe(path: str) -> dict:
    """Duration, codecs and width via ffprobe ({} without ffprobe or for a broken file)."""
    if not shutil.which("ffprobe"):
        return {}
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration:stream=codec_type,codec_name,width,height", "-of", "json", path],
                             capture_output=True, text=True, timeout=20).stdout
        data = json.loads(out or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    res: dict = {"duration": float(data.get("format", {}).get("duration") or 0)}
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and "video" not in res:
            res["video"], res["width"] = s.get("codec_name", ""), s.get("width") or 0
        elif s.get("codec_type") == "audio" and "audio" not in res:
            res["audio"] = s.get("codec_name", "")
    return res


_ENCODERS: set[str] | None = None


def encoders() -> set[str]:
    global _ENCODERS
    if _ENCODERS is None:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout \
            if shutil.which("ffmpeg") else ""
        _ENCODERS = set(re.findall(r"^\s*[VAS][\w.]{5}\s+(\S+)", out, re.M))
    return _ENCODERS


def magick() -> str:
    """ImageMagick 7 is `magick`; Debian/Ubuntu still ship 6, where it's `convert`."""
    return "magick" if shutil.which("magick") else "convert"


# --- ffmpeg ------------------------------------------------------------------------------------

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-n"]
CRF = {"low": 30, "medium": 26, "high": 21, "": 23}
AUDIO_KBPS = {"low": 96, "medium": 128, "high": 192, "": 160}


def _h264(crf: int, kbps: int = 0) -> list[str]:
    if "libx264" in encoders() or not encoders():
        rate = ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.4)}k", "-bufsize", f"{kbps * 2}k"] if kbps else ["-crf", str(crf)]
        return ["-c:v", "libx264", "-preset", "medium", *rate, "-pix_fmt", "yuv420p"]
    # Fedora's ffmpeg-free has no x264: OpenH264 only takes a bitrate
    return ["-c:v", "libopenh264", "-b:v", f"{kbps or 2500}k"]


def _video_codec(container: str, crf: int, kbps: int = 0) -> list[str]:
    if container == "webm":
        rate = ["-b:v", f"{kbps}k"] if kbps else ["-crf", str(crf + 10), "-b:v", "0"]
        return ["-c:v", "libvpx-vp9", *rate, "-row-mt", "1"]
    return _h264(crf, kbps)


def _audio_codec(fmt: str, kbps: int = 0, quality: str = "") -> list[str]:
    kbps = kbps or AUDIO_KBPS[quality]
    return {
        "mp3": ["-c:a", "libmp3lame", "-b:a", f"{kbps}k"],
        "ogg": ["-c:a", "libvorbis", "-b:a", f"{kbps}k"],
        "opus": ["-c:a", "libopus", "-b:a", f"{min(kbps, 160)}k"],
        "webm": ["-c:a", "libopus", "-b:a", f"{min(kbps, 160)}k"],
        "flac": ["-c:a", "flac"],
        "wav": ["-c:a", "pcm_s16le"],
        "aiff": ["-c:a", "pcm_s16be"],
    }.get(fmt, ["-c:a", "aac", "-b:a", f"{kbps}k"])


def _scale(width: int, source_width: int = 0) -> list[str]:
    """Only ever down: a picture narrower than asked stays as it is."""
    return ["-vf", f"scale={width}:-2"] if width and (not source_width or width < source_width) else []


def _cut(plan: Plan) -> tuple[list[str], list[str]]:
    """(-ss before -i for a fast seek, -t after it)."""
    start, end = seconds(plan.start), seconds(plan.end)
    before = ["-ss", f"{start:g}"] if start else []
    after = ["-t", f"{end - (start or 0):g}"] if end and end > (start or 0) else []
    return before, after


def video_step(plan: Plan, src: str, info: dict, taken: set[str]) -> Step:
    p, op = Path(src), plan.operation
    ext = p.suffix.lower().lstrip(".")
    q = plan.quality
    if op == "extract_audio" or (op == "convert" and plan.target in AUDIO):
        fmt = plan.target if plan.target in AUDIO else ("m4a" if info.get("audio") == "aac" else "mp3")
        codec = ["-c:a", "copy"] if fmt == "m4a" and info.get("audio") == "aac" else _audio_codec(fmt, quality=q)
        out = free_path(p.with_suffix(f".{fmt}"), taken)
        return Step([[*FF, "-i", src, "-vn", *codec, str(out)]], src, str(out))
    if op == "to_gif":
        before, after = _cut(plan)
        w = plan.width or 480
        out = free_path(p.with_suffix(".gif"), taken)
        graph = f"fps=12,scale={w}:-2:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse"
        return Step([[*FF, *before, "-i", src, *after, "-filter_complex", graph, "-loop", "0", str(out)]], src, str(out))
    if op == "remove_audio":
        out = free_path(p.with_name(f"{p.stem}-noaudio{p.suffix}"), taken)
        return Step([[*FF, "-i", src, "-an", "-c:v", "copy", str(out)]], src, str(out))
    if op == "strip_metadata":
        out = free_path(p.with_name(f"{p.stem}-clean{p.suffix}"), taken)
        return Step([[*FF, "-i", src, "-map_metadata", "-1", "-map_chapters", "-1", "-c", "copy", str(out)]], src, str(out))
    if plan.target in IMAGE:
        raise ValueError(t(f"{p.name}: кадры из видео в картинки пока не делаю", f"{p.name}: video frames to images isn't supported yet"))
    container = plan.target if plan.target in VIDEO else (ext if ext in ("mp4", "mkv", "mov", "webm") else "mp4")
    if op == "convert":
        out = free_path(p.with_suffix(f".{container}"), taken)
        # mkv holds any codec, mp4/mov the common ones: then no re-encoding at all
        copy_ok = container == "mkv" or (container in ("mp4", "mov", "m4v") and info.get("video") in ("h264", "hevc", "av1")
                                         and info.get("audio", "aac") in ("aac", "mp3", "ac3", "eac3", "opus"))
        codec = ["-c", "copy"] if copy_ok and not plan.width else \
            [*_video_codec(container, CRF[q]), *_scale(plan.width, info.get("width", 0)), *_audio_codec("webm" if container == "webm" else "aac", quality=q)]
        return Step([[*FF, "-i", src, *codec, str(out)]], src, str(out))
    if op == "trim":
        before, after = _cut(plan)
        out = free_path(p.with_name(f"{p.stem}-cut.{container}"), taken)
        return Step([[*FF, *before, "-i", src, *after, *_video_codec(container, 20), *_scale(plan.width, info.get("width", 0)),
                      *_audio_codec("webm" if container == "webm" else "aac"), str(out)]], src, str(out))
    # compress / resize
    if op == "compress" and plan.max_size_mb and p.stat().st_size <= plan.max_size_mb * 1024 * 1024:
        raise ValueError(t(f"{p.name} уже меньше {plan.max_size_mb:g} МБ", f"{p.name} is already under {plan.max_size_mb:g} MB"))
    kbps = 0
    if op == "compress" and plan.max_size_mb and info.get("duration"):
        kbps = video_kbps(plan.max_size_mb, info["duration"], 64)
        if kbps < 60:
            raise ValueError(t(f"{p.name}: в {plan.max_size_mb:g} МБ при длине {_clock(info['duration'])} "
                               "не уместить видео — укажи размер побольше или вырежи кусок",
                               f"{p.name}: {plan.max_size_mb:g} MB for {_clock(info['duration'])} can't hold a "
                               "video — ask for a bigger size or cut a piece"))
    width = plan.width
    if op == "compress" and not width:
        # few bits spread over many pixels look worse than a sharp smaller picture
        fit = 480 if kbps and kbps < 300 else 640 if kbps and kbps < 700 else 1280 if kbps and kbps < 1500 else 1920
        if (info.get("width") or 0) > fit and q != "high":
            width = fit
    tag = f"-{width}px" if op == "resize" and width else "-small"
    out = free_path(p.with_name(f"{p.stem}{tag}.{container}"), taken)
    crf = CRF[q] + (4 if op == "compress" and not q else 0)
    audio = _audio_codec("webm" if container == "webm" else "aac", 64 if kbps else 0, q)
    return Step([[*FF, "-i", src, *_video_codec(container, crf, kbps), *_scale(width, info.get("width", 0)), *audio, str(out)]], src, str(out))


def audio_step(plan: Plan, src: str, info: dict, taken: set[str]) -> Step:
    p, op = Path(src), plan.operation
    ext = p.suffix.lower().lstrip(".")
    fmt = plan.target if plan.target in AUDIO else (ext if ext in AUDIO and ext not in ("wma", "alac") else "mp3")
    kbps = 0
    if op == "compress":
        if plan.max_size_mb and info.get("duration"):
            kbps = max(32, min(320, int(plan.max_size_mb * 8 * 1024 * 1024 / info["duration"] / 1000 * 0.97)))
        if fmt in ("flac", "wav", "aiff"):
            fmt = "mp3" if not plan.target else fmt  # lossless can't be squeezed
    before, after = _cut(plan) if op == "trim" else ([], [])
    tag = {"compress": "-small", "trim": "-cut", "strip_metadata": "-clean"}.get(op, "")
    out = free_path(p.with_name(f"{p.stem}{tag}.{fmt}"), taken)
    if op == "strip_metadata":
        return Step([[*FF, "-i", src, "-map_metadata", "-1", "-c", "copy", str(out)]], src, str(out))
    return Step([[*FF, *before, "-i", src, *after, "-vn", *_audio_codec(fmt, kbps, plan.quality), str(out)]], src, str(out))


# --- images ------------------------------------------------------------------------------------

IMG_QUALITY = {"low": 60, "medium": 75, "high": 88, "": 82}


def image_step(plan: Plan, src: str, taken: set[str]) -> Step:
    p, op = Path(src), plan.operation
    ext = p.suffix.lower().lstrip(".")
    fmt = plan.target if plan.target in IMAGE else ("jpg" if ext in ("heic", "heif") else ext)
    im = magick()
    args: list[str] = ["-auto-orient"]
    tag = ""
    if op == "resize":
        if not plan.width:
            raise ValueError(t("до какой ширины уменьшить? Например: «уменьши до 1280»",
                               "to what width? For example: “resize to 1280”"))
        args += ["-resize", f"{plan.width}x>"]
        tag = f"-{plan.width}px"
    elif op == "strip_metadata":
        args += ["-strip"]
        tag = "-clean"
    elif op == "compress":
        if plan.max_size_mb and p.stat().st_size <= plan.max_size_mb * 1024 * 1024 and not plan.target:
            raise ValueError(t(f"{p.name} уже меньше {plan.max_size_mb:g} МБ", f"{p.name} is already under {plan.max_size_mb:g} MB"))
        args += ["-strip"]
        if plan.width:
            args += ["-resize", f"{plan.width}x>"]
        if fmt == "png" and not plan.target:
            args += ["-define", "png:compression-level=9"]
        elif plan.max_size_mb and fmt in ("jpg", "jpeg"):
            args += ["-define", f"jpeg:extent={int(plan.max_size_mb * 1024)}kb"]
        else:
            args += ["-quality", str(IMG_QUALITY[plan.quality or "medium"])]
        tag = "-small"
    elif fmt in ("jpg", "jpeg", "webp", "avif", "heic", "jxl"):
        args += ["-quality", str(IMG_QUALITY[plan.quality])]
    out = free_path(p.with_name(f"{p.stem}{tag}.{fmt}"), taken)
    first = "[0]" if ext in ("gif", "tif", "tiff", "ico") and fmt not in ("gif", "tif", "tiff") else ""
    return Step([[im, src + first, *args, str(out)]], src, str(out))


# --- archives ----------------------------------------------------------------------------------


def top_entries(src: str) -> set[str]:
    """First path parts inside the archive (empty when it can't be listed)."""
    tool = ["bsdtar", "-tf", src] if shutil.which("bsdtar") else ["tar", "-tf", src]
    try:
        out = subprocess.run(tool, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {line.lstrip("./").split("/")[0] for line in out.splitlines() if line.lstrip("./")}


def extract_step(src: str, taken: set[str]) -> Step:
    p = Path(src)
    ext = p.suffix.lower()
    tops = top_entries(src) if ext not in (".7z", ".rar") or not shutil.which("7z") else set()
    if len(tops) == 1:  # the archive has its own folder: unpack beside it, not into folder/folder
        inner = p.parent / next(iter(tops))
        if not inner.exists() and str(inner) not in taken:
            tool = ["bsdtar", "-xf", src, "-C", str(p.parent)] if shutil.which("bsdtar") else ["tar", "-xf", src, "-C", str(p.parent)]
            return Step([tool], src, str(inner))
    dest = free_path(p.with_name(_stem(p)), taken)
    if ext in (".7z", ".rar") and shutil.which("7z"):
        cmd = ["7z", "x", f"-o{dest}", src]
    elif shutil.which("bsdtar"):
        cmd = ["bsdtar", "-xf", src, "-C", str(dest)]
    elif ext == ".zip" and shutil.which("unzip"):
        cmd = ["unzip", "-q", src, "-d", str(dest)]
    else:
        cmd = ["tar", "-xf", src, "-C", str(dest)]
    return Step([["mkdir", "-p", str(dest)], cmd] if cmd[0] != "7z" else [cmd], src, str(dest))


def archive_step(plan: Plan, paths: list[str], taken: set[str]) -> Step:
    fmt = plan.target if plan.target in PACK else "zip"
    first = Path(paths[0])
    parent = first.parent
    same = all(Path(x).parent == parent for x in paths)
    name = _stem(first) if len(paths) == 1 else (parent.name if same and parent.name else "archive")
    out = free_path(parent / f"{name}.{fmt}", taken)
    if shutil.which("bsdtar"):
        items = [Path(x).name for x in paths] if same else paths
        cmd = ["bsdtar", "-a", "-cf", str(out), *(["-C", str(parent)] if same else []), *items]
    elif fmt == "7z":
        cmd = ["7z", "a", str(out), *paths]
    elif fmt == "zip":
        cmd = ["zip", "-r", str(out), *paths]
    else:
        cmd = ["tar", "-caf", str(out), *paths]
    return Step([cmd], ", ".join(Path(x).name for x in paths), str(out))


# --- files -------------------------------------------------------------------------------------


def resolve(inputs: list[str], cwd: Path, home: Path | None = None) -> tuple[list[str], list[str]]:
    """(existing paths, names that matched nothing). Patterns are expanded; a bare name is also looked
    for in the usual folders (Downloads, Videos, …)."""
    home = home or Path.home()
    found, missing = [], []
    for raw in inputs:
        text = raw.strip().strip("\"'«»")
        for word, folder in DIRS.items():  # "загрузки/*.heic" from a model that kept the Russian word
            if text.lower().startswith(word):
                text = folder + text[text.find("/"):] if "/" in text else folder
                break
        path = Path(text.replace("~", str(home), 1) if text.startswith("~") else text)
        if not path.is_absolute():
            path = cwd / path
        if any(c in text for c in "*?["):
            hits = sorted(glob.glob(str(path)))
            found += [h for h in hits if Path(h).is_file()]
            if not hits:
                missing.append(raw)
            continue
        if path.exists():
            found.append(str(path))
            continue
        near = [d / Path(text).name for d in (cwd, home / "Downloads", home / "Videos", home / "Pictures",
                                              home / "Music", home / "Desktop", home / "Documents")]
        hit = next((str(n) for n in near if n.exists()), "")
        if not hit:  # case and extension differ: "Video" for "video.MP4"
            low = Path(text).name.lower()
            for d in near:
                if d.parent.is_dir():
                    hit = next((str(f) for f in d.parent.iterdir() if f.name.lower() == low or f.stem.lower() == low), "")
                if hit:
                    break
        (found.append(hit) if hit else missing.append(raw))
    return list(dict.fromkeys(found)), missing


def candidates(kinds: set[str], cwd: Path, home: Path | None = None, limit: int = 40) -> list[Path]:
    """Files of these kinds in the current folder and Downloads, newest first."""
    home = home or Path.home()
    seen, res = set(), []
    for d in dict.fromkeys([cwd, home / "Downloads", home / "Videos", home / "Pictures", home / "Music"]):
        try:
            files = [f for f in d.iterdir() if f.is_file() and kind(f.name) in kinds]
        except OSError:
            continue
        for f in files:
            if f.resolve() not in seen:
                seen.add(f.resolve())
                res.append(f)
    return sorted(res, key=lambda f: -f.stat().st_mtime)[:limit]


def steps(plan: Plan, paths: list[str]) -> tuple[list[Step], list[str]]:
    """(steps, problems): one step per file; files that the operation can't apply to become problems."""
    taken: set[str] = set()
    if plan.operation == "archive":
        return ([archive_step(plan, paths, taken)] if paths else []), []
    res, problems = [], []
    for src in paths:
        k = kind(src)
        try:
            if plan.operation == "extract" or (k == "archive" and plan.operation == "convert"):
                if k != "archive":
                    raise ValueError(t(f"{Path(src).name} — не архив", f"{Path(src).name} isn't an archive"))
                step = extract_step(src, taken)
            elif k == "video":
                step = video_step(plan, src, probe(src), taken)
            elif k == "audio":
                if plan.operation in ("to_gif", "remove_audio", "resize") or plan.target in IMAGE | VIDEO:
                    raise ValueError(t(f"{Path(src).name} — это звук, а не видео", f"{Path(src).name} is audio, not video"))
                step = audio_step(plan, src, probe(src), taken)
            elif k == "image":
                if plan.operation in ("trim", "extract_audio", "remove_audio", "to_gif") or plan.target in VIDEO | AUDIO:
                    raise ValueError(t(f"{Path(src).name} — картинка, это с ней не сделать",
                                       f"{Path(src).name} is an image, that can't be done to it"))
                step = image_step(plan, src, taken)
            else:
                raise ValueError(t(f"{Path(src).name}: не знаю, что это за формат", f"{Path(src).name}: unknown format"))
        except ValueError as e:
            problems.append(str(e))
            continue
        taken.add(step.output)
        res.append(step)
    return res, problems


def tools_needed(all_steps: list[Step], has_heic: bool) -> list[str]:
    """Programs the steps call that aren't installed (as TOOLS keys)."""
    need = []
    for s in all_steps:
        for cmd in s.argv:
            prog = "magick" if cmd[0] in ("magick", "convert") else cmd[0]
            if prog in TOOLS and not shutil.which(cmd[0]) and prog not in need:
                need.append(prog)
    if has_heic and "magick" not in need and not heic_ok():
        need.append("heic")
    return need


def heic_ok() -> bool:
    if not shutil.which(magick()):
        return True  # reported as missing ImageMagick instead
    out = subprocess.run([magick(), "-list", "format"], capture_output=True, text=True).stdout
    return bool(re.search(r"^\s*HEIC\*?\s+\S+\s+r", out, re.M))


def package_for(tool: str, family: str) -> str:
    if tool == "heic":
        return {"arch": "libheif", "debian": "libheif-plugin-libde265", "fedora": "libheif"}.get(family, "libheif")
    return TOOLS[tool].get(family, TOOLS[tool]["arch"])
