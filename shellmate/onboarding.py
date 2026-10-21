"""First-run setup: introduce the agent, ask how it should talk, pick a model for this hardware."""

import getpass
import shutil

from shellmate import config, hardware, i18n, llm, persona, runner, ui, web
from shellmate.i18n import t

EXAMPLES_EN = [
    "install obs studio please",
    "update the system",
    "what's eating my disk space?",
    "check if everything is fine with the system",
    "why is there no sound",
    "what is slowing my computer down",
    "search the web for how to enable hdr in hyprland",
    "add ~/Documents to sources",
]
EXAMPLES = [
    "установи пж обс студио",
    "обнови систему",
    "что жрёт место на диске?",
    "проверь, всё ли в порядке с системой",
    "почему не работает звук",
    "что тормозит комп",
    "найди в интернете, как включить hdr в hyprland",
    "добавь ~/Documents в источники",
]


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _prompt(text: str) -> str:
    return _ask(ui.rgb("accent2", "❯ ") + text)


def lang_step(cfg: config.Config) -> None:
    langs = list(i18n.LANGS)
    default = cfg.lang or i18n.system_lang()
    ui.panel("Язык · Language", [f"{ui.rgb('accent2', str(i))}  {i18n.LANGS[k]}" + (ui.dim("  ←") if k == default else "")
                                 for i, k in enumerate(langs, 1)], "accent2", "◇")
    ans = _prompt(ui.dim("(Enter — " + i18n.LANGS[default] + "): "))
    cfg.lang = langs[int(ans) - 1] if ans.isdigit() and 1 <= int(ans) <= len(langs) else default
    i18n.set_lang(cfg.lang)


def run(cfg: config.Config) -> None:
    lang_step(cfg)
    ui.banner(cfg.name, t("локальный помощник в терминале · модель работает на этом компьютере, без облаков",
                          "a local terminal assistant · the model runs on this computer, no cloud"))
    ui.panel(t("Пиши мне своими словами — на русском или английском", "Talk to me in your own words — in English or Russian"), [
        *(f"{ui.dim('$')} {ui.bold(cfg.name)} {e}" for e in t(EXAMPLES, EXAMPLES_EN)),
        f"{ui.dim('$')} paru -S foo 2>&1 | {ui.bold(cfg.name)} explain",
        "",
        ui.dim(t("Перед любым действием я покажу точную команду и спрошу разрешения.",
                 "Before any action I show the exact command and ask for permission.")),
    ], "accent", "✦")

    keys = list(persona.STYLES)
    ui.panel(t("Как мне с тобой общаться?", "How should I talk to you?"), [
        *(f"{ui.rgb('accent2', str(i))}  {persona.title(persona.STYLES[k])}" for i, k in enumerate(keys, 1)),
        f"{ui.rgb('accent2', str(len(keys) + 1))}  " + t("Свой вариант — опишешь сам", "Your own — describe it yourself"),
    ], "accent2", "☺")
    ans = _prompt("")
    custom = ""
    if ans.isdigit() and 1 <= int(ans) <= len(keys):
        cfg.style = keys[int(ans) - 1]
    elif ans == str(len(keys) + 1):
        custom = _prompt(t("Опиши стиль своими словами: ", "Describe the style in your own words: "))
    elif ans and not ans.isdigit():
        custom = ans  # typed a description straight into the menu — take it as is
    if custom:
        cfg.style, cfg.style_custom = "custom", custom
    elif not cfg.style:
        cfg.style = persona.CUSTOM_BASE

    cfg.address = _prompt(t("Как к тебе обращаться? ", "What should I call you? ") +
                          ui.dim(t("(имя, «босс», что угодно; Enter — никак)", "(a name, “boss”, anything; Enter — nothing)")) + " ")
    color_step(cfg)
    model_step(cfg)
    path = config.save(cfg)
    print()
    ui.say(cfg.name, persona.phrase(cfg, "hello"))
    print(ui.dim(t(f"   настройки: {path} · стиль: {cfg.name} --setup · модель: {cfg.name} --pick-model\n",
                   f"   settings: {path} · style: {cfg.name} --setup · model: {cfg.name} --pick-model\n")))


def color_step(cfg: config.Config) -> None:
    keys = list(ui.PRESETS)
    ui.panel(t("Какого я буду цвета?", "What color shall I be?"), [
        *(f"{ui.rgb('accent2', str(i))}  {ui.swatch(cfg.name, k)}  {ui.dim(ui.preset_label(k))}"
          for i, k in enumerate(keys, 1)),
        ui.dim(t("…или свой: #ff8800 (можно два: #ff8800,#00ffaa). Поменять потом: ",
                 "…or your own: #ff8800 (or two: #ff8800,#00ffaa). Change it later: ") + f"{cfg.name} --color"),
    ], "accent2", "◐")
    ans = _prompt(t("номер или #hex ", "number or #hex ") + ui.dim(t("(Enter — оставить)", "(Enter — keep)")) + ": ")
    if ans.isdigit() and 1 <= int(ans) <= len(keys):
        cfg.color = keys[int(ans) - 1]
    elif ui.resolve_theme(ans):
        cfg.color = ui.theme_name(ans)
    ui.set_theme(cfg.color)
    ui.say(cfg.name, t("вот так: ", "like this: ") + ui.swatch(cfg.name, cfg.color))


def model_step(cfg: config.Config) -> None:
    """Checks the hardware, recommends a model (llmfit-style fit + speed estimate), pulls it."""
    print()
    with ui.working(t("проверяю железо и смотрю размеры моделей…", "checking the hardware and model sizes…")):
        hw = hardware.detect(cfg.ollama_url)
        fits = [f for f in hardware.evaluate(hw, cfg.ollama_url) if f.verdict != "too_tight"]
    ui.kv(t("Железо", "Hardware"), hardware.describe(hw), "accent", "⚙")
    if not fits:
        ui.problem(t("Ни одна модель не помещается в память — нужно хотя бы ~2 ГБ свободной RAM.",
                     "No model fits in memory — at least ~2 GB of free RAM is needed."))
        return
    best = fits[0]
    verdict_color = {"perfect": "ok", "good": "accent", "marginal": "warn"}
    lines = []
    for i, f in enumerate(fits[:5], 1):
        where = t("видеокарта", "GPU") if f.mode == "gpu" else t("процессор", "CPU")
        mark = ui.green(t(" ✓ скачана", " ✓ downloaded")) if f.installed else ""
        star = ui.rgb("accent2", "★") if f is best else " "
        lines.append(f"{star} {ui.rgb('accent2', str(i))}  {ui.bold(f.cand.tag.ljust(13))} "
                     f"{ui.rgb(verdict_color[f.verdict], hardware.verdict_label(f.verdict).ljust(8))} "
                     f"{where:<10} ~{f.tps:>3.0f} {t('ток/с', 'tok/s')}  {f.size / 1e9:.1f} {t('ГБ', 'GB')}{mark}")
        if f.cand.remark:
            lines.append(f"      {ui.dim(f.cand.remark)}")
    pool = t("видеопамяти", "of video memory") if best.mode == "gpu" else t("оперативной памяти", "of RAM")
    lines += ["", f"{ui.rgb('accent2', '★')} " + t(f"Рекомендую {ui.bold(best.cand.tag)}: займёт "
                                                    f"{best.utilization:.0%} {pool}, скорость ~{best.tps:.0f} ток/с.",
                                                    f"I recommend {ui.bold(best.cand.tag)}: it takes "
                                                    f"{best.utilization:.0%} {pool}, speed ~{best.tps:.0f} tok/s.")]
    ui.panel(t("Какая модель подойдёт", "Which model fits"), lines, "accent2", "◆")
    if not shutil.which("ollama"):
        cfg.model = best.cand.tag
        ui.panel(t("Нужна Ollama", "Ollama is needed"), [
            f"{ui.dim('$')} {hardware.ollama_install(hw)}",
            f"{ui.dim('$')} ollama pull {best.cand.tag}"], "warn", "!")
        return
    if not hardware.ollama_running(cfg.ollama_url):  # installed, service stopped: pull would fail
        cfg.model = best.cand.tag
        ui.panel(t("Ollama не запущена", "Ollama isn't running"), [
            f"{ui.dim('$')} sudo systemctl enable --now ollama",
            t("потом ещё раз: ", "then again: ") + f"{ui.dim('$')} {cfg.name} --pick-model"], "warn", "!")
        return
    _offer_limits()
    ans = _prompt(t("Enter — взять рекомендованную, номер — другую ", "Enter — take the recommended one, a number — another ")
                  + ui.dim(t("(сейчас: ", "(now: ") + cfg.model + ")") + ": ")
    pick = fits[int(ans) - 1] if ans.isdigit() and 1 <= int(ans) <= len(fits[:5]) else best
    if not pick.installed:
        cmd = ["ollama", "pull", pick.cand.tag]
        if not ui.confirm(cmd, t(f"Скачать {pick.size / 1e9:.1f} ГБ?", f"Download {pick.size / 1e9:.1f} GB?")):
            print(ui.dim(t("  оставляю ", "  keeping ") + cfg.model))
            return
        code, _ = runner.run(cmd)
        if code != 0:
            ui.problem(t(f"не удалось скачать {pick.cand.tag}, оставляю {cfg.model}", f"couldn't download {pick.cand.tag}, keeping {cfg.model}"))
            return
    if pick.cand.tag != cfg.model:
        llm.unload(cfg.ollama_url, cfg.model)  # don't keep two models in VRAM
    cfg.model = pick.cand.tag
    ui.say(cfg.name, ui.green(t("модель: ", "model: ") + cfg.model))
    _offer_big_model(cfg, fits)


def _offer_limits() -> None:
    """Ollama's prompt cache may grow to 8 GB of RAM until the system kills it (hardware.OLLAMA_LIMITS)."""
    if not hardware.ollama_limits_missing():
        return
    ui.panel(t("Ограничить память Ollama", "Limit Ollama's memory"), [
        t("Внутри Ollama кэш промптов по умолчанию может занять до 8 ГБ оперативной памяти, и при поиске",
          "Inside Ollama the prompt cache may take up to 8 GB of RAM by default; when searching the web it"),
        t("в интернете он растёт, пока система не убьёт Ollama. Ограничу его 1 ГБ — скорость не пострадает.",
          "grows until the system kills Ollama. I'll limit it to 1 GB — speed doesn't suffer."),
    ], "accent2", "◆")
    cmd = hardware.ollama_limits_cmd()
    if ui.confirm(cmd, t("Применить (перезапустит Ollama)?", "Apply (restarts Ollama)?")):
        code, _ = runner.run(cmd)
        if code != 0:
            ui.problem(t("не получилось — Ollama работает без ограничения", "that failed — Ollama runs without the limit"))
        else:
            hardware.wait_for_ollama()


def _offer_big_model(cfg: config.Config, fits: list) -> None:
    """The cascade (fast model asks the big one when unsure) needs the big model downloaded."""
    big = next((f for f in fits if f.cand.tag == cfg.big_model), None)
    # on CPU the 9B takes many seconds per request: the cascade would make the slow path common
    if not big or big.installed or cfg.big_model == cfg.model or big.verdict == "marginal" or big.mode != "gpu":
        return
    ui.panel(t("Вторая модель для сложных случаев", "A second model for hard cases"), [
        t(f"{ui.bold(cfg.big_model)} ({big.size / 1e9:.1f} ГБ) подключается, только когда {cfg.model} не уверена,",
          f"{ui.bold(cfg.big_model)} ({big.size / 1e9:.1f} GB) steps in only when {cfg.model} is unsure,"),
        t("и для «подумай хорошенько» / -d. В тестах это подняло точность с 92% до 98%.",
          "and for “think hard” / -d. In tests this raised accuracy from 92% to 98%."),
    ], "accent2", "◆")
    cmd = ["ollama", "pull", cfg.big_model]
    if ui.confirm(cmd, t(f"Скачать {big.size / 1e9:.1f} ГБ?", f"Download {big.size / 1e9:.1f} GB?")):
        code, _ = runner.run(cmd)
        if code != 0:
            ui.problem(t(f"не удалось скачать {cfg.big_model} — работаю с одной моделью", f"couldn't download {cfg.big_model} — working with one model"))


def _provider_rows(cfg: config.Config) -> list[tuple[str, str]]:
    usage = web.tavily_usage()
    has_key, no_key = ui.green(t("ключ есть", "key set")), ui.dim(t("нет ключа", "no key"))
    tav = (has_key + ui.dim(t(" · в этом месяце ", " · this month ") + f"{usage['used']}/{web.TAVILY_FREE}")
           + (ui.red(t(" · лимит исчерпан", " · limit reached")) if usage["exhausted"] else "")) if web.SETTINGS.tavily_key \
        else no_key
    brave = has_key if web.SETTINGS.brave_key else no_key
    searx = ui.dim(t("не настроен", "not set up"))
    if cfg.searx_url:
        try:
            web.get(cfg.searx_url.rstrip("/") + "/healthz", 3)
            searx = ui.green(t("работает · ", "works · ") + cfg.searx_url)
        except OSError:
            searx = ui.red(t("не отвечает · ", "not responding · ") + cfg.searx_url)
    return [("1. Tavily API", tav), ("2. Brave API", brave), ("3. SearXNG", searx),
            ("4. DuckDuckGo", ui.dim(t("без ключа, запасной", "no key, fallback")))]


def _ask_key(cfg: config.Config, field: str, provider: str, hint: str) -> None:
    try:
        key = getpass.getpass(ui.rgb("accent2", "❯ ") + f"{hint} " +
                              ui.dim(t("(ввод скрыт; Enter — пропустить)", "(input hidden; Enter — skip)")) + ": ").strip()
    except EOFError:
        return
    if not key:
        return
    old = getattr(web.SETTINGS, field)
    setattr(web.SETTINGS, field, key)
    try:
        with ui.working(t(f"проверяю ключ {provider}…", f"checking the {provider} key…")):
            ok = web.PROVIDERS[provider]("linux kernel", 1, None)
    except (OSError, ValueError, web.WebError) as e:
        setattr(web.SETTINGS, field, old)
        ui.problem(t(f"ключ {provider} не сработал: {e}", f"the {provider} key didn't work: {e}"))
        return
    setattr(cfg, field, key)
    config.save(cfg)
    ui.say(cfg.name, ui.green(t(f"{provider} подключён ({len(ok)} результат на тестовый запрос)",
                                f"{provider} connected ({len(ok)} results for a test query)")))


def search_step(cfg: config.Config) -> int:
    """Shows the search chain, takes API keys, tests that searching actually works."""
    ui.kv(t("Поиск в интернете — по порядку, следующий подхватывает при сбое",
            "Web search — in order, the next one takes over on failure"), _provider_rows(cfg), "accent", "⌕")
    link = ui.bold(ui.link("app.tavily.com", "https://app.tavily.com"))
    ui.panel(t("Самый надёжный вариант — Tavily", "The most reliable option — Tavily"), [
        t("• 1000 запросов в месяц бесплатно, каждый месяц заново, карта не нужна",
          "• 1000 free searches a month, renewed monthly, no card needed"),
        t(f"• ключ: зарегистрируйся на {link} → API Keys → скопируй ключ вида tvly-…",
          f"• key: sign up at {link} → API Keys → copy the key that looks like tvly-…"),
        ui.dim(t("Повторные вопросы берутся из кэша на сутки и запросов не тратят.",
                 "Repeated questions come from a one-day cache and don't use up searches.")),
    ], "accent2", "★")
    _ask_key(cfg, "tavily_key", "tavily", t("ключ Tavily", "Tavily key"))
    _ask_key(cfg, "brave_key", "brave", t("ключ Brave Search API ", "Brave Search API key ") +
             ui.dim(t("(необязательно, нужна карта)", "(optional, needs a card)")))
    failed = t("Поиск не работает", "Search doesn't work")
    try:
        with ui.working(t("проверяю поиск целиком…", "testing the whole search chain…")):
            res = web.search("linux kernel", n=3, cache=False)
    except web.WebError as e:
        ui.problem(str(e), title=failed)
        return 1
    if not res:
        ui.problem(t("поисковики ответили, но ничего не нашли — похоже, их временно ограничили",
                     "the search engines answered but found nothing — they seem to be rate-limited for now"),
                   t("подожди пару минут или подключи Tavily", "wait a couple of minutes or connect Tavily"), title=failed)
        return 1
    ui.say(cfg.name, ui.green(t(f"поиск работает: ответил {web.LAST_PROVIDER}, результатов: {len(res)}",
                                f"search works: {web.LAST_PROVIDER} answered, results: {len(res)}")))
    return 0
