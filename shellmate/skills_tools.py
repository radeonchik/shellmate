"""Intent handlers for tools: explaining a command before it runs, what broke after an update,
whether a game runs here.
Mixed into cli.App, so `self` has say/phrase/execute/cfg/_translate."""

import shutil
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from shellmate import afterupdate, backends, cmdcheck, games, library, manpages, news, packages, router, system, ui, web
from shellmate.packages import Pkg, run_quiet
from shellmate.i18n import plural, t
from shellmate.prompts import lang_of, ui_language

LEVEL = {"danger": ("err", "✗"), "caution": ("warn", "!"), "info": ("accent", "·")}


def known_program(prog: str) -> bool:
    return bool(shutil.which(prog)) or library.tldr(prog) is not None or prog in cmdcheck.BUILTINS


class ToolSkills:
    # --- what does this command do --------------------------------------------------------------

    def check_command(self, text: str, query: str = "") -> int:
        """Explains a command without running it: what each program and option does (tldr and man,
        verbatim), what it would change, and the risks found by cmdcheck's rules."""
        parsed = cmdcheck.parse(text)
        if parsed.error:
            self.say(ui.yellow(t(f"не могу разобрать команду: {parsed.error}", f"can't parse the command: {parsed.error}")))
            return 1
        lang = lang_of(query) if query else ui_language()
        want_ru = lang in ("Russian", "Ukrainian")
        blocks, english = [], []  # english: texts to translate, in order
        for c in _unique(parsed.simples()):
            blocks.append(self._describe(c, want_ru, english))
        shown = english
        if english and want_ru:
            with ui.working(t("перевожу описания из man и tldr…", "translating the descriptions…")):
                shown = self._translate(english, lang) or english
        # several commands: the whole line first, then each part
        lines = [ui.bold(text if len(text) < 300 else text[:300] + "…"), ""] if len(blocks) > 1 else []
        for block in blocks:
            for kind, value in block:
                if kind == "cmd":
                    lines.append(ui.rgb("accent2", "▸ ") + ui.bold(value))
                elif kind == "note":
                    lines.append(f"    {value}")
                else:  # ("en", (index, prefix)): a translated quote with its original under it
                    i, prefix = value
                    lines.append(f"    {prefix}«{shown[i]}»")
                    if shown[i] != english[i]:
                        lines.append(f"    {' ' * ui.width(prefix)}{ui.dim(ui.italic(english[i]))}")
        ui.panel(t("Что делает команда", "What the command does"), lines, "accent", "❯")
        found = cmdcheck.risks(parsed, text)
        self._verdict(found, cmdcheck.effects(parsed))
        script = next((f.url for f in found if f.url), "")
        if script and sys.stdin.isatty() and self.ask_yes(
                t("Скачать этот скрипт и показать, что в нём — не запуская?", "Download this script and show what's in it — without running it?")):
            return self.script_report(script)
        return 0

    def _describe(self, c: cmdcheck.Simple, want_ru: bool, english: list[str]) -> list[tuple]:
        """Lines for one simple command; quotes needing translation go into `english`."""
        name = f"{c.prog} {c.sub}".strip()
        out: list[tuple] = [("cmd", c.text() if len(c.text()) < 120 else c.text()[:120] + "…")]
        if not c.prog:
            return out

        def quote(s: str, prefix: str = "") -> None:
            english.append(s)
            out.append(("en", (len(english) - 1, prefix)))

        page = library.tldr(f"{c.prog}-{c.sub}", "ru" if want_ru else "en") if c.sub else None
        page = page or library.tldr(c.prog, "ru" if want_ru else "en")
        man = manpages.page(f"{c.prog}-{c.sub}") if c.sub else ""
        sub_page = bool(man)
        man = man or manpages.page(c.prog)
        if page and page.description:
            if page.lang == "en":
                quote(page.description)
            else:
                out.append(("note", f"«{page.description}»"))
        elif man and manpages.summary(man):
            quote(manpages.summary(man))
        elif not known_program(c.prog):
            out.append(("note", ui.yellow(t(f"{c.prog}: такой программы нет на этом компьютере",
                                            f"{c.prog}: no such program on this computer"))))
        if c.sub and man and not sub_page:
            desc = manpages.command(man, c.sub)
            if desc:
                quote(desc, f"{ui.bold(c.sub)} — ")
        section = manpages.section_for(c.prog, c.flags())
        for f in c.flags()[:8]:
            desc = manpages.option(man, f, section) if man else ""
            if desc:
                quote(desc, f"{ui.bold(f)} — ")
        example = _matching_example(page, c) if page else None
        if example:
            out.append(("note", ui.dim(t("как в примере tldr: ", "like the tldr example: ") + f"«{example}»")))
        if not (page or man):
            out.append(("note", ui.dim(t(f"нет описания {name} в man и tldr", f"no description of {name} in man or tldr"))))
        return out

    def _verdict(self, found: list[cmdcheck.Finding], e: cmdcheck.Effects) -> None:
        lines = []
        for f in found:
            color, icon = LEVEL[f.level]
            lines.append(f"{ui.rgb(color, icon)} {f.text}")
        changes = []
        if e.root:
            changes.append(t("работает с правами root (sudo) — может менять всю систему", "runs as root (sudo) — can change the whole system"))
        for label, items in ((t("удалит", "deletes"), e.deletes), (t("запишет", "writes"), e.writes),
                             (t("пакеты", "packages"), e.packages), (t("службы", "services"), e.services),
                             (t("сеть", "network"), e.network)):
            if items:
                changes.append(f"{label}: {', '.join(items[:6])}" + (" …" if len(items) > 6 else ""))
        if changes:
            lines += ([""] if lines else []) + [ui.dim(t("что изменит:", "what it changes:"))] + [f"  {c}" for c in changes]
        if not lines:
            lines = [ui.green(t("ничего опасного не нашёл: команда ничего не удаляет, не ставит и не пишет в систему",
                                "nothing dangerous found: the command deletes, installs and writes nothing"))]
        worst = found[0].level if found else ""
        color = {"danger": "err", "caution": "warn"}.get(worst, "ok")
        title = {"danger": t("Опасно", "Dangerous"), "caution": t("Осторожно", "Careful")}.get(worst, t("Вердикт", "Verdict"))
        ui.panel(title, lines, color, {"danger": "✗", "caution": "!"}.get(worst, "✓"))

    # --- what broke after the update -------------------------------------------------------------

    def afterupdate(self, it, query: str) -> int:
        be = packages.backend()
        sub = it.text if it.text in afterupdate.SUBSYSTEMS else afterupdate.subsystem_of(query)
        with ui.working(t("читаю журнал пакетов и системный журнал…", "reading the package log and the system journal…")):
            report = afterupdate.analyze(be.transactions(), sub, lambda path: (be.owner(path) or (None,))[0])
        if not report:
            self.say(ui.yellow(t("в журнале пакетов нет ни одного обновления", "the package log has no updates at all")))
            return 1
        upd = report.update
        top = afterupdate.important(upd.upgrades)
        lines = [ui.bold(upd.time.strftime("%Y-%m-%d %H:%M")) + ui.dim(f" · {ago(upd.time)} · ") +
                 plural(len(upd.changes), ("пакет", "пакета", "пакетов"), ("package", "packages")), ""]
        shown = (afterupdate.subsystem_changes(upd, sub) or top)[:8]
        lines += [f"  {afterupdate.version_arrow(c)}" for c in shown]
        if len(upd.changes) > len(shown):
            lines.append(ui.dim(t(f"  …и ещё {len(upd.changes) - len(shown)}", f"  …and {len(upd.changes) - len(shown)} more")))
        ui.panel(t("Последнее обновление", "The last update"), lines, "accent", "⟳")
        for note in report.notes:
            ui.panel(t("Нужна перезагрузка", "A reboot is needed"), [note], "warn", "⟳")
        if report.errors:
            head = t("Новые ошибки после обновления", "New errors after the update") if report.journal_covers else \
                t("Ошибки этой загрузки (журнал не помнит, что было до обновления)",
                  "Errors of this boot (the journal doesn't remember what was before the update)")
            err_lines = []
            for s in report.errors:
                err_lines.append(f"{ui.yellow(('×' + str(s.error.count)).rjust(4))} {ui.bold(s.error.source)}: {s.error.message[:110]}")
                if s.changes:
                    label = t("из пакета", "from the package") if s.strong else t("возможно связано", "possibly related")
                    err_lines.append(ui.dim(f"       → {label}: " + ", ".join(afterupdate.version_arrow(c) for c in s.changes[:3])))
            ui.panel(head, err_lines, "warn", "⚑")
        elif report.journal_covers:
            self.say(ui.green(t("новых ошибок после обновления в журнале нет", "no new errors in the journal since the update")))
        else:
            self.say(ui.green(t("ошибок в журнале этой загрузки нет", "no errors in this boot's journal")))
        if be.news_feeds():
            with ui.working(t("смотрю новости дистрибутива за те дни…", "checking the distribution news of those days…")):
                items = [i for i in news.since(upd.time - timedelta(days=21)) if i.date <= upd.time + timedelta(days=3)]
            self.show_news(items, None, empty_ok=False)
        return self._afterupdate_menu(report, query)

    def _afterupdate_menu(self, report, query: str) -> int:
        upd = report.update
        suspects = list(dict.fromkeys(c.name for s in report.errors for c in s.changes if c.action in ("upgraded", "downgraded")))
        while sys.stdin.isatty():
            options, actions = [], []
            if suspects:
                options.append(t("откатить: ", "roll back: ") + ", ".join(suspects[:4]))
                actions.append(lambda: self.downgrade(router.Intent("downgrade", suspects[:4]), query))
            if report.errors:
                options.append(t("объяснить ошибку…", "explain an error…"))
                actions.append(lambda: self._explain_one(report))
            options.append(t(f"все пакеты этого обновления ({len(upd.changes)})", f"every package of this update ({len(upd.changes)})"))
            actions.append(lambda: ui.panel(t("Пакеты обновления", "The update's packages"),
                                            [afterupdate.version_arrow(c) for c in afterupdate.important(upd.changes)], "accent", "⟳") or 0)
            if packages.installed_version("snapper") or packages.installed_version("timeshift"):
                options.append(t("как откатить всю систему снимком", "how to roll the whole system back with a snapshot"))
                actions.append(lambda: ui.panel(t("Откат всей системы", "Rolling back the whole system"), [
                    t("снимком btrfs: открой btrfs-assistant или Timeshift, или выбери снимок в меню загрузчика",
                      "with a btrfs snapshot: open btrfs-assistant or Timeshift, or pick a snapshot in the boot menu")], "accent", "🛟") or 0)
            i = ui.choose(options, t("что дальше >", "what next >"))
            if i is None:
                return 0
            actions[i]()
        return 0

    def _explain_one(self, report) -> int:
        i = ui.choose([f"{s.error.source}: {s.error.message[:120]}" for s in report.errors], t("какую >", "which >"))
        if i is None:
            return 0
        e = report.errors[i].error
        return self.explain_text(f"{e.source}: {e.message}", "")

    # --- games ----------------------------------------------------------------------------------

    def game(self, it, query: str) -> int:
        name = (it.text or "").strip()
        if not name:
            return self.game_ready()
        with ui.working(t(f"ищу «{name}» в Steam и GOG…", f"looking for “{name}” on Steam and GOG…")), ThreadPoolExecutor(2) as ex:
            steam, gog = ex.submit(games.steam_search, name), ex.submit(games.gog_search, name)
            found = steam.result() + gog.result()
        if not found:
            self.say(ui.yellow(t(f"не нашёл «{name}» ни в Steam, ни в GOG", f"couldn't find “{name}” on Steam or GOG")))
            return 1
        exact = [g for g in found if games.same_name(g.name, name)]
        # the same game in both stores: Steam's has the ProtonDB data
        exact_steam = [g for g in exact if g.store == "steam"]
        if len(exact_steam) == 1 or len(exact) == 1:
            game = (exact_steam or exact)[0]
        elif len(found) == 1:
            game = found[0]
        else:
            i = ui.choose([f"{g.name}  {ui.dim(g.store.upper())}" + (ui.dim(" · Linux") if g.linux else "") for g in found],
                          t("какая игра >", "which game >"))
            if i is None:
                return 1
            game = found[i]
        return self._game_report(game)

    def _game_report(self, game: games.Game) -> int:
        appid = game.id if game.store == "steam" else ""
        twin = None
        if game.store == "gog":  # Proton reports are about the Steam version of the same game
            twin = next((g for g in games.steam_search(game.name, 3) if games.same_name(g.name, game.name)), None)
            appid = twin.id if twin else ""
        with ui.working(t("смотрю ProtonDB, Steam Deck и античит…", "checking ProtonDB, Steam Deck and anti-cheat…")), ThreadPoolExecutor(4) as ex:
            summary = ex.submit(games.protondb_summary, appid) if appid else None
            deck = ex.submit(games.deck_status, appid) if appid else None
            cheat = ex.submit(games.anticheat, appid, game.name)
            lutris = ex.submit(games.lutris_slug, game.name) if game.store == "gog" else None
            summary, deck, cheat = summary.result() if summary else None, deck.result() if deck else "", cheat.result()
            lutris = lutris.result() if lutris else ""
        tier_ru = {"platinum": "платина — работает идеально", "gold": "золото — работает после мелкой настройки",
                   "silver": "серебро — работает с проблемами", "bronze": "бронза — запускается, но играть тяжело",
                   "borked": "не работает", "pending": "мало отчётов", "native": "нативная версия"}
        tier_en = {"platinum": "platinum — works perfectly", "gold": "gold — works after minor tweaks", "silver": "silver — works with issues",
                   "bronze": "bronze — starts, but hard to play", "borked": "doesn't work", "pending": "too few reports", "native": "native"}
        lines = []
        native = game.linux or (twin.linux if twin else False)
        lines.append((t("Linux", "Linux"), ui.green(t("есть нативная версия", "has a native version")) if native else
                      t("только Windows — запускается через Proton", "Windows only — runs through Proton")))
        tier = (summary or {}).get("tier", "")
        if tier:
            color = ui.green if games.TIERS.get(tier, 0) >= 4 else ui.yellow if games.TIERS.get(tier, 0) >= 2 else ui.red
            trend = (summary or {}).get("trendingTier", "")
            short = {"platinum": t("платина", "platinum"), "gold": t("золото", "gold"), "silver": t("серебро", "silver"),
                     "bronze": t("бронза", "bronze"), "borked": t("не работает", "borked")}
            lines.append(("ProtonDB", color(t(tier_ru.get(tier, tier), tier_en.get(tier, tier))) + ui.dim(
                " · " + plural(int(summary.get("total") or 0), ("отчёт", "отчёта", "отчётов"), ("report", "reports")) +
                (t(f" · сейчас: {short.get(trend, trend)}", f" · lately: {short.get(trend, trend)}") if trend and trend != tier else ""))))
        elif appid:
            lines.append(("ProtonDB", ui.dim(t("отчётов нет", "no reports"))))
        if deck:
            label = {"verified": (ui.green, t("проверена", "verified")), "playable": (ui.yellow, t("играбельна", "playable")),
                     "unsupported": (ui.red, t("не поддерживается", "unsupported"))}[deck]
            lines.append(("Steam Deck", label[0](label[1])))
        if cheat:
            meaning = {"Supported": (ui.green, t("поддерживает Linux — онлайн работает", "supports Linux — online works")),
                       "Running": (ui.green, t("работает", "works")),
                       "Planned": (ui.yellow, t("поддержку Linux обещают", "Linux support is planned")),
                       "Broken": (ui.red, t("не работает на Linux — онлайн недоступен", "broken on Linux — no online play")),
                       "Denied": (ui.red, t("разработчики запретили Linux — онлайн недоступен", "the developers block Linux — no online play"))}
            color, text = meaning.get(cheat.status, (ui.dim, cheat.status))
            lines.append((t("Античит", "Anti-cheat"), color(text) + ui.dim(f" · {', '.join(cheat.anticheats)}" if cheat.anticheats else "")))
        if game.store == "gog":
            lines.append(("GOG", t("ставь через Heroic Games Launcher", "install it with the Heroic Games Launcher") +
                          (ui.dim(f" · Lutris: lutris.net/games/{lutris}/") if lutris else "")))
            if not twin:
                lines.append(("", ui.dim(t("в Steam такой игры нет — отчётов ProtonDB нет", "not on Steam — no ProtonDB reports"))))
        links = [game.url] + ([f"https://www.protondb.com/app/{appid}"] if appid else [])
        ui.kv(f"{game.name} · {game.store.upper()}", lines + [("", ui.dim("  ".join(ui.link(u, u) for u in links if u)))], "accent", "🎮")
        if appid and not native:
            self._game_similar(appid)
        return 0

    def _game_similar(self, appid: str) -> None:
        """Real reports from people with the same GPU generation (the ProtonDB export, downloaded once)."""
        mine = _my_gpu_class()
        if not games.dump_info():
            if not (sys.stdin.isatty() and self.ask_yes(t(
                    "Скачать отчёты пользователей ProtonDB (~72 МБ), чтобы сравнить с твоим железом?",
                    "Download the ProtonDB user reports (~72 MB) to compare with your hardware?"))):
                return
            with ui.working(t("скачиваю отчёты ProtonDB…", "downloading the ProtonDB reports…")) as w:
                try:
                    games.update_dump(lambda m: w.update(m))
                except (games.src.SourceError, OSError) as e:
                    ui.problem(t(f"не удалось: {e}", f"that failed: {e}"))
                    return
        sim = games.similar(appid, mine)
        if not sim:
            self.say(ui.dim(t("отчётов с похожей видеокартой нет", "no reports with a similar GPU")))
            return
        share = sim.works / sim.total
        color = ui.green if share >= 0.8 else ui.yellow if share >= 0.5 else ui.red
        lines = [color(t(f"работает у {sim.works} из {sim.total} ({share:.0%})", f"works for {sim.works} of {sim.total} ({share:.0%})")) +
                 ui.dim(t(f" · за последний год: {sim.recent}", f" · in the last year: {sim.recent}"))]
        proton = [(p, n) for p, n in sim.proton if n > 1]
        if proton:
            lines.append(t("чаще всего с: ", "most often with: ") + ", ".join(f"{p} ({n})" for p, n in proton))
        for opts, n in sim.launch:
            lines.append(t("параметры запуска: ", "launch options: ") + ui.bold(opts) + ui.dim(f" ×{n}"))
        notes = [n for _, _, n in sim.notes[:2]]
        shown = notes
        if notes and ui_language() in ("Russian", "Ukrainian"):
            with ui.working(t("перевожу отзывы…", "translating the reports…")):
                shown = [" ".join(x.split()) for x in self._translate(notes, ui_language()) or notes]
        for (date, gpu, original), text in zip(sim.notes, shown):
            lines += ["", ui.dim(f"{date} · {gpu}"), f"«{text}»"]
            if text != original:
                lines.append(ui.dim(ui.italic(original)))
        dump = games.dump_info()
        lines += ["", ui.dim(t(f"данные: ProtonDB ({dump[0] if dump else ''}, лицензия ODbL)", f"data: ProtonDB ({dump[0] if dump else ''}, ODbL license)"))]
        ui.panel(t(f"На похожем железе ({sim.label})", f"On similar hardware ({sim.label})"), lines, "quote", "❝")

    def game_ready(self) -> int:
        d = backends.distro()
        flatpaks = set(run_quiet(["flatpak", "list", "--app", "--columns=application"]).split()) if shutil.which("flatpak") else set()
        foreign = run_quiet(["dpkg", "--print-foreign-architectures"]) if d.family == "debian" else ""
        try:
            max_map = int(Path("/proc/sys/vm/max_map_count").read_text())
        except (OSError, ValueError):
            max_map = 0
        have = lambda what: bool(packages.installed_version(what[4:])) if what.startswith("pkg:") else bool(shutil.which(what))  # noqa: E731
        with ui.working(t("проверяю драйверы, Vulkan и игровые программы…", "checking drivers, Vulkan and gaming tools…")):
            checks = games.readiness(d.family, _gpu_names(), have, flatpaks, foreign_arch=foreign, max_map=max_map)
        lines = []
        for c in checks:
            mark = ui.green("✓") if c.ok else (ui.dim("○") if c.optional else ui.red("✗"))
            lines.append(f"{mark} {c.what}")
            if not c.ok and c.hint:
                lines.append(ui.dim(f"    {c.hint}"))
        ui.panel(t("Готовность к играм", "Ready for games"), lines, "accent", "🎮")
        needed = [p for c in checks if not c.ok and not c.optional for p in c.fix]
        extra = [p for c in checks if not c.ok and c.optional for p in c.fix]
        if not needed and not extra:
            self.say(ui.green(t("для игр всё на месте", "everything for games is in place")))
            return 0
        if "cachyos" in d.ids and len(needed) + len(extra) >= 3 and not packages.installed_version("cachyos-gaming-meta"):
            self.say(ui.dim(t("на CachyOS всё нужное ставит один пакет: cachyos-gaming-meta", "on CachyOS one package installs everything: cachyos-gaming-meta")))
        want = needed or extra
        if not sys.stdin.isatty() or not self.ask_yes(t(f"Поставить {', '.join(want)}?", f"Install {', '.join(want)}?")):
            return 0
        pkgs = [Pkg("repo", n, "") for n in want]
        return self.execute(packages.install_commands(pkgs), t("для игр: ", "for games: ") + ", ".join(want),
                            {"kind": "remove", **self._by_kind(pkgs)})

    def script_report(self, url: str) -> int:
        """What a downloaded install script would do, line by line, without running any of it."""
        with ui.working(t("скачиваю скрипт (не запускаю)…", "downloading the script (not running it)…")):
            try:
                ctype, body = web.get(url, 20)
            except OSError as e:
                ui.problem(t(f"не удалось скачать: {e}", f"couldn't download it: {e}"))
                return 1
        lines = cmdcheck.script_lines(body)
        found, effects, sudo = [], cmdcheck.Effects(), 0
        for n, line in lines:
            p = cmdcheck.parse(line)
            if p.error:
                continue
            sudo += any(c.root for c in p.simples())
            found += [(n, f) for f in cmdcheck.risks(p, line) if f.level != "info" or "root" in f.text]
            e = cmdcheck.effects(p)
            for name in ("writes", "deletes", "packages", "services", "network"):
                getattr(effects, name).extend(x for x in getattr(e, name) if x not in getattr(effects, name))
        host = urllib.parse.urlsplit(url).netloc
        out = [t(f"{len(body.splitlines())} строк · команд: {len(lines)} · с sudo: {sudo}",
                 f"{len(body.splitlines())} lines · commands: {len(lines)} · with sudo: {sudo}")]
        for label, items in ((t("качает с", "downloads from"), effects.network), (t("ставит / удаляет пакеты", "installs / removes packages"), effects.packages),
                             (t("пишет в", "writes to"), effects.writes), (t("удаляет", "deletes"), effects.deletes),
                             (t("службы", "services"), effects.services)):
            if items:
                out.append(f"{label}: " + ", ".join(items[:8]) + (" …" if len(items) > 8 else ""))
        order = {"danger": 0, "caution": 1, "info": 2}
        found.sort(key=lambda x: (order[x[1].level], x[0]))
        if found:
            out.append("")
            for n, f in found[:12]:
                color, icon = LEVEL[f.level]
                out.append(f"{ui.rgb(color, icon)} {ui.dim(t('строка', 'line') + f' {n}:')} {f.text}")
                if f.where:
                    out.append(f"    {ui.dim(f.where[:110])}")
            if len(found) > 12:
                out.append(ui.dim(t(f"…и ещё {len(found) - 12}", f"…and {len(found) - 12} more")))
        ui.panel(t(f"Что внутри скрипта с {host}", f"What's inside the script from {host}"), out,
                 "err" if any(f.level == "danger" for _, f in found) else "warn" if found else "ok", "§")
        return 0


def ago(when: datetime) -> str:
    d = datetime.now().astimezone() - when
    if d < timedelta(hours=1):
        return t("меньше часа назад", "less than an hour ago")
    if d < timedelta(days=1):
        return plural(int(d.total_seconds() // 3600), ("час", "часа", "часов"), ("hour", "hours")) + t(" назад", " ago")
    return plural(d.days, ("день", "дня", "дней"), ("day", "days")) + t(" назад", " ago")


def _gpu_names() -> list[str]:
    return [g.split(" · ")[0] for g in system.gpus()]


def _my_gpu_class() -> str:
    """The GPU games run on: the discrete one when there are two."""
    classes = [games.gpu_class(g) for g in _gpu_names()]
    return next((c for c in classes if c and "igpu" not in c), next((c for c in classes if c), ""))


def _unique(cmds: list[cmdcheck.Simple]) -> list[cmdcheck.Simple]:
    seen, out = set(), []
    for c in cmds:
        if c.argv and not c.prog.startswith("__SUB") and c.text() not in seen:
            seen.add(c.text())
            out.append(c)
    return out


def _matching_example(page, c: cmdcheck.Simple) -> str:
    """The tldr example closest to this command: the same subcommand and most of the same options."""
    mine = set(c.flags())
    best, score = "", 0.0
    for what, cmd in page.examples:
        ex = cmdcheck.parse(cmd).simples()
        if not ex or ex[0].prog != c.prog or (c.sub and ex[0].sub != c.sub):
            continue
        theirs = set(ex[0].flags())
        if not mine and not theirs:
            s = 0.5
        else:
            s = len(mine & theirs) / len(mine | theirs)
        if s > score:
            best, score = what.rstrip(":"), s
    return best if score >= 0.6 else ""
