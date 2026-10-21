"""Turns a free-form request into a structured Intent. This is the ONLY job the model has
for actions: it never writes shell commands, it only fills a JSON schema."""

import json
import re
from difflib import SequenceMatcher
from dataclasses import dataclass, field

from shellmate import llm
from shellmate.i18n import t

INTENTS = ["install", "remove", "search", "update", "downgrade", "disk", "bigfiles", "health",
           "service", "processes", "pkginfo", "sysinfo", "network", "web", "docs", "explain",
           "theme", "news", "undo", "history", "chat", "unknown"]
ACTIONS = ["", "status", "start", "stop", "restart", "enable", "disable", "kill", "add", "remove"]

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "packages": {"type": "array", "items": {"type": "string"}},
        "text": {"type": "string"},
        "action": {"type": "string", "enum": ACTIONS},
    },
    "required": ["intent", "packages", "text", "action"],
}

SYSTEM = """You are the intent parser of a terminal assistant on @SYSTEM@ (@FAMILY@ family of Linux).
Convert the user's request into JSON. You never run commands and never chat.

intent:
- install: user wants to install a program.
- remove: user wants to uninstall a program.
- search: user wants to find or browse programs, or asks whether something exists.
- update: update / upgrade the system or all packages.
- downgrade: go back to an older version of a PACKAGE ("откати firefox", "верни старую версию
  mesa"), or roll back after a bad update.
- disk: free disk space, how full the disk is, or cleaning up the system / freeing space.
- bigfiles: which files or folders take the most space, largest files in some folder.
- health: what is broken, errors in the system log, failed services, general "check my system".
- service: status / start / stop / restart / enable / disable of a system service, or why a
  specific subsystem (sound, wifi, bluetooth, printer...) does not work.
- processes: what is slowing the computer down, what uses CPU or RAM, kill / close a program.
- pkginfo: which package provides a command or file, or details about an installed package.
- sysinfo: hardware and system overview: kernel, CPU, GPU, driver, RAM, temperature, battery.
- network: the internet connection of THIS computer: is it working, IP address, Wi-Fi, DNS,
  which program uses a port.
- web: the user explicitly asks to search the internet; asks about news, releases, prices or
  other current facts about the world; or asks HOW to do / configure something on Linux that is
  not one of the actions above (Hyprland settings, autologin, HDR, keyboard layout...).
  Not for checking this computer's own connection.
- docs: the user asks about THEIR OWN documents / notes / files content, or adds or removes a
  folder as a source of documents.
- explain: user asks what an error message, command or Linux term means, or why something failed.
- theme: change the assistant's own color / look in the terminal.
- news: news of the user's Linux distribution itself (announcements, updates needing manual intervention).
- undo: undo / revert what the ASSISTANT did last, no package named ("отмени", "верни как было").
- history: what the assistant has done recently.
- chat: greetings, thanks, small talk, or questions about the assistant itself.
- unknown: any other task (math, poems, writing code, tasks not listed above).

packages: @PKGS@ package names of the programs the user named. Lowercase, dashes instead of
spaces. Use the real package name on this system when you know it ("vs code" -> "code",
"telegram" -> "telegram-desktop", "chrome" -> "google-chrome"). Empty list if none named.

text:
- install/search without a named program: short English keywords of what is wanted.
- bigfiles: the folder the user named as a path like "~/Downloads", else "".
- service: the systemd unit name or keyword (sound -> "pipewire", wifi -> "NetworkManager",
  bluetooth -> "bluetooth", printer -> "cups").
- processes: the program name if the user named one, else "".
- pkginfo: the command name or file path the user asks about.
- network: the port number if asked about a port, "public ip" if asked for the external IP, else "".
- web: a short web search query about EXACTLY the user's topic. The user's computer runs @SYSTEM@,
  but web questions can be about anything: never add the distro name unless the user mentioned it.
- docs: the user's question about their documents, or the folder path when adding/removing.
- explain: the error or term, copied verbatim.
- theme: the color the user wants, as they said it (or a #hex), else "".
- otherwise "".

action: service -> status/start/stop/restart/enable/disable (status if unclear);
processes -> "kill" when the user wants to close or kill a program; docs -> "add" or "remove"
when managing folders; otherwise "".

Earlier turns of the conversation may be included: use them to resolve follow-ups like
"а теперь удали его", "а как это отключить?", "и ещё дискорд" (take names/topics from them).

The request may be in any language and may contain typos and slang. Russian tech slang:
раст = Rust, питон = Python, джава = Java, плюсы = C++, шарп = C#, гоу = Go, убунта/убунту = Ubuntu,
винда = Windows, линь/линух = Linux, видюха = GPU, проц = CPU, оператива = RAM, прога = program."""


def _ex(intent: str, packages: list[str] | None = None, text: str = "", action: str = "") -> dict:
    return {"intent": intent, "packages": packages or [], "text": text, "action": action}


# Few-shot turns: small models follow examples far better than rules.
EXAMPLES = [
    ("install me obs studio", _ex("install", ["obs-studio"])),
    ("поставь телегу и вскод", _ex("install", ["telegram-desktop", "code"])),
    ("накати гимп и криту", _ex("install", ["gimp", "krita"])),  # Cyrillic transliteration
    ("поставь что-нибудь для записи звука", _ex("install", text="audio recorder")),
    ("uninstall discord pls", _ex("remove", ["discord"])),
    ("поставь гимп", _ex("install", ["gimp"])),
    ("а теперь удали его", _ex("remove", ["gimp"])),  # follow-up: "его" = the previous package
    ("есть ли нормальный редактор pdf?", _ex("search", text="pdf editor")),
    ("обнови всё", _ex("update")),
    ("после обновления сломался mesa, верни старую версию", _ex("downgrade", ["mesa"])),
    ("откати nvidia-utils", _ex("downgrade", ["nvidia-utils"])),
    ("сколько места осталось", _ex("disk")),
    ("что жрёт место в загрузках?", _ex("bigfiles", text="~/Downloads")),
    ("проверь, всё ли в порядке с системой", _ex("health")),
    ("почему не работает звук", _ex("service", text="pipewire", action="status")),
    ("перезапусти блютуз", _ex("service", text="bluetooth", action="restart")),
    ("убери ssh из автозагрузки", _ex("service", text="sshd", action="disable")),
    ("что тормозит комп", _ex("processes")),
    ("закрой стим", _ex("processes", text="steam", action="kill")),
    ("в каком пакете команда convert", _ex("pkginfo", text="convert")),
    ("какая у меня видеокарта и драйвер", _ex("sysinfo")),
    ("кто занял порт 8080", _ex("network", text="8080")),
    ("интернет вообще работает", _ex("network")),
    ("найди в интернете, когда выйдет ядро 7.3", _ex("web", text="linux kernel 7.3 release date")),
    ("что пишут про переписывание питона на плюсах", _ex("web", text="Python rewrite in C++ discussion")),
    ("как настроить автологин в sddm", _ex("web", text="sddm autologin configuration")),
    ("добавь папку ~/Documents в источники", _ex("docs", text="~/Documents", action="add")),
    ("что у меня в заметках про настройку роутера", _ex("docs", text="настройка роутера")),
    ("что значит error: failed to commit transaction (conflicting files)",
     _ex("explain", text="error: failed to commit transaction (conflicting files)")),
    ("перекрась себя в зелёный", _ex("theme", text="зелёный")),
    ("есть важные новости арча?", _ex("news")),
    ("отмени последнее", _ex("undo")),
    ("что ты сегодня делал", _ex("history")),
    ("привет", _ex("chat")),
    ("what can you do?", _ex("chat")),
    ("сколько будет 2+2", _ex("unknown")),
]


@dataclass
class Intent:
    intent: str
    packages: list[str] = field(default_factory=list)
    text: str = ""
    action: str = ""
    # how sure the model was (lowest token probability), not part of the JSON schema
    intent_p: float = 1.0
    packages_p: float = 1.0
    model: str = ""

    def fields(self) -> dict:
        """Just the schema fields — what gets fed back to the model as conversation context."""
        return {"intent": self.intent, "packages": self.packages, "text": self.text, "action": self.action}


PKG_KIND = {"arch": "Arch/AUR", "debian": "Debian/Ubuntu (apt)", "fedora": "Fedora (dnf)"}


def system_prompt() -> str:
    from shellmate import backends  # local: keeps the router importable without probing the system
    d = backends.distro()
    return (SYSTEM.replace("@SYSTEM@", d.name).replace("@FAMILY@", d.family.capitalize())
            .replace("@PKGS@", PKG_KIND.get(d.family, "Linux")))


def build_messages(query: str, context: list[dict] | None = None) -> list[dict]:
    msgs = [{"role": "system", "content": system_prompt()}]
    for q, a in EXAMPLES:
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": json.dumps(a, ensure_ascii=False)})
    for turn in context or []:
        # what was actually done beats what the model guessed: "obs" -> the package really installed
        intent = dict(turn["intent"])
        if turn.get("result", {}).get("packages"):
            intent["packages"] = turn["result"]["packages"]
        msgs.append({"role": "user", "content": turn["query"]})
        msgs.append({"role": "assistant", "content": json.dumps(intent, ensure_ascii=False)})
    msgs.append({"role": "user", "content": query})
    return msgs


def _norm_pkg(name: str) -> str:
    return re.sub(r"[^a-z0-9@._+-]", "", re.sub(r"\s+", "-", name.strip().lower()))


def parse(raw: str) -> Intent:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return Intent("unknown")
    intent = data.get("intent")
    if intent not in INTENTS:
        intent = "unknown"
    action = data.get("action") if data.get("action") in ACTIONS else ""
    pkgs = [p for p in (_norm_pkg(str(x)) for x in data.get("packages") or []) if p]
    return Intent(intent, list(dict.fromkeys(pkgs)), str(data.get("text") or "").strip(), action)


def classify(url: str, model: str, query: str, context: list[dict] | None = None) -> Intent:
    r = llm.chat_full(url, model, build_messages(query, context), schema=SCHEMA, logprobs=True)
    content, lps = r["message"]["content"], r.get("logprobs") or []
    it = parse(content)
    it.model = model
    m = re.search(r'"intent"\s*:\s*"([^"]*)"', content)
    if m and lps:
        it.intent_p = llm.span_confidence(content, lps, m.start(1), m.end(1))
    m = re.search(r'"packages"\s*:\s*\[([^\]]*)\]', content)
    if m and lps and m.group(1).strip():
        it.packages_p = llm.span_confidence(content, lps, m.start(1), m.end(1))
    return it


# --- when the fast model's answer needs a second opinion ------------------------------------------
# "подумай хорошенько", "это сложная задача", "think hard" — the user asks for the big model
DEEP = re.compile(r"[,.]?\s*(?:(?:подумай|подумайте|разберись|think)(?:\s+(?:хорошенько|как следует|внимательно|"
                  r"тщательно|получше|hard|carefully))?|(?:это\s+)?сложн\w*\s+(?:задач\w*|вопрос\w*|случа\w*)|"
                  r"take your time)\s*[,.:]?", re.I)
# "проверь внимательно систему": the adverb asks for care, the verb is the request itself
CAREFUL = re.compile(r"(?<=\s)(?:внимательно|тщательно|carefully|thoroughly)\b\s*", re.I)


# Follow-ups point back with a pronoun or a connector: "а теперь удали его", "и ещё дискорд".
# Without one, earlier turns only bias a standalone request ("кинь мне обс" after "обсидиан").
FOLLOW_UP = re.compile(r"(?:^|\s)(?:его|её|ее|их|ему|ей|это|этот|эта|эту|этим|тот|ту|то|туда|тоже|ещё|еще|"
                       r"снова|опять|обратно|так же|также|it|its|this|that|them|those|too|also|again)(?=\s|[?!.,]|$)|"
                       r"^\s*(?:а|и|ну а|and|also|now)\s", re.I)


def is_follow_up(query: str) -> bool:
    return bool(FOLLOW_UP.search(query))


def wants_deep(query: str) -> tuple[bool, str]:
    """(asked to think hard, the request without that remark)."""
    if not DEEP.search(query) and not CAREFUL.search(query):
        return False, query
    rest = re.sub(r"\s+", " ", CAREFUL.sub("", DEEP.sub(" ", query))).strip(" ,.:")
    return True, rest or query


REMOVE_WORDS = re.compile(r"\b(удал|снес|снёс|выпил|избав|убер|uninstall|remove|delete|purge)", re.I)
INSTALL_WORDS = re.compile(r"\b(постав|установ|накат|скача|install|add)", re.I)


def doubts(it: Intent, query: str, min_intent: float, min_packages: float) -> list[str]:
    """Reasons not to trust a routing result. Token probabilities catch hesitation; the checks
    against the request itself catch confident mistakes (a confident hallucination looks sure)."""
    from shellmate.packages import spelled_words  # local: packages imports nothing from here
    reasons = []
    if it.intent_p < min_intent:
        reasons.append(t(f"колеблется в действии ({it.intent_p:.0%})", f"hesitates about the action ({it.intent_p:.0%})"))
    if it.packages and it.packages_p < min_packages:
        reasons.append(t(f"не уверена в имени пакета ({it.packages_p:.0%})", f"unsure of the package name ({it.packages_p:.0%})"))
    if it.intent == "unknown":
        reasons.append(t("не поняла запрос", "didn't understand the request"))
    if it.packages and it.intent not in ("install", "remove", "search", "downgrade", "pkginfo"):
        reasons.append(t("назвала пакет, но действие не про пакеты", "named a package, but the action isn't about packages"))
    if it.intent == "install" and REMOVE_WORDS.search(query) and not INSTALL_WORDS.search(query):
        reasons.append(t("в запросе «удалить», а модель решила «установить»", "the request says “remove”, the model chose “install”"))
    if it.intent == "remove" and INSTALL_WORDS.search(query) and not REMOVE_WORDS.search(query):
        reasons.append(t("в запросе «установить», а модель решила «удалить»", "the request says “install”, the model chose “remove”"))
    # in a follow-up ("удали его") the name comes from the conversation, not from the typed words
    if it.packages and it.intent in ("install", "remove", "downgrade") and not is_follow_up(query):
        # a package nobody typed: "обсидиан" -> obs-studio. Typed words are transliterated and
        # compared with the start of each package name
        words = [w for w in spelled_words(query) if len(w) >= 4]
        latin = [w for w in re.findall(r"[a-z][a-z0-9+.-]{2,}", query.lower())]
        typed = words + latin
        def alike(w: str, p: str) -> bool:  # "diskord" ~ discord, "telegu" ~ telegram-desktop
            return SequenceMatcher(None, w, p[: len(w) + 2]).ratio() >= 0.6 or w in p
        if typed and not any(alike(w, p) for p in it.packages for w in typed):
            reasons.append(t("имя пакета не похоже на написанное", "the package name doesn't look like what was typed"))
    return reasons
