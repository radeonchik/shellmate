"""Minimal Ollama client (stdlib only)."""

import json
import math
import shutil
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


def chat(url: str, model: str, messages: list[dict], schema: dict | None = None,
         timeout: float = 180, num_ctx: int = 8192) -> str:
    return chat_full(url, model, messages, schema, timeout, num_ctx)["message"]["content"]


def chat_full(url: str, model: str, messages: list[dict], schema: dict | None = None,
              timeout: float = 180, num_ctx: int = 8192, logprobs: bool = False) -> dict:
    """Raw Ollama response; with logprobs=True it has per-token log-probabilities."""
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,  # thinking makes small models 5-10x slower and doesn't help routing
        # same num_ctx on every call: changing it makes Ollama reload the model
        "options": {"temperature": 0, "num_ctx": num_ctx},
        "keep_alive": "15m",
    }
    if schema:
        body["format"] = schema  # Ollama constrains decoding to this JSON schema
    if logprobs:
        body["logprobs"] = True
    try:
        return _post(url, body, timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if "think" in detail:  # model without thinking support rejects the flag
            body.pop("think")
            return _post(url, body, timeout)
        if e.code == 404:
            raise LLMError(f"модель {model!r} не найдена — выполни: ollama pull {model}") from e
        raise LLMError(f"Ollama ответила {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        if not shutil.which("ollama"):
            from shellmate import hardware
            hint = f"Ollama не установлена. Установи:\n  {hardware.ollama_install()}"
        else:
            hint = "Ollama установлена, но не запущена. Запусти:\n  sudo systemctl enable --now ollama"
        raise LLMError(f"нет связи с Ollama ({url}: {e.reason}).\n{hint}") from e


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url.rstrip("/") + "/api/chat",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def span_confidence(content: str, logprobs: list[dict], start: int, end: int) -> float:
    """Lowest token probability inside content[start:end] — how sure the model was about that
    part of its answer (e.g. the intent value). 1.0 when there are no tokens there."""
    pos, lowest = 0, 0.0
    for t in logprobs:
        tok = t.get("token", "")
        if pos < end and pos + len(tok) > start and tok.strip(' "'):
            lowest = min(lowest, t.get("logprob", 0.0))
        pos += len(tok)
    return math.exp(lowest)


def unload(url: str, model: str) -> None:
    """Frees the model's (V)RAM now instead of after keep_alive — two models at once can OOM a laptop GPU."""
    try:
        _post_raw(url, "/api/generate", {"model": model, "keep_alive": 0}, 30)
    except (urllib.error.URLError, OSError):
        pass


def _post_raw(url: str, path: str, body: dict, timeout: float) -> None:
    req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout):
        pass
