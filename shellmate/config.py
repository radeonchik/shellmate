"""Config lives in ~/.config/<agent-name>/config.toml, so each name can have its own settings."""

import json
import os
import re

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (Ubuntu 22.04): our config is flat, a tiny reader is enough
    tomllib = None
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Config:
    name: str
    model: str = "bonsai-2:4b"  # fast model: everything by default
    big_model: str = "bonsai-2:9b"  # asked when the fast one is unsure, and for everything with -d
    # escalation thresholds on token probabilities, calibrated in eval/cascade_eval.py
    escalate_intent: float = 0.5
    escalate_packages: float = 0.5
    escalate_quotes: float = 0.7  # eval/quotes_eval.py: 90% -> 95% precision, ~1 in 14 escalated
    ollama_url: str = "http://127.0.0.1:11434"
    use_flatpak: bool = True
    max_results: int = 15
    lang: str = ""  # interface language "ru" | "en"; empty = the desktop's locale
    style: str = ""  # "friendly" | "butler" | "brief" | "custom"; empty = first run
    style_custom: str = ""  # user's own description when style == "custom"
    address: str = ""  # how to address the user, e.g. a name or "boss"
    color: str = "classic"  # ui.PRESETS name or "#rrggbb" / "#rrggbb,#rrggbb"
    docs: list[str] = field(default_factory=list)  # folders used as a local knowledge source
    searx_url: str = ""  # your SearXNG instance (tools/setup-searxng.sh)
    tavily_key: str = ""  # tavily.com, 1000 free searches/month; or env TAVILY_API_KEY
    brave_key: str = ""  # api.search.brave.com, $5 free credit/month; or env BRAVE_API_KEY
    search_order: list[str] = field(default_factory=lambda: ["tavily", "brave", "searxng", "duckduckgo"])
    answer_mode: str = "quotes"  # "quotes": verbatim sentences only; "summary": + short retelling
    # favored sources, searched first for how-to/error questions; empty = chosen for the distro
    # (see trusted.py: ArchWiki on Arch, Ubuntu Docs on Ubuntu, Fedora Docs on Fedora…)
    trusted_sites: list[str] = field(default_factory=list)


def config_path(name: str) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / name / "config.toml"


def _read_flat_toml(text: str) -> dict:
    """key = "string" | 'literal' | true/false | number | ["list", …] — what config.save writes."""
    data = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z_][\w-]*)\s*=\s*(.+?)\s*$", line)
        if not m:
            continue
        key, raw = m.groups()
        for candidate in (raw, raw.split(" #", 1)[0].strip()):
            if candidate in ("true", "false"):
                data[key] = candidate == "true"
                break
            if candidate.startswith("'") and candidate.endswith("'"):
                data[key] = candidate[1:-1]
                break
            try:
                data[key] = json.loads(candidate)
                break
            except ValueError:
                continue
    return data


def load(name: str) -> Config:
    path = config_path(name)
    data = {}
    if path.exists():
        if tomllib:
            with path.open("rb") as f:
                data = tomllib.load(f)
        else:
            data = _read_flat_toml(path.read_text())
    defaults = Config(name=name)
    values = {}
    for k, v in data.items():
        if k == "name" or not hasattr(defaults, k):
            continue
        want = type(getattr(defaults, k))
        if want is float and isinstance(v, (int, str)):  # versions before 0.2.1 saved floats as strings
            try:
                v = float(v)
            except ValueError:
                continue
        if isinstance(v, want):
            values[k] = v
    return Config(name=name, **values)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return json.dumps([str(x) for x in v], ensure_ascii=False)
    return json.dumps(str(v), ensure_ascii=False)  # JSON strings are valid TOML basic strings


def save(cfg: Config) -> Path:
    path = config_path(cfg.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k} = {_toml_value(v)}" for k, v in asdict(cfg).items() if k != "name"]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)  # may hold API keys
    return path
