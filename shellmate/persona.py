"""Communication style: how the agent talks. Fixed phrases come from a phrasebook
(instant, no model call); free-form replies get the style as part of the system prompt."""

from dataclasses import dataclass

from shellmate.config import Config


@dataclass
class Style:
    title: str  # shown in the setup menu
    prompt: str  # injected into the model's system prompt
    phrases: dict[str, str]


STYLES = {
    "friendly": Style(
        "Дружелюбно — на «ты», просто, можно с шуткой",
        "Talk casually and warmly, like a friend who is good with computers. "
        "In Russian address the user informally («ты»). Light humor is fine.",
        {
            "hello": "Привет{addr_comma}! Говори, что нужно.",
            "done": "Готово!",
            "cancelled": "Окей, отменил.",
            "not_found": "Хм, по «{q}» ничего не нашёл.",
            "unknown": "С этим я пока не помогу, но вот что умею:",
            "unclear": "Не совсем понял, что именно {what}. Уточни?",
            "run": "Запускаю?",
            "failed": "Упс, команда завершилась с ошибкой, и я её не узнал.",
            "healthy": "Всё чисто: упавших сервисов и ошибок с момента загрузки нет!",
            "quotes": "Вот что пишут — дословно:",
            "no_evidence": "Источники нашёл, но прямого ответа на твой вопрос в них нет.",
        },
    ),
    "butler": Style(
        "Как дворецкий — вежливо, на «вы», с достоинством",
        "Speak like a polite, composed English butler: formal, respectful, slightly dry wit. "
        "In Russian address the user formally («вы»).",
        {
            "hello": "К вашим услугам{addr_comma}. Чем могу быть полезен?",
            "done": "Готово{addr_comma}.",
            "cancelled": "Как пожелаете{addr_comma}, отменяю.",
            "not_found": "Боюсь, по запросу «{q}» ничего не найдено.",
            "unknown": "Прошу прощения, это вне моих полномочий. Вот чем я могу помочь:",
            "unclear": "Позвольте уточнить, что именно {what}?",
            "run": "Выполнить{addr_comma}?",
            "failed": "К сожалению, команда завершилась ошибкой, которая мне незнакома.",
            "healthy": "Рад доложить{addr_comma}: система в полном порядке.",
            "quotes": "Позвольте процитировать источники:",
            "no_evidence": "Боюсь, найденные источники не отвечают на ваш вопрос.",
        },
    ),
    "brief": Style(
        "Кратко — только по делу, без лишних слов",
        "Be extremely brief and matter-of-fact. No greetings, no small talk, no filler.",
        {
            "hello": "Готов.",
            "done": "Готово.",
            "cancelled": "Отменено.",
            "not_found": "Не найдено: {q}",
            "unknown": "Не умею. Умею:",
            "unclear": "Что {what}?",
            "run": "Выполнить?",
            "failed": "Ошибка, неизвестная.",
            "healthy": "Ошибок нет.",
            "quotes": "Цитаты:",
            "no_evidence": "В источниках ответа нет.",
        },
    ),
}
CUSTOM_BASE = "friendly"  # phrasebook used with a user-described style


def style_of(cfg: Config) -> Style:
    return STYLES.get(cfg.style) or STYLES[CUSTOM_BASE]


def phrase(cfg: Config, key: str, **kw) -> str:
    addr = cfg.address.strip()
    return style_of(cfg).phrases[key].format(addr_comma=f", {addr}" if addr else "", **kw)


def system_prompt(cfg: Config) -> str:
    style = cfg.style_custom if cfg.style == "custom" else style_of(cfg).prompt
    who = (f' When you address the user, call them "{cfg.address}" (now and then, not in every '
           "message).") if cfg.address.strip() else ""
    from shellmate import backends
    return (f"You are {cfg.name}, a small assistant living in the terminal of the user's "
            f"{backends.distro().name} computer. Communication style: {style}{who} "
            "Don't greet the user unless they greeted you first. Plain text for a terminal.")
