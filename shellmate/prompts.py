"""Task prompts for free-form model replies (the persona's style is prepended to each)."""

import re

from shellmate.i18n import en, t

EXPLAIN = """Explain what the given error, command or term means and give the most likely fix
as concrete shell commands. The system is {system}: use {tools},
NEVER {never}. NEVER suggest destructive commands (rm -rf, rm -r,
dd, mkfs, chattr, chmod -R, wiping folders); if a fix needs deleting data, say so in words.
If you are not sure, say so instead of guessing.
At most 8 short lines, no markdown headers. Answer in {lang}."""

LOG = """This is an error line from the systemd journal of a {system} computer. Say in 2-4 short
lines whether it is a common harmless message or needs action, and if it needs action, what to do.
Answer in {lang}."""


def _system() -> dict:
    from shellmate import backends
    be = backends.current()
    return {"system": be.distro.name, "tools": be.tools_hint or "the system's package manager / systemctl",
            "never": be.other_tools or "tools of other distributions"}


def explain_task(lang: str) -> str:
    return EXPLAIN.format(lang=lang, **_system())


def log_task(lang: str) -> str:
    return LOG.format(lang=lang, **_system())

TRANSLATE_TASK = """You are a professional translator. First decide the target language: the one the
request names; if the request names none — {target}. Then translate the text into it naturally, the way a
native speaker would say it, keeping meaning, tone, line breaks and formatting; names, code, commands and
paths stay unchanged. The translation must be in the target language, never a copy of the original."""

TRANSLATE_SCHEMA = {"type": "object",
                    "properties": {"target_language": {"type": "string"}, "translation": {"type": "string"}},
                    "required": ["target_language", "translation"]}

# when the model copied the text instead: the target named outright
TRANSLATE_AGAIN = """You are a professional translator. Output ONLY the translation, natural, as a native
speaker would say it."""

CHAT_TASK = """Reply to the user's message in 1-3 short sentences, in {lang}, in your communication style.
If they ask what you can do, use ONLY this list and never claim other abilities:
{capabilities}
You cannot run arbitrary commands or do anything not on this list."""

CAPABILITIES_RU = """  установить, удалить и найти программу ({sources})
  обновить систему (со снимком btrfs, если он настроен) и откатить пакет на старую версию
  показать место на диске, почистить мусор, найти самые большие файлы, сказать, что лежит в папке
  проверить систему: упавшие сервисы и ошибки в журнале
  управлять сервисами (звук, блютуз, wi-fi…): статус, перезапуск, автозапуск
  показать, что грузит процессор и память, и закрыть программу
  сказать, из какого пакета команда или файл
  сводка о системе: железо, драйверы, температура, батарея
  сеть: есть ли интернет, IP, Wi-Fi, кто занял порт
  найти ответ в интернете
  отвечать по твоим документам (добавь папку: «{name} добавь ~/Documents в источники»)
  объяснить ошибку (можно так: команда 2>&1 | {name} explain)
  перевести текст (можно и файл: cat файл | {name} переведи на английский)"""

CAPABILITIES_EN = """  install, remove and find programs ({sources})
  update the system (with a btrfs snapshot if set up) and roll a package back to an older version
  show disk space, clean up junk, find the biggest files, tell what is in a folder
  check the system: failed services and errors in the journal
  manage services (sound, bluetooth, wi-fi…): status, restart, autostart
  show what loads the CPU and memory, and close a program
  tell which package a command or file comes from
  system overview: hardware, drivers, temperature, battery
  network: is the internet up, IP, Wi-Fi, who holds a port
  find an answer on the web
  answer from your documents (add a folder: “{name} add ~/Documents to sources”)
  explain an error (like this: command 2>&1 | {name} explain)
  translate a text (a file too: cat file | {name} translate to Russian)"""


def capabilities() -> str:
    return t(CAPABILITIES_RU, CAPABILITIES_EN)


# a small model's free-form advice must never carry a command that can destroy data
DANGEROUS = re.compile(r"\brm\s+-[a-zA-Z]*[rf]|\brm\s+--recursive|\bdd\s+if=|\bmkfs\b|\bchattr\b|"
                       r"\bchmod\s+-R|\bchown\s+-R|\bwipefs\b|\bshred\b|>\s*/dev/(?:sd|nvme)|:\(\)\s*\{|"
                       r"\bfind\b.*-delete|\bpacman\s+-Rdd|--overwrite\s+['\"]?\*")


def defang(reply: str) -> tuple[str, bool]:
    """Replaces lines with destructive commands by a warning. Returns (text, anything removed)."""
    out, removed = [], False
    for line in reply.splitlines():
        if DANGEROUS.search(line):
            removed = True
            out.append(t("⚠ [здесь была опасная команда — я её убрал]", "⚠ [a dangerous command was here — I removed it]"))
        else:
            out.append(line)
    return "\n".join(out), removed


def ui_language() -> str:
    return t("Russian", "English")


def lang_of(text: str) -> str:
    """The language to answer in: the request's own, or the interface's when it has no letters."""
    if re.search(r"[іїєґ]", text, re.IGNORECASE):
        return "Ukrainian"
    if re.search(r"[а-яё]", text, re.IGNORECASE):
        return "Russian"
    return "English" if re.search(r"[a-z]", text, re.IGNORECASE) or en() else "Russian"
