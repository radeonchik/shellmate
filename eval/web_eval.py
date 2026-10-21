"""End-to-end check of answers from the web: the real path (router -> favored wikis / web search ->
quotes), on questions from many fields with different kinds of answers. An answer counts as right
when its quotes contain the key fact.

    uv run eval/web_eval.py                      # the shellmate config (its model)
    uv run eval/web_eval.py --config jopa -v     # another assistant name's config; print the quotes
    uv run eval/web_eval.py -k платон -k omelet  # only questions containing these words

Search results are cached for a day, so a second run spends no search credits and measures only
the ranking and quote picking.
"""

import argparse
import contextlib
import re
import statistics
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shellmate import cli, config, evidence, router, skills_knowledge, ui, web  # noqa: E402

# (question, kind of answer, any of these in the quotes = the key fact is there[, intents that answer it too])
CASES = [
    ("какая основа философии Платона", "concept", ["идей", "idea", "form", "форм", "эйдос"]),
    ("годы жизни Пушкина", "dates", ["1799", "1837"]),
    ("когда началась Первая мировая война", "date", ["1914"]),
    ("столица Австралии", "place", ["canberra", "канберр"]),
    ("сколько костей в теле взрослого человека", "number", ["206"]),
    ("почему небо голубое", "why", ["rayleigh", "рэле", "scatter", "рассеи", "рассея"]),
    ("что такое фотосинтез", "definition", ["light", "свет", "chlorophyll", "хлорофил", "glucose", "глюкоз"]),
    ("кто написал «Мастера и Маргариту»", "person", ["bulgakov", "булгаков"]),
    ("при какой температуре кипит вода на Эвересте", "number", ["68", "69", "70", "71"]),
    ("какая самая длинная река в мире", "place", ["nile", "нил", "amazon", "амазон"]),
    ("what is the speed of light", "number", ["299,792", "299 792", "299792", "300,000", "300 000", "186,000"]),
    ("how many players are on a football team", "number", ["11", "eleven"]),
    ("who discovered penicillin", "person", ["fleming", "флеминг"]),
    ("чем вирус отличается от бактерии", "comparison", ["клетк", "cell", "антибиот", "antibiotic", "живой", "living"]),
    ("что такое инфляция", "definition", ["цен", "price"]),
    ("формула площади круга", "formula", ["πr", "pi r", "πr²", "r²", "r^2", "r2", "радиус"]),
    ("как приготовить омлет", "howto", ["яйц", "egg"]),
    ("what is the capital of Canada", "place", ["ottawa", "оттав"]),
    ("сколько планет в Солнечной системе", "number", ["8", "eight", "восемь"]),
    ("в каком году основана Москва", "date", ["1147"]),
    ("на каком языке говорит больше всего людей в мире", "fact", ["english", "английск", "mandarin", "chinese", "китайск"]),
    ("what does DNA stand for", "expansion", ["deoxyribonucleic", "дезоксирибонуклеинов"]),
    ("who painted the Mona Lisa", "person", ["leonardo", "леонардо"]),
    ("как работает блокчейн", "explanation", ["блок", "block", "хеш", "hash", "реестр", "ledger"]),
    ("как узнать версию ядра linux", "howto", ["uname"], ["sysinfo"]),  # the system overview shows the kernel
    ("как поменять раскладку клавиатуры в hyprland", "howto", ["kb_layout"]),
    ("какая длина марафонской дистанции", "number", ["42", "26.2"]),
    ("что такое закон Ома", "definition", ["напряжен", "voltage", "сопротивлен", "resistance"]),
    ("в каком году человек впервые высадился на Луну", "date", ["1969"]),
    ("who won the 2018 FIFA World Cup", "fact", ["france", "франци"]),
]


def run_case(app: cli.App, q: str) -> dict:
    """The quotes the user would see (answers only), how the request was routed, which search answered."""
    picks: list = []
    real_pick = evidence.pick

    def pick(*a, **k):
        ev = real_pick(*a, **k)
        picks.append(ev)
        return ev

    routed: list[router.Intent] = []
    real_classify = router.classify

    def classify(*a, **k):
        it = real_classify(*a, **k)
        routed.append(it)
        return it

    quiet = [mock.patch.object(ui, name, lambda *a, **k: None) for name in ("say", "panel", "kv", "problem")]
    quiet += [mock.patch.object(ui, "working", lambda *a, **k: mock.MagicMock()),
              mock.patch.object(skills_knowledge.KnowledgeSkills, "_show", lambda *a, **k: None),  # no translation
              mock.patch.object(evidence, "pick", pick), mock.patch.object(skills_knowledge.evidence, "pick", pick),
              mock.patch.object(router, "classify", classify), mock.patch("builtins.input", lambda *a: "n"),
              mock.patch.object(cli.state, "save_context", lambda *a, **k: None)]  # keep the user's conversation
    t0 = time.perf_counter()
    with contextlib.ExitStack() as stack:
        for p in quiet:
            stack.enter_context(p)
        try:
            app.handle(q)
        except Exception as e:  # a crash is a result too
            return {"error": repr(e), "time": time.perf_counter() - t0, "answers": [], "intent": "?", "text": ""}
    answers = [s for ev in picks for s in ev.answers]
    return {"answers": answers, "time": time.perf_counter() - t0, "provider": web.LAST_PROVIDER,
            "intent": routed[-1].intent if routed else "?", "text": routed[-1].text if routed else ""}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="shellmate", help="assistant name whose config to use")
    ap.add_argument("-m", "--model", help="override the config's model")
    ap.add_argument("-k", action="append", default=[], help="only questions containing this (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    cfg = config.load(args.config)
    if args.model:
        cfg.model = args.model
    web.configure(cfg.search_order, cfg.tavily_key, cfg.brave_key, cfg.searx_url)
    app = cli.App(cfg, dry_run=True)
    cases = [c for c in CASES if not args.k or any(k.lower() in c[0].lower() for k in args.k)]
    right = found_wrong = 0
    times = []
    for q, kind, keys, *also in cases:
        r = run_case(app, q)
        text = " ".join(s.text for s in r["answers"]).lower()
        ok = any(k.lower() in text for k in keys) or r["intent"] in (also[0] if also else [])
        right += ok
        found_wrong += bool(r["answers"]) and not ok
        times.append(r["time"])
        mark = "✓" if ok else ("≈" if r["answers"] else "✗")
        print(f"{mark} {r['time']:5.1f}s  {r['intent']:8} {kind:11} {q}" + (f"   [{r['error']}]" if "error" in r else ""))
        if args.verbose or not ok:
            print(f"      → {r['text']!r} via {r.get('provider') or '—'}")
            for s in r["answers"][:3]:
                print(f"      · {re.sub(chr(10), ' ⏎ ', s.text)[:150]}  ({s.src[:60]})")
    n = len(cases)
    print(f"\nверно {right}/{n} ({right / n:.0%}) · нашёл, но не то: {found_wrong} · не нашёл: {n - right - found_wrong}"
          f" · среднее {statistics.mean(times):.1f}s, медиана {statistics.median(times):.1f}s")


if __name__ == "__main__":
    main()
