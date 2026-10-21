"""First-run setup: introduce the agent, ask how it should talk, pick a model for this hardware."""

import getpass
import shutil

from shellmate import config, hardware, llm, persona, runner, ui, web

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


def run(cfg: config.Config) -> None:
    ui.banner(cfg.name, "локальный помощник в терминале · модель работает на этом компьютере, без облаков")
    ui.panel("Пиши мне своими словами — на русском или английском", [
        *(f"{ui.dim('$')} {ui.bold(cfg.name)} {e}" for e in EXAMPLES),
        f"{ui.dim('$')} paru -S foo 2>&1 | {ui.bold(cfg.name)} explain",
        "",
        ui.dim("Перед любым действием я покажу точную команду и спрошу разрешения."),
    ], "accent", "✦")

    keys = list(persona.STYLES)
    ui.panel("Как мне с тобой общаться?", [
        *(f"{ui.rgb('accent2', str(i))}  {persona.STYLES[k].title}" for i, k in enumerate(keys, 1)),
        f"{ui.rgb('accent2', str(len(keys) + 1))}  Свой вариант — опишешь сам",
    ], "accent2", "☺")
    ans = _prompt("")
    custom = ""
    if ans.isdigit() and 1 <= int(ans) <= len(keys):
        cfg.style = keys[int(ans) - 1]
    elif ans == str(len(keys) + 1):
        custom = _prompt("Опиши стиль своими словами: ")
    elif ans and not ans.isdigit():
        custom = ans  # typed a description straight into the menu — take it as is
    if custom:
        cfg.style, cfg.style_custom = "custom", custom
    elif not cfg.style:
        cfg.style = persona.CUSTOM_BASE

    cfg.address = _prompt(f"Как к тебе обращаться? {ui.dim('(имя, «босс», что угодно; Enter — никак)')} ")
    color_step(cfg)
    model_step(cfg)
    path = config.save(cfg)
    print()
    ui.say(cfg.name, persona.phrase(cfg, "hello"))
    print(ui.dim(f"   настройки: {path} · стиль: {cfg.name} --setup · модель: {cfg.name} --pick-model\n"))


def color_step(cfg: config.Config) -> None:
    keys = list(ui.PRESETS)
    ui.panel("Какого я буду цвета?", [
        *(f"{ui.rgb('accent2', str(i))}  {ui.swatch(cfg.name, k)}  {ui.dim(ui.PRESETS[k][0])}"
          for i, k in enumerate(keys, 1)),
        ui.dim("…или свой: #ff8800 (можно два: #ff8800,#00ffaa). Поменять потом: "
               f"{cfg.name} --color"),
    ], "accent2", "◐")
    ans = _prompt(f"номер или #hex {ui.dim('(Enter — оставить)')}: ")
    if ans.isdigit() and 1 <= int(ans) <= len(keys):
        cfg.color = keys[int(ans) - 1]
    elif ui.resolve_theme(ans):
        cfg.color = ui.theme_name(ans)
    ui.set_theme(cfg.color)
    ui.say(cfg.name, f"вот так: {ui.swatch(cfg.name, cfg.color)}")


def model_step(cfg: config.Config) -> None:
    """Checks the hardware, recommends a model (llmfit-style fit + speed estimate), pulls it."""
    print()
    with ui.working("проверяю железо и смотрю размеры моделей…"):
        hw = hardware.detect(cfg.ollama_url)
        fits = [f for f in hardware.evaluate(hw, cfg.ollama_url) if f.verdict != "too_tight"]
    ui.kv("Железо", hardware.describe(hw), "accent", "⚙")
    if not fits:
        ui.problem("Ни одна модель не помещается в память — нужно хотя бы ~2 ГБ свободной RAM.")
        return
    best = fits[0]
    verdict_color = {"perfect": "ok", "good": "accent", "marginal": "warn"}
    lines = []
    for i, f in enumerate(fits[:5], 1):
        where = "видеокарта" if f.mode == "gpu" else "процессор"
        mark = ui.green(" ✓ скачана") if f.installed else ""
        star = ui.rgb("accent2", "★") if f is best else " "
        lines.append(f"{star} {ui.rgb('accent2', str(i))}  {ui.bold(f.cand.tag.ljust(13))} "
                     f"{ui.rgb(verdict_color[f.verdict], hardware.VERDICTS[f.verdict].ljust(8))} "
                     f"{where:<10} ~{f.tps:>3.0f} ток/с  {f.size / 1e9:.1f} ГБ{mark}")
        if f.cand.note:
            lines.append(f"      {ui.dim(f.cand.note)}")
    pool = "видеопамяти" if best.mode == "gpu" else "оперативной памяти"
    lines += ["", f"{ui.rgb('accent2', '★')} Рекомендую {ui.bold(best.cand.tag)}: займёт "
                  f"{best.utilization:.0%} {pool}, скорость ~{best.tps:.0f} ток/с."]
    ui.panel("Какая модель подойдёт", lines, "accent2", "◆")
    if not shutil.which("ollama"):
        cfg.model = best.cand.tag
        ui.panel("Нужна Ollama", [
            f"{ui.dim('$')} {hardware.ollama_install(hw)}",
            f"{ui.dim('$')} ollama pull {best.cand.tag}"], "warn", "!")
        return
    if not hardware.ollama_running(cfg.ollama_url):  # installed, service stopped: pull would fail
        cfg.model = best.cand.tag
        ui.panel("Ollama не запущена", [
            f"{ui.dim('$')} sudo systemctl enable --now ollama",
            f"потом ещё раз: {ui.dim('$')} {cfg.name} --pick-model"], "warn", "!")
        return
    ans =_prompt(f"Enter — взять рекомендованную, номер — другую {ui.dim(f'(сейчас: {cfg.model})')}: ")
    pick = fits[int(ans) - 1] if ans.isdigit() and 1 <= int(ans) <= len(fits[:5]) else best
    if not pick.installed:
        cmd = ["ollama", "pull", pick.cand.tag]
        if not ui.confirm(cmd, f"Скачать {pick.size / 1e9:.1f} ГБ?"):
            print(ui.dim(f"  оставляю {cfg.model}"))
            return
        code, _ = runner.run(cmd)
        if code != 0:
            ui.problem(f"не удалось скачать {pick.cand.tag}, оставляю {cfg.model}")
            return
    if pick.cand.tag != cfg.model:
        llm.unload(cfg.ollama_url, cfg.model)  # don't keep two models in VRAM
    cfg.model = pick.cand.tag
    ui.say(cfg.name, ui.green(f"модель: {cfg.model}"))
    _offer_big_model(cfg, fits)


def _offer_big_model(cfg: config.Config, fits: list) -> None:
    """The cascade (fast model asks the big one when unsure) needs the big model downloaded."""
    big = next((f for f in fits if f.cand.tag == cfg.big_model), None)
    if not big or big.installed or cfg.big_model == cfg.model or big.verdict == "marginal":
        return
    ui.panel("Вторая модель для сложных случаев", [
        f"{ui.bold(cfg.big_model)} ({big.size / 1e9:.1f} ГБ) подключается, только когда {cfg.model} не уверена,",
        "и для «подумай хорошенько» / -d. В тестах это подняло точность с 92% до 98%.",
    ], "accent2", "◆")
    cmd = ["ollama", "pull", cfg.big_model]
    if ui.confirm(cmd, f"Скачать {big.size / 1e9:.1f} ГБ?"):
        code, _ = runner.run(cmd)
        if code != 0:
            ui.problem(f"не удалось скачать {cfg.big_model} — работаю с одной моделью")


def _provider_rows(cfg: config.Config) -> list[tuple[str, str]]:
    usage = web.tavily_usage()
    tav = (ui.green("ключ есть") + ui.dim(f" · в этом месяце {usage['used']}/{web.TAVILY_FREE}")
           + (ui.red(" · лимит исчерпан") if usage["exhausted"] else "")) if web.SETTINGS.tavily_key \
        else ui.dim("нет ключа")
    brave = ui.green("ключ есть") if web.SETTINGS.brave_key else ui.dim("нет ключа")
    searx = ui.dim("не настроен")
    if cfg.searx_url:
        try:
            web.get(cfg.searx_url.rstrip("/") + "/healthz", 3)
            searx = ui.green(f"работает · {cfg.searx_url}")
        except OSError:
            searx = ui.red(f"не отвечает · {cfg.searx_url}")
    return [("1. Tavily API", tav), ("2. Brave API", brave), ("3. SearXNG", searx),
            ("4. DuckDuckGo", ui.dim("без ключа, запасной"))]


def _ask_key(cfg: config.Config, field: str, provider: str, hint: str) -> None:
    try:
        key = getpass.getpass(ui.rgb("accent2", "❯ ") + f"{hint} {ui.dim('(ввод скрыт; Enter — пропустить)')}: ").strip()
    except EOFError:
        return
    if not key:
        return
    old = getattr(web.SETTINGS, field)
    setattr(web.SETTINGS, field, key)
    try:
        with ui.working(f"проверяю ключ {provider}…"):
            ok = web.PROVIDERS[provider]("linux kernel", 1, None)
    except (OSError, ValueError, web.WebError) as e:
        setattr(web.SETTINGS, field, old)
        ui.problem(f"ключ {provider} не сработал: {e}")
        return
    setattr(cfg, field, key)
    config.save(cfg)
    ui.say(cfg.name, ui.green(f"{provider} подключён ({len(ok)} результат на тестовый запрос)"))


def search_step(cfg: config.Config) -> int:
    """Shows the search chain, takes API keys, tests that searching actually works."""
    ui.kv("Поиск в интернете — по порядку, следующий подхватывает при сбое", _provider_rows(cfg),
          "accent", "⌕")
    ui.panel("Самый надёжный вариант — Tavily", [
        "• 1000 запросов в месяц бесплатно, каждый месяц заново, карта не нужна",
        "• ключ: зарегистрируйся на " + ui.bold(ui.link("app.tavily.com", "https://app.tavily.com")) +
        " → API Keys → скопируй ключ вида tvly-…",
        ui.dim("Повторные вопросы берутся из кэша на сутки и запросов не тратят."),
    ], "accent2", "★")
    _ask_key(cfg, "tavily_key", "tavily", "ключ Tavily")
    _ask_key(cfg, "brave_key", "brave", f"ключ Brave Search API {ui.dim('(необязательно, нужна карта)')}")
    try:
        with ui.working("проверяю поиск целиком…"):
            res = web.search("linux kernel", n=3, cache=False)
    except web.WebError as e:
        ui.problem(str(e), title="Поиск не работает")
        return 1
    if not res:
        ui.problem("поисковики ответили, но ничего не нашли — похоже, их временно ограничили",
                   "подожди пару минут или подключи Tavily", title="Поиск не работает")
        return 1
    ui.say(cfg.name, ui.green(f"поиск работает: ответил {web.LAST_PROVIDER}, результатов: {len(res)}"))
    return 0
