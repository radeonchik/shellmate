"""Hardware check and model recommendation, after the approach of llmfit
(github.com/AlexsJones/llmfit): memory fit = required / available pool, speed ≈
memory bandwidth / model size × 0.55, verdict by pool utilization."""

import json
import shutil
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from shellmate.packages import run_quiet
from shellmate.system import meminfo

GB = 1e9
EFFICIENCY = 0.55  # llmfit's measured share of theoretical bandwidth that decoding achieves
OVERHEAD = 0.6 * GB  # runtime + 8k-token KV cache (Ollama reports ~+0.05 GB for qwen3.5:4b)

# per-backend bandwidth fallbacks, GB/s (llmfit's constants)
BACKEND_BW = {"cuda": 220, "rocm": 180, "cpu": 70}
# known laptop/desktop GPUs, GB/s; matched as substrings of the GPU name
GPU_BW = {"5090": 1792, "5080": 960, "5070 ti": 896, "5070": 672, "5060 ti": 448, "5060": 448,
          "5050 laptop": 384, "5050": 320, "4090": 1008, "4080": 717, "4070 ti": 504, "4070": 504,
          "4060 ti": 288, "4060": 272, "4050": 192, "3090": 936, "3080": 760, "3070": 448,
          "3060": 360, "3050": 224, "2080": 448, "2070": 448, "2060": 336, "1660": 192,
          "1650": 128, "7900 xtx": 960, "7900": 800, "7800": 624, "7700": 432, "7600": 288,
          "6800": 512, "6700": 384, "6600": 224}


@dataclass
class Candidate:
    tag: str
    size: float  # bytes, fallback when the registry is unreachable
    quality: int  # 0-100, our rough judgement of routing/answer quality
    note: str = ""


# Only models that fit this assistant's job: tool-calling/JSON + multilingual, ≤ 9B.
CATALOG = [
    # first four scored from eval/bench.py (routing 87 cases, quote selection 10 cases,
    # 2026-10-20, 8 GB laptop GPU); the rest are estimates
    Candidate("qwen3.5:9b", 6.55 * GB, 89, "самые точные цитаты, но 5.5 ГБ VRAM и медленнее"),
    Candidate("bonsai-2:9b", 3.18 * GB, 88, "тернарная; лучшая точность в тестах shellmate"),
    Candidate("qwen3.5:4b", 3.32 * GB, 82),
    Candidate("bonsai-2:4b", 1.58 * GB, 80, "тернарная; быстрая и лёгкая, тесты почти без ошибок"),
    Candidate("qwen3.5:2b", 2.68 * GB, 62),
    Candidate("bonsai-2:2b", 0.91 * GB, 52, "тернарная, свежая — мало независимых тестов"),
    Candidate("qwen3.5:0.8b", 1.32 * GB, 38, "только для очень слабого железа"),
]


@dataclass
class Hardware:
    ram_total: int
    ram_avail: int
    cores: int
    gpu_name: str = ""
    vram_total: int = 0
    vram_free: int = 0
    backend: str = "cpu"
    bandwidth: float = BACKEND_BW["cpu"]  # GB/s of the memory the model would live in


def _ollama_loaded(url: str) -> tuple[int, int]:
    """Memory currently held by Ollama itself (vram, ram) — it's ours to reuse, not taken."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/ps", timeout=3) as r:
            models = json.load(r).get("models", [])
    except (OSError, ValueError):
        return 0, 0
    vram = sum(m.get("size_vram", 0) for m in models)
    return vram, sum(m.get("size", 0) for m in models) - vram


def detect(ollama_url: str) -> Hardware:
    mem = meminfo()
    own_vram, own_ram = _ollama_loaded(ollama_url)
    hw = Hardware(mem["MemTotal"], mem["MemAvailable"] + own_ram, len(re.findall(
        r"^processor", Path("/proc/cpuinfo").read_text(), re.M)))
    q = run_quiet(["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
                   "--format=csv,noheader,nounits"]).strip().splitlines()
    if q and q[0].count(",") == 2:
        name, total, used = (x.strip() for x in q[0].split(","))
        hw.gpu_name, hw.backend = name, "cuda"
        hw.vram_total = int(total) * 1024**2
        hw.vram_free = hw.vram_total - int(used) * 1024**2 + own_vram
    else:
        for card in Path("/sys/class/drm").glob("card[0-9]/device"):
            total = (card / "mem_info_vram_total")
            if total.exists() and int(total.read_text()) > 2 * GB:  # discrete AMD
                used = int((card / "mem_info_vram_used").read_text())
                hw.backend, hw.vram_total = "rocm", int(total.read_text())
                hw.vram_free = hw.vram_total - used + own_vram
                hw.gpu_name = re.sub(r".*\[(.+)\].*", r"\1", run_quiet(["lspci", "-s", card.resolve().name]))
                break
    if hw.backend != "cpu":
        name = hw.gpu_name.lower()
        hw.bandwidth = next((bw for key, bw in GPU_BW.items() if key in name), BACKEND_BW[hw.backend])
    return hw


@dataclass
class Fit:
    cand: Candidate
    size: float
    mode: str  # "gpu" | "cpu"
    utilization: float
    verdict: str  # perfect | good | marginal | too_tight
    tps: float
    score: float
    installed: bool = False


VERDICTS = {"perfect": "отлично", "good": "хорошо", "marginal": "впритык", "too_tight": "не влезет"}


def _verdict(util: float, mode: str) -> str:
    v = "perfect" if util <= 0.60 else "good" if util <= 0.85 else "marginal" if util <= 0.98 else "too_tight"
    if mode == "cpu" and v == "perfect":  # llmfit caps CPU inference at "good"
        v = "good"
    return v


def registry_size(tag: str) -> float | None:
    name, _, ver = tag.partition(":")
    req = urllib.request.Request(f"https://registry.ollama.ai/v2/library/{name}/manifests/{ver}",
                                 headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return float(sum(layer["size"] for layer in json.load(r)["layers"]))
    except (OSError, ValueError, KeyError):
        return None


def installed_models(url: str) -> set[str]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=3) as r:
            return {m["name"] for m in json.load(r).get("models", [])}
    except (OSError, ValueError):
        return set()


def evaluate(hw: Hardware, ollama_url: str, online: bool = True) -> list[Fit]:
    have = installed_models(ollama_url)
    res = []
    for c in CATALOG:
        size = (registry_size(c.tag) if online else None) or c.size
        need = size + OVERHEAD
        if hw.vram_total and need <= hw.vram_free * 0.98:
            mode, pool, bw = "gpu", hw.vram_free, hw.bandwidth
        else:  # CPU (Ollama would also split GPU+CPU, but that's barely faster for small models)
            mode, pool, bw = "cpu", hw.ram_avail, BACKEND_BW["cpu"]
        util = need / pool if pool else 9
        verdict = _verdict(util, mode)
        tps = bw * GB / size * EFFICIENCY
        # ~40 tok/s feels instant for an assistant (the long routing prompt also has to be
        # processed, so slower models lag more than their decode speed suggests)
        speed_score = min(100, tps / 40 * 100)
        score = 0.55 * c.quality + 0.45 * speed_score - {"marginal": 25, "too_tight": 1000}.get(verdict, 0)
        res.append(Fit(c, size, mode, util, verdict, tps, score, c.tag in have))
    return sorted(res, key=lambda f: -f.score)


def describe(hw: Hardware) -> list[tuple[str, str]]:
    rows = [("Процессор", f"{hw.cores} потоков"),
            ("Память", f"{hw.ram_total / 1024**3:.1f} ГБ, свободно {hw.ram_avail / 1024**3:.1f} ГБ")]
    if hw.gpu_name:
        rows.append(("Видеокарта", f"{hw.gpu_name}, {hw.vram_total / 1024**3:.1f} ГБ VRAM "
                                   f"(доступно {hw.vram_free / 1024**3:.1f}), ~{hw.bandwidth:.0f} ГБ/с"))
    else:
        rows.append(("Видеокарта", "подходящей нет — модель будет работать на процессоре"))
    return rows


def ollama_install(hw: Hardware | None = None) -> str:
    """The command that installs Ollama on this distro (with the GPU-enabled build where it matters)."""
    from shellmate import backends
    gpu = hw.backend if hw else ("cuda" if shutil.which("nvidia-smi") else "rocm" if Path("/dev/kfd").exists() else "cpu")
    return backends.current().ollama_install(gpu)


def ollama_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/version", timeout=2):
            return True
    except OSError:
        return False
