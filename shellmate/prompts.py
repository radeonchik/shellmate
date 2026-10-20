"""Task prompts for free-form model replies (the persona's style is prepended to each)."""

import re

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

CHAT_TASK = """Reply to the user's message in 1-3 short sentences, in {lang}, in your communication style.
If they ask what you can do, use ONLY this list and never claim other abilities:
{capabilities}
You cannot run arbitrary commands or do anything not on this list."""

CAPABILITIES = """  установить, удалить и найти программу ({sources})
  обновить систему (со снимком btrfs, если он настроен) и откатить пакет на старую версию
  показать место на диске, почистить мусор, найти самые большие файлы
  проверить систему: упавшие сервисы и ошибки в журнале
  управлять сервисами (звук, блютуз, wi-fi…): статус, перезапуск, автозапуск
  показать, что грузит процессор и память, и закрыть программу
  сказать, из какого пакета команда или файл
  сводка о системе: железо, драйверы, температура, батарея
  сеть: есть ли интернет, IP, Wi-Fi, кто занял порт
  найти ответ в интернете
  отвечать по твоим документам (добавь папку: «{name} добавь ~/Documents в источники»)
  объяснить ошибку (можно так: команда 2>&1 | {name} explain)"""


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
            out.append("⚠ [здесь была опасная команда — я её убрал]")
        else:
            out.append(line)
    return "\n".join(out), removed


def lang_of(text: str) -> str:
    if re.search(r"[іїєґ]", text, re.IGNORECASE):
        return "Ukrainian"
    return "Russian" if re.search(r"[а-яё]", text, re.IGNORECASE) else "English"
