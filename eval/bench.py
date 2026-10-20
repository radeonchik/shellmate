"""One table to choose a model: routing accuracy, quote selection, speed and real memory use.
Models run one at a time (the previous one is unloaded) so VRAM numbers are honest.

    uv run eval/bench.py qwen3.5:4b bonsai-2:4b bonsai-2:9b
"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from shellmate import llm  # noqa: E402
import quotes_eval  # noqa: E402
import run_eval  # noqa: E402

SPEED_PROMPT = "Explain in about 150 words how a Linux package manager resolves dependencies."


def _api(url: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def speed(url: str, model: str) -> tuple[float, float, float]:
    """(cold load seconds, prompt tokens/s, generation tokens/s) from Ollama's own counters."""
    body = {"model": model, "messages": [{"role": "user", "content": SPEED_PROMPT}], "stream": False,
            "think": False, "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 200}}
    r = _api(url, "/api/chat", body)
    load = r.get("load_duration", 0) / 1e9
    r = _api(url, "/api/chat", body)  # warm run for the speeds
    pp = r["prompt_eval_count"] / (r["prompt_eval_duration"] / 1e9) if r.get("prompt_eval_duration") else 0
    tg = r["eval_count"] / (r["eval_duration"] / 1e9) if r.get("eval_duration") else 0
    return load, pp, tg


def vram(url: str, model: str) -> tuple[float, float]:
    for m in _api(url, "/api/ps").get("models", []):
        if m["name"].split(":")[0] == model.split(":")[0] and (":" not in model or m["name"] == model):
            return m.get("size_vram", 0) / 1e9, (m["size"] - m.get("size_vram", 0)) / 1e9
    return 0.0, 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    args = ap.parse_args()
    cases = [json.loads(line) for line in run_eval.CASES.read_text().splitlines() if line.strip()]
    sources = quotes_eval.load_all()
    for m in args.models:
        llm.unload(args.url, m)
    rows = []
    for model in args.models:
        print(f"→ {model}", flush=True)
        load, pp, tg = speed(args.url, model)
        gpu, cpu = vram(args.url, model)
        t = time.perf_counter()
        r = run_eval.run_model(args.url, model, cases, verbose=False)
        q = quotes_eval.run_quotes(args.url, model, sources)
        rows.append((model, r, q, load, pp, tg, gpu, cpu))
        print(f"   маршрутизация {r['intent']:.0%}/{r['pkgs']:.0%}, цитаты {q['found']}/{q['cases']} "
              f"({q['precision']:.0%}), {time.perf_counter() - t:.0f} с" +
              (f"; промахи в цитатах: {q['fails']}" if q["fails"] else ""), flush=True)
        llm.unload(args.url, model)

    print(f"\n{'модель':<18}{'интент':>7}{'пакеты':>7}{'ответ':>7}{'цитаты':>8}{'точн.':>7}"
          f"{'чтение':>9}{'генер.':>9}{'загрузка':>9}{'VRAM':>7}{'RAM':>6}")
    for model, r, q, load, pp, tg, gpu, cpu in rows:
        print(f"{model:<18}{r['intent']:>7.0%}{r['pkgs']:>7.0%}{r['avg_s']:>6.2f}s"
              f"{q['found']:>5}/{q['cases']}{q['precision']:>7.0%}{pp:>6.0f}т/с{tg:>6.0f}т/с"
              f"{load:>8.1f}s{gpu:>6.1f}G{cpu:>5.1f}G")


if __name__ == "__main__":
    main()
