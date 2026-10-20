"""Compares models on intent routing.

    uv run eval/run_eval.py qwen3.5:4b bonsai-2:4b bonsai-2:2b
    uv run eval/run_eval.py -v bonsai-2:4b     # also print every failure
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shellmate import llm, router  # noqa: E402

CASES = Path(__file__).with_name("cases.jsonl")


def score_case(case: dict, got: router.Intent) -> tuple[bool, float]:
    allowed = case["intent"] if isinstance(case["intent"], list) else [case["intent"]]
    intent_ok = got.intent in allowed
    groups = case["pkgs"]
    if not groups:
        return intent_ok, 1.0
    hit = sum(any(name in got.packages for name in g) for g in groups)
    return intent_ok, hit / len(groups)


def run_model(url: str, model: str, cases: list[dict], verbose: bool) -> dict:
    router.classify(url, model, "install firefox")  # warm-up: exclude model load time
    intent_hits, pkg_scores, times, fails = 0, [], [], []
    for case in cases:
        t = time.perf_counter()
        try:
            got = router.classify(url, model, case["q"])
        except llm.LLMError as e:
            got = router.Intent("error", text=str(e))
        times.append(time.perf_counter() - t)
        intent_ok, pkg = score_case(case, got)
        intent_hits += intent_ok
        pkg_scores.append(pkg)
        if not intent_ok or pkg < 1:
            fails.append((case, got))
    if verbose:
        for case, got in fails:
            print(f"  ✗ {case['q']!r}\n      ожидал {case['intent']} {case['pkgs']}\n"
                  f"      получил {got.intent} {got.packages} {got.text!r}")
    return {
        "model": model,
        "intent": intent_hits / len(cases),
        "pkgs": statistics.mean(pkg_scores),
        "avg_s": statistics.mean(times),
        "p90_s": sorted(times)[int(len(times) * 0.9) - 1],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]

    results = []
    for model in args.models:
        print(f"→ {model} ({len(cases)} запросов)")
        try:
            results.append(run_model(args.url, model, cases, args.verbose))
        except llm.LLMError as e:
            print(f"  пропускаю: {e}")
        llm.unload(args.url, model)

    print(f"\n{'модель':<22}{'интент':>8}{'пакеты':>8}{'сред.':>8}{'p90':>8}")
    for r in results:
        print(f"{r['model']:<22}{r['intent']:>8.0%}{r['pkgs']:>8.0%}"
              f"{r['avg_s']:>7.2f}s{r['p90_s']:>7.2f}s")


if __name__ == "__main__":
    main()
