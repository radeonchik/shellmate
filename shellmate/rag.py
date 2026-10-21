"""Shared retrieval helpers for web pages and local documents: text extraction, chunking,
keyword ranking and the grounded-answer prompt."""

import html
import json
import math
import re
import zipfile
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

from shellmate.packages import run_quiet

CHUNK = 900  # characters; ~6 chunks fit comfortably into an 8k context with the question


HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}


class _HTMLText(HTMLParser):
    """HTML -> text. Headings become lines "§ Section › Subsection" so every piece of text
    can later say which part of the page it came from ("HDR › Sway" is not about Hyprland)."""
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg"}
    BLOCK = {"p", "div", "li", "br", "tr", "pre", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0
        self.heading: list[str] | None = None  # collecting a heading's text
        self.level = 0
        self.path: dict[int, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in HEADINGS and not self.skip:
            self.heading, self.level = [], HEADINGS[tag]
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):  # table cells must not glue into one word
            self.parts.append(" · ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in HEADINGS and self.heading is not None:
            title = re.sub(r"\s+", " ", "".join(self.heading)).replace("[edit]", "").strip(" #¶")
            self.heading = None
            if title:
                self.path = {lvl: t for lvl, t in self.path.items() if lvl < self.level}
                self.path[self.level] = title
                # the page title (h1) is already the source's name; the path starts below it
                shown = [t for lvl, t in sorted(self.path.items()) if lvl > 1] or [title]
                self.parts.append(f"\n\n§ {' › '.join(shown)}\n\n")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)
        elif not self.skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    # prefer the page's main content over menus and sidebars when it's marked up
    m = re.search(r"<(main|article)\b.*?</\1>", raw, re.S | re.I)
    if m and len(m.group(0)) > 2000:
        raw = m.group(0)
    p = _HTMLText()
    try:
        p.feed(raw)
    except Exception:  # broken markup: keep whatever was parsed
        pass
    return normalize("".join(p.parts))


def _xml_text(xml: str, para_tag: str) -> str:
    xml = re.sub(rf"</{para_tag}>", "\n", xml)
    return normalize(html.unescape(re.sub(r"<[^>]+>", "", xml)))


def normalize(text: str) -> str:
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".org", ".tex", ".csv", ".log", ".json",
            ".yaml", ".yml", ".toml", ".ini", ".conf", ".py", ".sh", ".gd"}
DOC_EXT = TEXT_EXT | {".pdf", ".docx", ".odt", ".html", ".htm", ".epub"}


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXT:
            return path.read_text(errors="replace")
        if ext in (".html", ".htm"):
            return html_to_text(path.read_text(errors="replace"))
        if ext == ".pdf":
            return normalize(run_quiet(["pdftotext", "-enc", "UTF-8", str(path), "-"], timeout=120))
        if ext in (".docx", ".odt", ".epub"):
            with zipfile.ZipFile(path) as z:
                if ext == ".docx":
                    return _xml_text(z.read("word/document.xml").decode(errors="replace"), "w:p")
                if ext == ".odt":
                    return _xml_text(z.read("content.xml").decode(errors="replace"), "text:p")
                return "\n\n".join(html_to_text(z.read(n).decode(errors="replace"))
                                   for n in z.namelist() if n.endswith((".xhtml", ".html", ".htm")))
    except (OSError, zipfile.BadZipFile, KeyError):
        return ""
    return ""


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Packs paragraphs into ~size-char chunks; overly long paragraphs are split by sentences.
    A chunk never crosses a section boundary and starts with its "§ section" line."""
    res, cur, heading = [], "", ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if para.startswith("§ "):
            if cur and cur != heading:
                res.append(cur)
            heading = cur = para
            continue
        if not cur and heading:
            cur = heading
        pieces = [para] if len(para) <= size else re.split(r"(?<=[.!?])\s+", para)
        for piece in pieces:
            while len(piece) > size:  # a single giant "sentence" (tables, code)
                res.append(piece[:size])
                piece = piece[size:]
            if len(cur) + len(piece) > size and cur and cur != heading:
                res.append(cur)
                cur = heading
            cur = f"{cur}\n{piece}" if cur else piece
    if cur and cur != heading:
        res.append(cur)
    # drop crumbs, but judge the text itself: a short section ("§ A › B" + one line) is still content
    return [c for c in res if len(c.split("\n", 1)[-1] if c.startswith("§ ") else c) > 15]


STOP = set("""что как где когда почему зачем это эти мои мой моя моих мне меня про для при или
the and for with what how why when where which this that about from are was есть был была
які який яка що про мене мої мій для або чому коли""".split())


def terms(text: str) -> list[str]:
    """Crude multilingual stemming: words cut to 6 chars, so «ціни»/«цінами», «настройка»/«настройки» match."""
    return [w[:6] for w in re.findall(r"\w+", text.lower()) if len(w) > 2 and w not in STOP]


def rank(query: str, items: list[tuple[str, str]], k: int = 6) -> list[tuple[str, str]]:
    """BM25 over (source, chunk) pairs held in memory — used for freshly fetched web pages."""
    q = set(terms(query))
    if not items or not q:
        return items[:k]
    docs = [Counter(terms(t)) for _, t in items]
    avg = sum(sum(d.values()) for d in docs) / len(docs) or 1
    df = Counter(w for d in docs for w in q if w in d)
    n = len(docs)

    def score(d: Counter) -> float:
        length = sum(d.values())
        return sum(math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5)) * d[w] * 2.2
                   / (d[w] + 1.2 * (0.25 + 0.75 * length / avg)) for w in q if w in d)

    scored = sorted(zip(map(score, docs), range(n)), reverse=True)
    return [items[i] for s, i in scored[:k] if s > 0] or items[:k]


def rank_many(queries: list[str], items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """All items, interleaving the rankings for several queries (the question in the user's words,
    a sketch of the answer…): each query's best chunks come early, none can push the others out."""
    rankings = [rank(q, items, k=len(items)) for q in queries if q.strip()] or [items]
    seen, merged = set(), []
    for row in zip(*rankings):
        for item in row:
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged + [i for i in items if i not in seen]


# --- extractive answers: the model picks sentences, it doesn't write them --------

SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[A-ZА-ЯЁІЇЄҐ0-9«\"(])")


# ArchWiki maintenance boxes ("Reason: …", "(Discuss in Talk:…)", "This article or section …")
WIKI_NOISE = re.compile(r"^(?:Reason:|\(Discuss in |This (?:article|section) (?:or section )?(?:is|needs|does)|"
                        r"Retrieved from |The factual accuracy of this)", re.I)
CODE_LINE = re.compile(r"^(?:[$#]\s|\S+\s*=|\S*[/{}()\[\]]|[a-z][\w.-]*(?:\s+-{1,2}\w|\s+\S+){0,6}$)")


def _looks_like_code(line: str) -> bool:
    return 2 <= len(line) <= 160 and not line.endswith((".", "!", "?")) and bool(CODE_LINE.match(line))


def _is_prose(line: str) -> bool:
    """A normal sentence: "Additional settings can be found on…" ends a code block."""
    words = line.split()
    sentence_like = len(words) >= 4 or (len(words) >= 2 and line.endswith((".", "!", "?")))
    return sentence_like and line[:1].isupper() and line[1:2].islower() and not CODE_LINE.match(line)


def sentences(items: list[tuple[str, str]], limit: int = 80) -> list[tuple[str, str]]:
    """(source, sentence) pairs in ranking order; menu/junk fragments are dropped. A sentence that
    ends with ':' keeps the command lines right after it ("To clean the cache, run:\n# paccache -r"),
    joined with '\n' — a quote that says "run:" without the command is useless."""
    res, seen = [], set()
    for src, text in items:
        # a line break before a lowercase letter is a hard wrap inside a sentence ("has been\nreleased");
        # other breaks separate headings, menu items and code, which must not glue into prose
        text = re.sub(r"(?<!:)\n(?=[a-zа-яёіїєґ0-9(\"«])", " ", text)
        lines = [line.strip() for line in re.split(r"\n+", text)]
        for li, line in enumerate(lines):
            if line.startswith("§ "):  # section heading, not a sentence
                continue
            if WIKI_NOISE.match(line):  # editors' notes, not content
                continue
            for s in SENT_SPLIT.split(line):
                s = s.strip(" _*")  # markdown emphasis leftovers
                letters = sum(ch.isalpha() for ch in s)
                if not (40 <= len(s) <= 500 and letters > len(s) * 0.6) or s in seen:
                    continue
                seen.add(s)
                if s.endswith(":") and line.endswith(s):
                    code = []  # first line must look like code; then everything until prose resumes
                    for nxt in lines[li + 1:li + 13]:
                        if nxt.startswith("§ ") or len(nxt) > 160 or (code and _is_prose(nxt)) or \
                                (not code and not _looks_like_code(nxt)):
                            break
                        code.append(nxt)
                    if code:
                        s += "\n" + "\n".join(code)
                res.append((src, s))
                if len(res) >= limit:
                    return res
    return res


SELECT_TASK = """You select evidence, you do not write answers. Below are numbered sentences
from {origin}. Return the IDs of up to 5 sentences that DIRECTLY answer the user's question,
most important first. Pick only sentences about the question's exact topic. If none of them
answer it, return found=false and an empty list.

{sentences}"""


# "find it on the internet" is an instruction to the assistant, not part of the question —
# small models otherwise look for sentences answering "search the internet"
META = re.compile(r"[,.]?\s*(?:пожалуйста\s+)?(?:найди|поищи|погугли|загугли|посмотри|глянь|search|google|look up|find(?!\s+out))"
                  r"(?:\s+(?:мне|это|плиз|пж|pls|please))?(?:\s+(?:в|на|по|in|on))?"
                  r"(?:\s+(?:the\s+)?(?:инете|интернете|сети|гугле|вики|web|internet|google|wiki))?(?:\s+for)?\s*[,:]?",
                  re.I)


def core_question(question: str) -> str:
    q = META.sub(" ", question)
    q = re.sub(r"\s+", " ", q).strip(" ,.:")
    return q or question


def select_schema(n: int) -> dict:
    ids = [f"S{i}" for i in range(1, n + 1)]
    return {"type": "object",
            "properties": {"found": {"type": "boolean"},
                           "quotes": {"type": "array", "maxItems": 5,
                                      "items": {"type": "string", "enum": ids}}},
            "required": ["found", "quotes"]}


def select_prompt(sents: list[tuple[str, str]], origin: str) -> str:
    return SELECT_TASK.format(origin=origin, sentences="\n".join(
        f"S{i}: {s}" for i, (_, s) in enumerate(sents, 1)))


def parse_selection(raw: str, n: int) -> list[int]:
    """0-based sentence indexes; anything malformed counts as 'nothing found'."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not data.get("found"):
        return []
    picked = [int(q[1:]) - 1 for q in data.get("quotes") or [] if re.fullmatch(r"S\d+", str(q))]
    return list(dict.fromkeys(i for i in picked if 0 <= i < n))


def drop_near_duplicates(quotes: list[tuple[str, str]], threshold: float = 0.6) -> list[tuple[str, str]]:
    """Pages often repeat their key sentence (intro + summary); keep the first wording."""
    kept: list[tuple[str, set[str]]] = []
    res = []
    for src, s in quotes:
        words = set(terms(s))
        # overlap of word stems relative to the shorter sentence: catches reworded repeats
        if all(len(words & k) < threshold * min(len(words), len(k)) for _, k in kept):
            kept.append((src, words))
            res.append((src, s))
    return res


# Pages rarely use the question's words: "years of Bismarck's life" is answered by "born … died …".
# The model sketches what an answer usually looks like, and pages are ranked against that sketch.
# The sketch is never shown — only verbatim quotes are — so a wrong guess can't reach the user.
ANSWER_SHAPE_TASK = """Write one or two short sentences in English the way an encyclopedia or official
documentation would answer the question, using the words such an answer typically contains (for dates:
born, died, began, ended, released; for software: the setting, option or command names). If you don't
know the facts, write placeholders instead of guessing. Output only the sentences."""


TRANSLATE_TASK = """Translate each sentence into {lang}, faithfully and literally: do not add, remove
or explain anything. Keep names, numbers and versions exactly. Return the same number of items."""


def translate_schema(n: int) -> dict:
    return {"type": "object",
            "properties": {"t": {"type": "array", "items": {"type": "string"}, "minItems": n, "maxItems": n}},
            "required": ["t"]}


SUMMARY_TASK = """Answer the user's question in 2-3 short sentences in {lang}, using ONLY the quotes
below. Every claim must come from a quote; cite like [1]. Do not add anything the quotes don't say.

{quotes}"""
