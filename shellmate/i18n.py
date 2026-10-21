"""Interface language. Both versions of a message sit side by side in the code: t("…", "…"),
so a new message can't be added in one language only."""

import os

LANGS = {"ru": "Русский", "en": "English"}
LANG = "ru"


def system_lang() -> str:
    """The desktop's language: ru for a Russian locale, English for everything else."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value:
            return "ru" if value.lower().startswith("ru") else "en"
    return "en"


def set_lang(lang: str) -> None:
    global LANG
    LANG = lang if lang in LANGS else system_lang()


def t(ru: str, en: str) -> str:
    return en if LANG == "en" else ru


def en() -> bool:
    return LANG == "en"
