"""Ranking page chunks: BM25 (+ the model's answer sketch) vs embedding models, on the web_eval questions.

    uv run eval/rank_eval.py capture            # run web_eval's path once: save every ranking call's
                                                # chunks and each stage's time (needs search + Ollama)
    uv run eval/rank_eval.py compare -e bge-m3  # rank the saved chunks every way; is the key fact in
                                                # the chunks that go on to the quote picker?
    uv run eval/rank_eval.py e2e -e bge-m3      # the whole path with an embedding ranking in it
    … --hard                                    # web_eval's hard why/how questions instead
"""

import argparse
import itertools
import json
import math
import pickle
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from shellmate import cli, config, evidence, rag, router, skills_knowledge, trusted, web  # noqa: E402
import web_eval  # noqa: E402

def saved(hard: bool) -> Path:
    return Path(__file__).resolve().parent / (".rank_calls_hard.pkl" if hard else ".rank_calls.pkl")


def capture(args) -> None:
    cfg = config.load(args.config)
    web.configure(cfg.search_order, cfg.tavily_key, cfg.brave_key, cfg.searx_url)
    app = cli.App(cfg, dry_run=True)
    calls, stages = [], []
    current = {}

    def timed(name, fn):
        def wrapper(*a, **k):
            t0 = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                current["t"][name] += time.perf_counter() - t0
        return wrapper

    real_rank_many = rag.rank_many

    def rank_many(queries, items):
        out = real_rank_many(queries, items)
        calls.append({"q": current["q"], "keys": current["keys"], "queries": list(queries), "items": list(items),
                      "where": "web" if any(it[0].startswith("http") and not trusted.is_trusted(it[0], [trusted.WIKIPEDIA]
                                                                                                   ) for it in items[:3]) else "wiki"})
        return out

    patches = [mock.patch.object(rag, "rank_many", rank_many),
               mock.patch.object(router, "classify", timed("route", router.classify)),
               mock.patch.object(skills_knowledge.KnowledgeSkills, "_answer_shape",
                                 timed("shape (parallel)", skills_knowledge.KnowledgeSkills._answer_shape)),
               mock.patch.object(trusted, "gather", timed("wiki fetch+rank", trusted.gather)),
               mock.patch.object(web, "search_many", timed("web search", web.search_many)),
               mock.patch.object(web, "gather", timed("web fetch+rank", web.gather)),
               mock.patch.object(evidence, "pick", timed("quotes (select+grade)", evidence.pick))]
    for p in patches:
        p.start()
    cases = [c for c in (web_eval.HARD if args.hard else web_eval.CASES) if not args.k or any(k.lower() in c[0].lower() for k in args.k)]
    for q, kind, keys, *_ in cases:
        current.update(q=q, keys=keys, t=defaultdict(float))
        t0 = time.perf_counter()
        r = web_eval.run_case(app, q)
        total = time.perf_counter() - t0
        text = " ".join(s.text for s in r["answers"]).lower()
        ok = any(k.lower() in text for k in keys)
        stages.append({"q": q, "total": total, "ok": ok, **current["t"]})
        print(f"{'✓' if ok else '·'} {total:5.1f}s  " + "  ".join(f"{k} {v:.1f}" for k, v in current["t"].items()) + f"  {q}")
    for p in patches:
        p.stop()
    saved(args.hard).write_bytes(pickle.dumps(calls))
    names = sorted({k for s in stages for k in s if k not in ("q", "total", "ok")})
    print(f"\n{len(calls)} ranking calls saved to {saved(args.hard).name}\nmean time per question, s:")
    print(f"  {'total':24} {statistics.mean(s['total'] for s in stages):.2f}")
    for n in names:
        print(f"  {n:24} {statistics.mean(s.get(n, 0) for s in stages):.2f}")


# --- comparing rankings on the saved calls ------------------------------------------------------

def embed(model: str, texts: list[str], url: str = "http://127.0.0.1:11434") -> list[list[float]]:
    req = urllib.request.Request(url + "/api/embed", json.dumps({"model": model, "input": texts, "keep_alive": "15m"}).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        vecs = json.load(r)["embeddings"]
    return [[x / (math.sqrt(sum(y * y for y in v)) or 1) for x in v] for v in vecs]


QUERY_PREFIX = {  # models trained with an instruction on the query side
    "qwen3-embedding": "Instruct: Given a question, retrieve passages that answer it\nQuery: ",
}


def by_embedding(model: str, query: str, items: list[tuple[str, str]], cache: dict) -> list[tuple[str, str]]:
    prefix = next((p for k, p in QUERY_PREFIX.items() if model.startswith(k)), "")
    todo = [t for _, t in items if (model, t) not in cache]
    for i in range(0, len(todo), 64):
        for t, v in zip(todo[i:i + 64], embed(model, todo[i:i + 64])):
            cache[(model, t)] = v
    qv = embed(model, [prefix + query])[0]
    scores = [sum(a * b for a, b in zip(qv, cache[(model, t)])) for _, t in items]
    return [items[i] for i in sorted(range(len(items)), key=lambda i: -scores[i])]


def interleave(*rankings):
    seen, out = set(), []
    for row in itertools.zip_longest(*rankings):
        for it in row:
            if it is not None and it not in seen:
                seen.add(it)
                out.append(it)
    return out


def compare(args) -> None:
    calls = pickle.loads(saved(args.hard).read_bytes())
    cache: dict = {}
    variants = {
        "bm25(question)": lambda c: rag.rank_many(c["queries"][:1], c["items"]),
        "bm25(question)+bm25(sketch)  [now]": lambda c: rag.rank_many(c["queries"], c["items"]),
    }
    for m in args.e:
        variants[f"{m}(question)"] = lambda c, m=m: by_embedding(m, c["q"], c["items"], cache)
        variants[f"bm25(question)+{m}"] = lambda c, m=m: interleave(
            rag.rank_many(c["queries"][:1], c["items"]), by_embedding(m, c["q"], c["items"], cache))
        variants[f"bm25+sketch+{m}"] = lambda c, m=m: interleave(
            rag.rank_many(c["queries"], c["items"]), by_embedding(m, c["q"], c["items"], cache))
    # only calls where the key fact is somewhere in the chunks: otherwise no ranking can find it
    useful = [c for c in calls if any(k.lower() in t.lower() for _, t in c["items"] for k in c["keys"])]
    print(f"{len(useful)} of {len(calls)} ranking calls have the key fact somewhere in their chunks\n")
    k = args.top
    print(f"{'ranking':44} key in top {k}   mean position of the first chunk with it   time per call")
    for name, fn in variants.items():
        hits, pos, times = 0, [], []
        # chunk vectors are cached across variants: the first variant of a model shows the real
        # (cold) time, the later ones only the query's vector
        for c in useful:
            t0 = time.perf_counter()
            ranked = fn(c)
            times.append(time.perf_counter() - t0)
            first = next(i for i, (_, t) in enumerate(ranked) if any(kk.lower() in t.lower() for kk in c["keys"]))
            hits += first < k
            pos.append(first)
            if args.v and first >= k:
                print(f"    miss {name[:20]:20} #{first:<3} {c['q']}")
        print(f"{name:44} {hits:3}/{len(useful)}         {statistics.mean(pos):5.1f}"
              f"                                 {statistics.mean(times) * 1000:5.0f} ms")


def e2e(args) -> None:
    """web_eval's questions through the real path, chunks ranked by BM25 interleaved with the
    embedding model; --no-sketch drops the model's answer sketch (one LLM call less)."""
    cache: dict = {}
    m = args.e[0]
    real = rag.rank_many

    def rank_many(queries, items):
        bm25 = real(queries if not args.no_sketch else queries[:1], items)
        return interleave(bm25, by_embedding(m, queries[0], items, cache)) if items else bm25

    patches = [mock.patch.object(rag, "rank_many", rank_many)]
    if args.no_sketch:
        patches.append(mock.patch.object(skills_knowledge.KnowledgeSkills, "_answer_shape", lambda *a, **k: ""))
    for p in patches:
        p.start()
    sys.argv = [sys.argv[0], "--config", args.config] + (["--hard"] if args.hard else []) + \
        [x for k in args.k for x in ("-k", k)] + (["-v"] if args.v else [])
    web_eval.main()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--config", default="shellmate")
    c.add_argument("-k", action="append", default=[])
    p = sub.add_parser("compare")
    p.add_argument("-e", action="append", default=[], help="embedding model (repeatable)")
    p.add_argument("--top", type=int, default=6, help="chunks that go on to the quote picker")
    p.add_argument("-v", action="store_true")
    p.add_argument("--hard", action="store_true")
    c.add_argument("--hard", action="store_true")
    e = sub.add_parser("e2e")
    e.add_argument("-e", action="append", required=True, help="embedding model")
    e.add_argument("--config", default="shellmate")
    e.add_argument("--no-sketch", action="store_true")
    e.add_argument("--hard", action="store_true")
    e.add_argument("-k", action="append", default=[])
    e.add_argument("-v", action="store_true")
    args = ap.parse_args()
    {"capture": capture, "compare": compare, "e2e": e2e}[args.cmd](args)


if __name__ == "__main__":
    main()
