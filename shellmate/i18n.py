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


def plural(n: int, ru: tuple[str, str, str], en: tuple[str, str]) -> str:
    """"1 файл / 2 файла / 5 файлов", "1 file / 2 files"."""
    if LANG == "en":
        return f"{n} {en[0] if n == 1 else en[1]}"
    form = 0 if n % 10 == 1 and n % 100 != 11 else 1 if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 2
    return f"{n} {ru[form]}"


def en() -> bool:
    return LANG == "en"
