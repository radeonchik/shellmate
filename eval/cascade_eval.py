"""Calibrates and checks the fast→big cascade for routing.

Runs every case through both models once, then replays the escalation rule for a grid of
thresholds: accuracy of the cascade, how many requests go to the big model, average latency,
and which mistakes of the fast model slip through.

    uv run eval/cascade_eval.py bonsai-2:4b bonsai-2:9b
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from shellmate import llm, router  # noqa: E402
from run_eval import score_case  # noqa: E402

HERE = Path(__file__).resolve().parent


def load() -> list[dict]:
    cases = []
    for name in ("cases.jsonl", "hard_cases.jsonl"):
        for line in (HERE / name).read_text().splitlines():
            if line.strip():
                cases.append({**json.loads(line), "set": name.split("_")[0].split(".")[0]})
    return cases


def run(url: str, model: str, cases: list[dict]) -> list[tuple[router.Intent, float, bool]]:
    router.classify(url, model, "install firefox")  # load the model
    out = []
    for c in cases:
        t = time.perf_counter()
        it = router.classify(url, model, c["q"])
        dt = time.perf_counter() - t
        intent_ok, pkg = score_case(c, it)
        out.append((it, dt, intent_ok and pkg == 1))
    llm.unload(url, model)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("small")
    ap.add_argument("big")
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    args = ap.parse_args()
    cases = load()
    print(f"{len(cases)} запросов ({sum(c['set'] == 'hard' for c in cases)} трудных)", flush=True)
    small, big = run(args.url, args.small, cases), run(args.url, args.big, cases)

    def report(name: str, picks: list[bool], esc: list[bool]) -> tuple:
        ok = [big[i][2] if e else small[i][2] for i, e in enumerate(esc)]
        hard = [o for o, c in zip(ok, cases) if c["set"] == "hard"]
        t = sum(small[i][1] + (big[i][1] if e else 0) for i, e in enumerate(esc)) / len(cases)
        return (name, sum(ok) / len(ok), sum(hard) / len(hard), sum(esc) / len(esc), t)

    rows = [report(f"только {args.small}", [], [False] * len(cases)),
            report(f"только {args.big}", [], [True] * len(cases))]
    grid = []
    for ti in (0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        for tp in (0, 0.3, 0.5, 0.6, 0.7, 0.8, 0.9):
            esc = [bool(router.doubts(small[i][0], c["q"], ti, tp)) for i, c in enumerate(cases)]
            # big model's time is only paid on escalation, small model's always
            grid.append((report(f"каскад: действие<{ti:.2f}, пакет<{tp:.2f}", [], esc), ti, tp, esc))
    print(f"\n{'вариант':<38}{'всего':>7}{'трудные':>9}{'на 9B':>7}{'время':>8}")
    for name, acc, hard, rate, t in rows:
        print(f"{name:<38}{acc:>7.0%}{hard:>9.0%}{rate:>7.0%}{t:>7.2f}s")
    best = sorted(grid, key=lambda g: (-g[0][1], g[0][3]))
    shown = set()
    for (name, acc, hard, rate, t), *_ in best:
        key = (round(acc, 3), round(rate, 3))
        if key in shown or len(shown) >= 8:
            continue
        shown.add(key)
        print(f"{name:<38}{acc:>7.0%}{hard:>9.0%}{rate:>7.0%}{t:>7.2f}s")
    rule_only = next(g for g in grid if g[1] == 0 and g[2] == 0)
    print(f"{'(только проверки кодом, без вероятностей)':<38}{rule_only[0][1]:>7.0%}{rule_only[0][2]:>9.0%}"
          f"{rule_only[0][3]:>7.0%}{rule_only[0][4]:>7.2f}s")

    (_, acc, _, rate, _), ti, tp, esc = best[0]
    print(f"\nлучший порог: действие {ti}, пакет {tp}. Ошибки {args.small}, которые каскад НЕ поймал:")
    for i, c in enumerate(cases):
        if not small[i][2] and not esc[i]:
            it = small[i][0]
            print(f"  {c['q']!r}: {it.intent} {it.packages} (уверенность {it.intent_p:.2f}/{it.packages_p:.2f})")
    print("ошибки, которые поймал (и что ответила большая):")
    for i, c in enumerate(cases):
        if not small[i][2] and esc[i]:
            s, b = small[i][0], big[i][0]
            print(f"  {c['q']!r}: {s.intent} {s.packages} → {b.intent} {b.packages} "
                  f"{'✓' if big[i][2] else '✗'}  ({'; '.join(router.doubts(s, c['q'], ti, tp))})")


if __name__ == "__main__":
    main()
