"""Picking quotes that actually answer the question.

Measured on eval/quotes_eval.py: letting the model pick IDs from the page in reading order keeps
step-by-step instructions together (keyword shortlists drop "cd into the folder, run makepkg"),
but the model also grabs neighbours from the wrong section and fills its quota. So:

  1. code: sentences in chunk-relevance order, each with its section ("HDR › Sway") and the
     sentence before it; sentences about a *different* program of the same kind are dropped
     (Sway/KDE for a Hyprland question, apt for a pacman question, Fedora for an Ubuntu one);
  2. model: picks up to 5 sentence IDs (schema enum — it can't invent text);
  3. model: checks EACH picked sentence on its own: answer / related / off, with the section
     visible — a focused yes/no is what small models do reliably;
  4. code: "It also says…" / "Это удалит…" get the previous sentence glued on.
"""

import json
import re
from dataclasses import dataclass

from shellmate import llm, rag


@dataclass
class Sentence:
    src: str
    text: str  # may carry command lines after "\n" (see rag.sentences)
    heading: str = ""
    prev: str = ""
    score: float = 0.0


def extract(items: list[tuple[str, str]], limit: int = 200) -> list[Sentence]:
    out, seen = [], set()
    for src, chunk in items:
        heading = ""
        lines = chunk.split("\n")
        if lines and lines[0].startswith("§ "):
            heading = lines[0][2:].strip()
            chunk = "\n".join(lines[1:])
        prev = ""
        for _, s in rag.sentences([(src, chunk)], limit):
            if s not in seen:
                seen.add(s)
                out.append(Sentence(src, s, heading, prev))
            prev = s.split("\n", 1)[0]
    return out[:limit]


# --- 2. same kind, different thing -----------------------------------------------------------
# Each group: things that are alternatives to each other; members are alias tuples.
# "x*" matches as a word prefix (hyprctl, убунту), everything else as a whole word ("arch" ≠ architecture).
SIBLINGS = [
    [("hyprland", "hypr*"), ("sway",), ("kde", "plasma", "kwin"), ("gnome", "mutter"), ("xfce",),
     ("i3",), ("wayfire",), ("niri",), ("cinnamon",), ("lxqt",), ("gamescope",)],
    # families, not single distros: Debian advice is usually right on Ubuntu, Arch advice on Manjaro
    [("arch", "archlinux", "cachyos", "endeavouros", "manjaro", "арч*"),
     ("debian", "ubuntu", "убунт*", "дебиан*", "mint", "kubuntu", "xubuntu", "pop!_os"),
     ("fedora", "федор*", "rhel", "centos", "rocky", "almalinux", "nobara"),
     ("opensuse", "suse"), ("nixos",), ("gentoo",)],
    # snap is not here: on Ubuntu it complements apt rather than replacing it
    [("pacman", "paru", "yay", "makepkg"), ("apt", "apt-get", "dpkg"), ("dnf", "yum", "rpm"),
     ("zypper",), ("emerge", "portage")],
    [("pipewire", "wireplumber"), ("pulseaudio",)],
    [("grub",), ("systemd-boot", "bootctl"), ("limine",), ("refind",)],
    [("sddm",), ("gdm",), ("lightdm",), ("lxdm",), ("greetd", "tuigreet")],
]


def _mentions(text: str, aliases: tuple[str, ...]) -> bool:
    for a in aliases:
        pat = re.escape(a.rstrip("*")) + ("" if a.endswith("*") else r"(?![\w-])")
        if re.search(rf"(?<![\w-]){pat}", text):
            return True
    return False


def off_topic(s: Sentence, question: str) -> bool:
    """True when the question names one member of a group and the sentence (or its section)
    talks about other members only: "add hdr to the sway config" for a Hyprland question."""
    q, t = question.lower(), f"{s.heading} {s.text}".lower()
    # unless another distro / package manager is named, the question is about this system
    if not any(_mentions(q, m) for m in SIBLINGS[1] + SIBLINGS[2]):
        from shellmate import backends
        q += " " + backends.current().context_words
    for group in SIBLINGS:
        asked = [m for m in group if _mentions(q, m)]
        if not asked:
            continue
        others = [m for m in group if m not in asked and _mentions(t, m)]
        if others and not any(_mentions(t, m) for m in asked):
            return True
    return False


# --- candidates in reading/relevance order -----------------------------------------------------


def candidates(items: list[tuple[str, str]], question: str, limit: int = 60) -> list[Sentence]:
    return [s for s in extract(items) if not off_topic(s, question)][:limit]


def select(sents: list[Sentence], question: str, hint: str, url: str, model: str) -> list[Sentence]:
    listing = [(s.src, f"[{s.heading}] {s.text.split(chr(10))[0]}" if s.heading else s.text.split(chr(10))[0])
               for s in sents]
    ask = question + (f"\n(in English: {hint})" if hint and hint != question else "")
    raw = llm.chat(url, model, [{"role": "system", "content": rag.select_prompt(listing, "web pages / documents")},
                                {"role": "user", "content": ask}], schema=rag.select_schema(len(sents)))
    return [sents[i] for i in rag.parse_selection(raw, len(sents))]


# --- 4. per-sentence labels from the model -------------------------------------------------------

LABELS = ["answer", "related", "off"]
GRADE_TASK = """You check evidence for a question. For EACH numbered sentence give one label:
- answer: it directly answers the question (gives the asked fact, command, setting or explanation);
- related: same topic, but does not answer the question;
- off: another topic, a question, navigation, or meaningless out of context — and ALWAYS off when the
  [section] is about a different program, application or system than the one the question asks about.
An instruction, setting or command that does what the question asks IS an answer.
The text in [brackets] is the section of the page the sentence comes from.
Return exactly one label per sentence, in order.

{sentences}"""


def grade(sents: list[Sentence], question: str, hint: str, url: str, model: str) -> tuple[list[str], list[float]]:
    """Labels and how sure the model was about each one (token probability)."""
    def shown(s: Sentence) -> str:  # with the first lines of attached code: "…like so: ⏎ cm = "hdr""
        first, *code = s.text.split("\n")
        return first + (" ⏎ " + " ⏎ ".join(code[:4]) if code else "")
    listing = "\n".join(f"{i}. [{s.heading or '—'}] {shown(s)}" for i, s in enumerate(sents, 1))
    schema = {"type": "object",
              "properties": {"labels": {"type": "array", "items": {"type": "string", "enum": LABELS},
                                        "minItems": len(sents), "maxItems": len(sents)}},
              "required": ["labels"]}
    ask = question + (f"\n(in English: {hint})" if hint and hint != question else "")
    r = llm.chat_full(url, model, [{"role": "system", "content": GRADE_TASK.format(sentences=listing)},
                                   {"role": "user", "content": ask}], schema=schema, logprobs=True)
    raw, lps = r["message"]["content"], r.get("logprobs") or []
    try:
        labels = json.loads(raw)["labels"]
    except (ValueError, KeyError, TypeError):
        return ["off"] * len(sents), [1.0] * len(sents)
    probs = [llm.span_confidence(raw, lps, m.start(1), m.end(1)) if lps else 1.0
             for m in re.finditer(r'"(answer|related|off)"', raw)]
    n = len(sents)
    return (labels + ["off"] * n)[:n], (probs + [1.0] * n)[:n]


# --- 5. make a quote readable on its own -----------------------------------------------------------

DANGLING = re.compile(r"^(?:it|this|that|these|those|they|he|she|however|also|but|so|then|there|"
                      r"such|both|thus|это|этот|эта|эти|он|она|оно|они|однако|также|но|поэтому|тогда|"
                      r"там|такой|такие|при этом|цей|ця|це|ці|він|вона|воно|вони|проте|також|тому)\b", re.I)


def readable(s: Sentence) -> str:
    if s.prev and DANGLING.match(s.text) and len(s.prev) + len(s.text) < 600:
        return f"{s.prev} {s.text}"
    return s.text


@dataclass
class Evidence:
    answers: list[Sentence]
    related: list[Sentence]
    confidence: float = 1.0  # lowest label probability among the accepted answers


def pick(items: list[tuple[str, str]], question: str, hint: str, url: str, model: str,
         max_quotes: int = 4, per_source: int = 3) -> Evidence:
    cands = candidates(items, f"{question} {hint}")
    picked = select(cands, question, hint, url, model) if cands else []
    if not picked:
        return Evidence([], [])
    labels, probs = grade(picked, question, hint, url, model)
    answers, related, per, conf = [], [], {}, 1.0
    for s, label, p in zip(picked, labels, probs):
        if label == "answer" and per.get(s.src, 0) < per_source and len(answers) < max_quotes:
            per[s.src] = per.get(s.src, 0) + 1
            answers.append(s)
            conf = min(conf, p)
        elif label == "related" and len(related) < 2:
            related.append(s)
    shown = {s.text.split("\n", 1)[0] for s in answers + related}
    for s in answers + related:
        first, *code = s.text.split("\n")
        if s.prev not in shown:  # don't repeat a sentence that is already its own quote
            first = readable(Sentence(s.src, first, s.heading, s.prev))
        s.text = "\n".join([first, *code])
    return Evidence(answers, related, conf)
