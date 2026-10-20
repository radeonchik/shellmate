"""Compares models on the extractive step: do they pick on-topic sentences, and do they
say "nothing found" when the sources are about something else?

    uv run eval/quotes_eval.py qwen3.5:4b bonsai-2:9b
Docs cases search your own document index (`shellmate --add-docs <folder>`); they are skipped when
it is empty. The two built in were written for a folder of articles about DRAM prices — edit
them for your documents.
"""

import argparse
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shellmate import docs, evidence, llm, rag, web  # noqa: E402

UBUNTU = "https://www.turbogeek.co.uk/rust-coreutils-sudo-rs-ubuntu-26-04/"
HYPR = "https://lwn.net/Articles/1030177/"
PACMAN = "https://wiki.archlinux.org/title/Pacman"
SNAPPER = "https://wiki.archlinux.org/title/Snapper"
AW = "https://wiki.archlinux.org/index.php?action=render&title="  # article body with section headings
# (question, source, keywords that mark an on-topic sentence, should an answer exist)
CASES = [
    ("что пишут про переход убунту на раст", ("url", UBUNTU, "Ubuntu Rust coreutils sudo-rs"),
     ["rust", "sudo-rs", "coreutils", "canonical", "memory", "staged", "gnu"], True),
    ("какая погода в Киеве завтра", ("url", UBUNTU, "Kyiv weather tomorrow"), [], False),
    ("когда вышла Hyprland 0.50 и что в ней нового", ("url", HYPR, "Hyprland 0.50 release changes"),
     ["0.50", "render", "release", "screen shar", "test suite", "fps"], True),
    ("кто написал «Войну и мир»", ("url", HYPR, "War and Peace author"), [], False),
    ("как очистить кэш пакетов pacman", ("url", PACMAN, "clean pacman package cache"), ["paccache", "-sc", "cache", "cached"], True),
    ("как очистить кэш пакетов pacman, найди в инете", ("url", PACMAN, "clean pacman package cache"),
     ["paccache", "-sc", "cache", "cached"], True),
    ("какой рецепт борща", ("url", PACMAN, "borscht recipe"), [], False),
    ("как вручную создать снимок в snapper", ("url", SNAPPER, "snapper create snapshot manually"), ["snapper", "create", "snapshot"], True),
    ("чому зросли ціни на оперативну пам'ять", ("docs", "ціни оперативна пам'ять зростання"),
     ["попит", "дефіцит", "штучн", "дата-центр", "AI", "сервер"], True),
    ("що було у справі про картель виробників DRAM 2002 року", ("docs", "DRAM antitrust price fixing 2002 картель"),
     ["antitrust", "price", "fix", "micron", "samsung", "hynix", "infineon", "картел", "conspir", "plea", "fine"], True),
    ("сколько стоит билет на поезд Киев–Львов", ("docs", "билет поезд Киев Львов"), [], False),
    # traps: the page also has sections about competing programs
    ("как включить hdr в hyprland", ("url", AW + "HDR", "enable hdr in hyprland"),
     ["hyprland", "cm", "bitdepth", "hdr"], True, ["sway", "kde", "plasma", "gnome", "kwin", "gamescope"]),
    ("как настроить автологин в sddm", ("url", AW + "SDDM", "sddm autologin"),
     ["autologin", "user=", "session=", "relogin"], True, ["gdm", "lightdm", "lxdm"]),
    ("как вручную собрать пакет из aur", ("url", AW + "Arch_User_Repository", "build aur package manually"),
     ["makepkg", "git clone", "pkgbuild", "build"], True, ["apt", "dnf"]),
]


CASES = [c if len(c) == 5 else (*c, []) for c in CASES]
if not docs.stats("shellmate")[0]:  # no local document index on this machine
    CASES = [c for c in CASES if c[1][0] != "docs"]


def load(source: tuple, query: str) -> list[tuple[str, str]]:
    kind, arg = source[:2]
    if kind == "url":  # like web.gather: rank by the question + the router's English rewrite
        return rag.rank(f"{query} {source[2]}", [(arg, c) for c in rag.chunks(web.fetch_text(arg))], k=6)
    return docs.search("shellmate", arg)


def load_all() -> dict[str, list[tuple[str, str]]]:
    return {c[0]: load(c[1], c[0]) for c in CASES}


def _old(url: str, model: str, items, q: str, hint: str) -> list[str]:
    """The previous approach: the model picks up to 5 IDs out of all sentences at once."""
    sents = rag.sentences(items)
    raw = llm.chat(url, model, [{"role": "system", "content": rag.select_prompt(sents, "web pages / documents")},
                                {"role": "user", "content": rag.core_question(q)}],
                   schema=rag.select_schema(len(sents)))
    return [sents[i][1] for i in rag.parse_selection(raw, len(sents))]


def _new(url: str, model: str, items, q: str, hint: str) -> list[str]:
    ev = evidence.pick(items, rag.core_question(q), hint, url, model)
    return [f"{s.heading} {s.text}" for s in ev.answers]


def run_quotes(url: str, model: str, sources: dict, verbose: bool = False, pipeline: str = "new") -> dict:
    found_ok, precision, times, fails, wrong = 0, [], [], [], 0
    for q, src, keywords, expect, neg in CASES:
        hint = src[2] if src[0] == "url" else src[1]
        t = time.perf_counter()
        picked = (_new if pipeline == "new" else _old)(url, model, sources[q], q, hint)
        times.append(time.perf_counter() - t)
        ok = bool(picked) == expect
        found_ok += ok
        bad = [s for s in picked if any(re.search(rf"\b{re.escape(n)}\b", s.lower()) for n in neg)]
        wrong += len(bad)
        if expect:
            precision.append(sum(any(k.lower() in s.lower() for k in keywords) and s not in bad for s in picked)
                             / len(picked) if picked else 0.0)
        if not ok or bad:
            fails.append(q)
        if verbose:
            print(f"  [{pipeline}] {'✓' if ok and not bad else '✗'} {q!r}: {len(picked)} цитат")
            for s in picked:
                print(f"      · {s[:110]}")
    return {"found": found_ok, "cases": len(CASES), "precision": statistics.mean(precision),
            "avg_s": statistics.mean(times), "fails": fails, "wrong": wrong}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--compare", action="store_true", help="also run the old pick-IDs approach")
    args = ap.parse_args()
    sources = load_all()
    print(f"{'модель':<20}{'способ':<8}{'есть/нет верно':>15}{'точность':>10}{'чужие':>7}{'сред.':>8}")
    for model in args.models:
        llm.chat(args.url, model, [{"role": "user", "content": "hi"}])  # warm-up / load
        for pipeline in (["old", "new"] if args.compare else ["new"]):
            r = run_quotes(args.url, model, sources, args.verbose, pipeline)
            print(f"{model:<20}{pipeline:<8}{r['found']:>12}/{r['cases']}{r['precision']:>10.0%}"
                  f"{r['wrong']:>7}{r['avg_s']:>7.1f}s" + (f"   промахи: {r['fails']}" if r["fails"] else ""))
        llm.unload(args.url, model)


if __name__ == "__main__":
    main()
