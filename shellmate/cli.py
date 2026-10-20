"""Entry point. Usage: <agent-name> <request in natural language>

The agent's name is whatever the executable is called (install.sh makes a symlink),
so `shellmate install obs` and `friday install obs` are the same program with separate configs."""

import argparse
import dataclasses
import difflib
import os
import re
import stat
import sys
import time
from pathlib import Path

from shellmate import (config, disk, docs, errors, llm, news, onboarding, packages, persona, router, runner,
                    state, system, ui, updates, web)
from shellmate.packages import Pkg
from shellmate.prompts import CAPABILITIES, CHAT_TASK, defang, explain_task, lang_of
from shellmate.skills_knowledge import KnowledgeSkills
from shellmate.skills_system import SystemSkills


def pick_color(name: str, current: str) -> str | None:
    """Menu of presets rendered in their own colors; returns the chosen spec or None."""
    keys = list(ui.PRESETS)
    lines = [f"{ui.swatch(name, k)}  {ui.PRESETS[k][0]}{ui.dim('  ← сейчас') if k == current else ''}"
             for k in keys] + [f"{ui.dim('#')}  свой цвет: напиши «{name} сделай себя #ff8800»"]
    i = ui.choose(lines, "цвет >")
    return keys[i] if i is not None and i < len(keys) else None


def agent_name() -> str:
    n = Path(sys.argv[0]).name
    return "shellmate" if n.startswith("python") or n.endswith(".py") else n


def read_piped() -> str:
    """Reads stdin only when something is really piped or redirected into us — a bare
    non-tty stdin (cron, scripts, IDE runners) would otherwise block forever."""
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
    except (OSError, ValueError):
        return ""
    if sys.stdin.isatty() or not (stat.S_ISFIFO(mode) or stat.S_ISREG(mode)):
        return ""
    return sys.stdin.read().strip()


def fmt(p: Pkg) -> str:
    name = f"{p.title} ({p.name})" if p.title else p.name
    mark = ui.green(" ✓ установлен") if p.installed else ""
    return f"{ui.bold(name)} {ui.dim(p.source + ' ' + p.version)}{mark}  {p.desc}"


class App(SystemSkills, KnowledgeSkills):
    def __init__(self, cfg: config.Config, dry_run: bool):
        self.cfg = cfg
        self.dry = dry_run
        self.query = ""
        self.outcome: dict = {}  # what a handler actually did (real package names) — for follow-ups
        self.deep = False  # -d / "подумай хорошенько": the big model does everything
        self._big: str | None = None

    @property
    def big(self) -> str:
        """The big model if it's configured and actually downloaded, else ''."""
        if self._big is None:
            from shellmate import hardware
            name = self.cfg.big_model
            have = hardware.installed_models(self.cfg.ollama_url) if name and name != self.cfg.model else set()
            self._big = name if name in have or f"{name}:latest" in have else ""
        return self._big

    def model_for(self, risky: bool = False) -> str:
        """Fast model by default; the big one in deep mode and for risky steps (free-form advice)."""
        return self.big if (self.deep or risky) and self.big else self.cfg.model

    def say(self, msg: str) -> None:
        ui.say(self.cfg.name, msg)

    def phrase(self, key: str, **kw) -> str:
        return persona.phrase(self.cfg, key, **kw)

    def capabilities(self) -> str:
        return CAPABILITIES.format(name=self.cfg.name, sources=packages.sources_label(self.cfg.use_flatpak))

    # --- shared steps -------------------------------------------------------

    def pick(self, want: str, candidates: list[Pkg]) -> Pkg | None:
        if not candidates:
            self.say(ui.yellow(self.phrase("not_found", q=want)))
            return None
        i = ui.choose([fmt(p) for p in candidates], f"{want} >")
        return None if i is None else candidates[i]

    def execute(self, cmds: list[list[str]], label: str = "", undo: dict | None = None) -> int:
        """Confirms and runs commands; a fully successful run is written to the action log
        together with `undo` — data describing how to revert it (see App.undo)."""
        for cmd in cmds:
            if not ui.confirm(cmd, self.phrase("run")):
                self.say(self.phrase("cancelled"))
                return 1
            if self.dry:
                self.say(ui.dim("(dry-run, не выполняю)"))
                continue
            env = packages.backend().run_env if packages.backend().tool in " ".join(cmd[:3]) else None
            code, out = runner.run(cmd, env)
            if code != 0:
                self.diagnose(out)
                return code
        if not self.dry:
            state.log_action(self.cfg.name, self.query, label or " ".join(cmds[0][:3]), cmds, undo)
            self.say(ui.green(self.phrase("done")))
        return 0

    def diagnose(self, output: str) -> None:
        hits = errors.match(output, self.cfg.name)
        if hits:
            for what, fix in hits[:2]:
                ui.problem(what, fix)
            return
        self.say(ui.red(self.phrase("failed")))
        try:
            ask = input(f"  Поискать, что это значит? {ui.dim('[y/N]')} ").strip().lower()
        except EOFError:
            return
        if ask in ("y", "yes", "д", "да"):
            lines = [l for l in output.splitlines() if re.search(r"error|fail|ошибк|не удалось", l, re.I)]
            self.explain_text("\n".join(lines[-5:]) or output[-1500:], "")

    def ask_model(self, task: str, text: str, title: str = "") -> str:
        """Free-form reply. With a title it's shown as a panel (explanations), without — as speech."""
        msgs = [{"role": "system", "content": persona.system_prompt(self.cfg) + "\n\n" + task},
                {"role": "user", "content": text}]
        # explanations (with a title) are free-form claims without sources: the big model writes them
        model = self.model_for(risky=bool(title))
        with ui.working("думаю…" if model == self.cfg.model else f"думаю внимательнее ({model})…"):
            reply = llm.chat(self.cfg.ollama_url, model, msgs).strip()
        reply, removed = defang(reply)
        if removed:
            reply += ("\n\nЯ убрал из ответа команду, которая может удалить данные. Такие вещи "
                      "проверяй по документации, а не по моим догадкам.")
        if title:
            ui.panel(title, ui.render_markdown(reply).split("\n"), "accent", "✦")
        else:
            self.say(ui.render_markdown(reply))
        return reply

    # --- packages -----------------------------------------------------------

    def install(self, it: router.Intent, query: str) -> int:
        wants = it.packages or ([it.text] if it.text else [])
        if not wants:
            self.say(self.phrase("unclear", what="установить"))
            return 1
        chosen = []
        for want in wants:
            with ui.working(f"ищу «{want}» в репозиториях, AUR и Flatpak…"):
                found = packages.search(want, self.cfg.use_flatpak, self.cfg.max_results)
                found = packages.merge_with_spelling(found, query, want, self.cfg.use_flatpak,
                                                     self.cfg.max_results) if found else \
                    packages.fuzzy_search(query, want, self.cfg.use_flatpak, self.cfg.max_results)
            p = self.pick(want, found)
            if p:
                chosen.append(p)
        if not chosen:
            return 1
        try:
            return self._install(chosen)
        except RuntimeError as e:
            ui.problem(str(e))
            return 1

    def remove(self, it: router.Intent, query: str) -> int:
        if not it.packages:
            self.say(self.phrase("unclear", what="удалить"))
            return 1
        chosen = []
        for want in it.packages:
            p = self.pick(want, packages.installed_matching(want, self.cfg.use_flatpak))
            if p:
                chosen.append(p)
        if not chosen:
            return 1
        self.outcome["packages"] = [p.name for p in chosen]
        return self.execute(packages.remove_commands(chosen), "удаление: " + ", ".join(p.name for p in chosen),
                            {"kind": "install", **self._by_kind(chosen)})

    @staticmethod
    def _by_kind(pkgs: list[Pkg]) -> dict:
        return {"native": [p.name for p in pkgs if p.is_native], "flatpak": [p.name for p in pkgs if p.is_flatpak],
                "snap": [p.name for p in pkgs if p.is_snap]}

    def _install(self, chosen: list[Pkg]) -> int:
        self.outcome["packages"] = [p.name for p in chosen]
        return self.execute(packages.install_commands(chosen), "установка: " + ", ".join(p.name for p in chosen),
                            {"kind": "remove", **self._by_kind(chosen)})

    def search(self, it: router.Intent, query: str) -> int:
        want = " ".join(it.packages) or it.text
        if not want:
            self.say(self.phrase("unclear", what="искать"))
            return 1
        with ui.working(f"ищу «{want}»…"):
            found = packages.search(want, self.cfg.use_flatpak, self.cfg.max_results)
        p = self.pick(want, found)
        if not p:
            return 0
        return self._install([p])

    def show_news(self, items: list, when, empty_ok: bool = True) -> list:
        """Prints the news panel; returns the items that need action before updating."""
        months = "янв фев мар апр мая июн июл авг сен окт ноя дек".split()
        day = lambda d: f"{d.day} {months[d.month - 1]}"  # noqa: E731
        since = f" с прошлого обновления ({day(when)})" if when else ""
        if not items:
            if empty_ok:
                self.say(ui.dim(f"новостей Arch и CachyOS{since} нет"))
            return []
        lines = []
        for i in items:
            mark = ""
            if i.manual:
                mark = ui.red("  ⚠ нужно вмешательство") if i.affects else ui.dim("  (нужно вмешательство, но пакета у тебя нет)")
            who = ui.yellow("  касается: " + ", ".join(i.affects)) if i.affects else ""
            lines.append(f"{ui.dim(day(i.date).rjust(6))}  {ui.dim(i.source)}  "
                         f"{ui.link(ui.bold(i.title) if i.affects else i.title, i.link)}{mark}{who}")
        ui.panel(f"Новости{since}" if since else "Последние новости", lines, "accent", "✉")
        return [i for i in items if i.manual and i.affects]

    def news(self, it: router.Intent, query: str) -> int:
        with ui.working("читаю новости Arch и CachyOS…"):
            when = news.last_upgrade()
            items = news.since(when)
            latest = [] if items else news.since(None, limit=5)
        if items:
            self.show_news(items, when)
        else:
            self.show_news([], when)
            self.show_news(latest, None)
        return 0

    def update(self, it: router.Intent, query: str) -> int:
        with ui.working("читаю новости Arch и CachyOS с прошлого обновления…"):
            when = news.last_upgrade()
            items = news.since(when)
        urgent = self.show_news(items, when)
        for i in urgent:
            ui.panel(f"Перед обновлением: {i.title}", [*i.text[:900].split("\n"), "",
                     ui.dim(ui.link(i.link, i.link))], "err", "⚠")
        if urgent:
            try:
                ok = input(f"  Прочитал, продолжаем обновление? {ui.dim('[y/N]')} ").strip().lower()
            except EOFError:
                ok = ""
            if ok not in ("y", "yes", "д", "да"):
                self.say(self.phrase("cancelled"))
                return 1
        note, snapshot = packages.snapshot_plan()
        self.say(ui.dim(note))
        # no undo entry: an update is rolled back with a btrfs snapshot, not command by command
        return self.execute(([snapshot] if snapshot else []) + packages.update_commands(self.cfg.use_flatpak),
                            "обновление системы")

    def downgrade(self, it: router.Intent, query: str) -> int:
        if not it.packages:
            self.say(self.phrase("unclear", what="откатить"))
            if packages.installed_version("snapper"):
                ui.panel("Откат всей системы", ["снимком btrfs: открой btrfs-assistant или выбери "
                                                "снимок в меню загрузчика"], "accent", "🛟")
            return 1
        files, names = [], []
        for name in it.packages:
            cur = packages.installed_version(name)
            if not cur:
                self.say(ui.yellow(self.phrase("not_found", q=name)))
                continue
            versions = [(v, f) for v, f in packages.cached_versions(name) if v != cur]
            if not versions:
                self.say(ui.yellow(f"в кэше нет других версий {name} (сейчас {cur})."))
                continue
            lines = [f"{ui.bold(v)}  {ui.dim('старее' if packages.vercmp(v, cur) < 0 else 'новее')}"
                     for v, _ in versions]
            i = ui.choose(lines, f"{name} {cur} →")
            if i is not None:
                files.append(str(versions[i][1]))
                names.append(name)
        if not files:
            return 1
        self.outcome["packages"] = names
        rc = self.execute([packages.backend().downgrade_cmd(files)], "откат: " + ", ".join(names),
                          {"kind": "upgrade", "names": names})
        hold = packages.backend().hold_hint(names)
        if rc == 0 and hold:
            ui.panel("Дальше", [
                f"• чтобы следующее обновление не вернуло новую версию, {hold}",
                "• связанные пакеты (например nvidia-utils и nvidia-open) откатывай вместе — "
                "иначе pacman сообщит о конфликте"], "warn", "!")
        return rc

    # --- disk -----------------------------------------------------------------

    def disk(self, it: router.Intent, query: str) -> int:
        mounts = disk.mounts()
        tw = max(len(m.target) for m in mounts)
        ui.panel("Диски", [f"{ui.bold(m.target.ljust(tw))}  {ui.bar(m.pct)} {m.pct:>3}%  свободно "
                           f"{ui.bold(disk.human(m.avail))} из {disk.human(m.size)}  {ui.dim(m.fstype)}"
                           for m in mounts], "accent", "◔")
        with ui.working("ищу, что можно почистить…"):
            cands = disk.cleanup_candidates()
        if not cands:
            self.say(self.phrase("done"))
            return 0
        while cands:
            i = ui.choose([f"{ui.bold(disk.human(c.size) if c.size is not None else '?'):>10}  {c.title}"
                           for c in cands], "почистить >")
            if i is None:
                return 0
            c = cands.pop(i)
            if self.execute([c.cmd], f"очистка: {c.title}") != 0:
                return 1
        return 0

    def bigfiles(self, it: router.Intent, query: str) -> int:
        root = disk.resolve_path(it.text)
        with ui.working(f"сканирую {docs.short(str(root))}…"):
            entries = disk.biggest(root)
        if not entries:
            self.say(self.phrase("not_found", q=str(root)))
            return 0
        while entries:
            lines = [f"{ui.bold(disk.human(e.size)):>10}  {'📁' if e.is_dir else '  '} {docs.short(e.path)}"
                     for e in entries]
            i = ui.choose(lines, "в корзину >")  # Esc = просто посмотреть и выйти
            if i is None:
                return 0
            # trash, not rm: a mistake stays recoverable
            path = entries.pop(i).path
            if self.execute([["gio", "trash", path]], f"в корзину: {docs.short(path)}",
                            {"kind": "untrash", "path": path}) != 0:
                return 1
        return 0

    # --- talk -----------------------------------------------------------------

    def explain(self, it: router.Intent, query: str) -> int:
        return self.explain_text(it.text or query, query)

    def explain_text(self, text: str, query: str) -> int:
        hits = errors.match(text, self.cfg.name)
        for what, fix in hits[:2]:
            ui.problem(what, fix)
        if hits:
            return 0
        # not in the dictionary: official docs, then the open web; the model's own knowledge last
        if self.from_trusted(query or text, text[-500:]) or self.from_web(query or text, text[-300:]):
            return 0
        self.ask_model(explain_task(lang_of(query or text)),
                       text if text == query else f"{query}\n\n{text}",
                       title="Объяснение · без источников, может ошибаться")
        return 0

    def theme(self, it: router.Intent, query: str) -> int:
        wanted = next((t for t in (it.text, query) if ui.resolve_theme(t)), "")
        spec = ui.theme_name(wanted) if wanted else pick_color(self.cfg.name, self.cfg.color)
        if spec is None:
            return 0
        self.cfg.color = spec
        config.save(self.cfg)
        ui.set_theme(spec)
        self.say(f"теперь я выгляжу так: {ui.swatch(self.cfg.name, spec)}")
        return 0

    # --- undo / history -----------------------------------------------------------

    def _undo_cmds(self, spec: dict) -> list[list[str]] | None:
        kind = spec.get("kind")
        if kind in ("remove", "install", "upgrade"):  # install <-> remove; upgrade = back to the repo version
            pkgs = [Pkg("repo", n, "") for n in spec.get("native", spec.get("names", []))] + \
                   [Pkg("flatpak", n, "") for n in spec.get("flatpak", [])] + \
                   [Pkg("snap", n, "") for n in spec.get("snap", [])]
            return packages.remove_commands(pkgs) if kind == "remove" else packages.install_commands(pkgs)
        if kind == "service":
            return [system.Unit(spec["unit"], "", "", spec.get("user", False)).cmd(spec["action"])]
        if kind == "untrash":  # find the file in the trash now — its trash name may have a suffix
            uris = [line.split("\t")[0] for line in packages.run_quiet(["gio", "trash", "--list"]).splitlines()
                    if line.split("\t")[-1] == spec["path"]]
            return [["gio", "trash", "--restore", uris[-1]]] if uris else None
        return None

    def undo(self, it: router.Intent, query: str) -> int:
        e = state.last_undoable(self.cfg.name)
        if not e:
            self.say("отменять нечего: последних действий с откатом нет.")
            if packages.installed_version("snapper"):
                print(f"  {ui.dim('→')} обновление системы откатывается снимком: btrfs-assistant или меню загрузчика")
            return 1
        cmds = self._undo_cmds(e["undo"])
        when = time.strftime("%d.%m %H:%M", time.localtime(e["time"]))
        if not cmds:
            ui.problem(f"не получается отменить «{e['label']}»", "файл уже убран из корзины?" if
                       e["undo"].get("kind") == "untrash" else "")
            return 1
        ui.panel("Отменяю", [f"{ui.bold(e['label'])}  {ui.dim(when)}",
                             ui.dim(f"по запросу: {e['query']}") if e.get("query") else ""], "warn", "↶")
        rc = self.execute(cmds, f"отмена: {e['label']}")
        if rc == 0 and not self.dry:
            state.mark_undone(self.cfg.name, e["id"])
        return rc

    def history(self, it: router.Intent, query: str) -> int:
        entries = state.history(self.cfg.name)
        if not entries:
            self.say("я пока ничего не делал.")
            return 0
        lines = []
        for e in reversed(entries):
            mark = ui.dim(" ↶ отменено") if e.get("undone") else (ui.dim(" · можно отменить") if e.get("undo") else "")
            lines.append(f"{ui.dim(time.strftime('%d.%m %H:%M', time.localtime(e['time'])))}  "
                         f"{ui.bold(e['label'])}{mark}")
        ui.panel("Что я делал", lines, "accent", "↶")
        return 0

    # --- shell integration ----------------------------------------------------------

    def not_found(self, cmd: str) -> int:
        """fish/bash/zsh 'command not found' hook: no model, must answer in a blink."""
        lines, cands = [], []
        execs = {f for d in os.environ.get("PATH", "").split(":") if os.path.isdir(d) for f in os.listdir(d)}
        # typos: close spellings, plus swapped letters ("gti" -> git) that similarity scores miss
        swapped = [e for e in execs if len(e) == len(cmd) and e != cmd and sorted(e) == sorted(cmd)]
        near = list(dict.fromkeys(swapped + difflib.get_close_matches(cmd, execs, n=3, cutoff=0.75)))[:3]
        if near:
            lines.append("может, имел в виду: " + ", ".join(ui.bold(n) for n in near))
        be = packages.backend()
        found = be.provides(cmd)
        if found is None:
            if be.provides_setup:
                lines.append(ui.dim(f"чтобы я подсказывал, в каком пакете команда, {be.provides_hint} — "
                                    f"один раз: {be.provides_setup_text}"))
        elif found:
            cands = found
            lines.append("есть в пакетах: " + ", ".join(f"{ui.bold(p.name)} {ui.dim(p.source)}" for p in cands))
        ui.panel(f"Команды «{cmd}» нет", lines or [ui.dim("и похожих тоже нет")], "warn", "?")
        if cands:
            p = cands[0] if len(cands) == 1 else None
            if p is None:
                i = ui.choose([fmt(c) for c in cands], "установить >")
                p = cands[i] if i is not None else None
            if p:
                self.query = f"{cmd} (command not found)"
                self._install([p])
        return 127

    def self_update(self) -> int:
        cmd, how = updates.update_command()
        ui.kv("Обновление", [("Сейчас", updates.version()), ("Установлен через", how),
                             ("Последний релиз", updates.latest_release(self.cfg.name, force=True) or "—")],
              "accent", "↻")
        if not cmd:
            self.say("обнови так же, как ставил (uv tool upgrade shellmate / pipx upgrade shellmate).")
            return 1
        rc = self.execute([cmd], "обновление shellmate")
        if rc == 0 and cmd[0] == "git":
            print(ui.dim("   рабочая копия обновлена; если менялись зависимости: uv tool install --editable --force ."))
        return rc

    def chat(self, it: router.Intent, query: str) -> int:
        self.ask_model(CHAT_TASK.format(lang=lang_of(query), capabilities=self.capabilities()), query)
        return 0

    def handle(self, query: str, debug: bool = False) -> int:
        asked_deep, query = router.wants_deep(query)
        context = state.load_context(self.cfg.name) if router.is_follow_up(query) else []
        deep_before, self.deep = self.deep, self.deep or asked_deep
        model = self.model_for()
        if self.deep and self.big:
            self.say(ui.dim(f"хорошо, подумаю внимательнее — отвечает {self.big}"))
        with ui.working("думаю…" if model == self.cfg.model else f"думаю внимательнее ({model})…"):
            it = router.classify(self.cfg.ollama_url, model, query, context)
        first = it
        if model != self.big and self.big:
            reasons = router.doubts(it, query, self.cfg.escalate_intent, self.cfg.escalate_packages)
            if reasons:  # the fast model is unsure (or confidently contradicts the request)
                with ui.working(f"{reasons[0]} — перепроверяю ({self.big})…"):
                    it = router.classify(self.cfg.ollama_url, self.big, query, context)
                if debug:
                    print(ui.dim(f"  перепроверка: {'; '.join(reasons)}"))
        if debug:
            via = f"{first.model} → {it.model}" if first is not it else it.model
            print(ui.dim(f"[{via}] {it.fields()} · уверенность {it.intent_p:.2f}/{it.packages_p:.2f}"
                         + (f" · контекст: {len(context)}" if context else "")))
        self.query, self.outcome = query, {}
        try:
            return getattr(self, it.intent)(it, query)
        finally:
            state.save_context(self.cfg.name, query, it.fields(), self.outcome)
            self.deep = deep_before  # "подумай" applies to this request, not the whole session

    def unknown(self, it: router.Intent, query: str) -> int:
        self.say(self.phrase("unknown"))
        ui.panel("Что я умею", [f"• {line.strip()}" for line in self.capabilities().splitlines()], "accent", "◆")
        return 1


EXIT_WORDS = {"exit", "quit", "q", "выход", "пока", "bye", ":q"}


def repl(app: "App", debug: bool) -> int:
    """`shellmate` with no request: a conversation. The model stays loaded, follow-ups have context,
    ↑/↓ browse earlier requests."""
    import readline
    hist = state.state_dir(app.cfg.name) / "repl_history"
    try:
        readline.read_history_file(hist)
    except OSError:
        pass
    readline.set_history_length(1000)
    app.say(app.phrase("hello"))
    print(ui.dim("   пиши запросы как обычно · выход: Ctrl+D или «выход»\n"))
    try:
        while True:
            try:
                line = input(ui.prompt()).strip()
            except EOFError:
                print()
                break
            except KeyboardInterrupt:  # Ctrl+C clears the line, doesn't quit
                print()
                continue
            if not line:
                continue
            if line.lower() in EXIT_WORDS:
                break
            try:
                app.handle(line, debug)
            except KeyboardInterrupt:
                print()
            except llm.LLMError as e:
                ui.problem(str(e), title="Модель недоступна")
            print()
    finally:
        readline.write_history_file(hist)
    return 0


FISH_HOOK = """# Installed by `{name} --install-shell`. Delete this file to get fish's default back.
function fish_command_not_found
    if command -q {name}
        command {name} --not-found $argv[1]
    else
        __fish_default_command_not_found_handler $argv
    end
end
"""


def install_shell(name: str) -> int:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    hook = base / "fish" / "functions" / "fish_command_not_found.fish"
    hook.parent.mkdir(parents=True, exist_ok=True)
    if hook.exists() and "--install-shell" not in hook.read_text():
        hook.rename(hook.with_suffix(".fish.bak"))  # someone's own handler: keep it
    hook.write_text(FISH_HOOK.format(name=name))
    ui.panel("Готово: fish будет спрашивать меня о ненайденных командах", [
        f"файл: {docs.short(str(hook))} — удали его, чтобы вернуть как было",
        ui.dim("открой новый терминал или выполни: source " + docs.short(str(hook))),
        "",
        ui.dim("для bash/zsh добавь в ~/.bashrc или ~/.zshrc:"),
        ui.dim(f'  command_not_found_handle()  {{ {name} --not-found "$1"; return 127; }}   # bash'),
        ui.dim(f'  command_not_found_handler() {{ {name} --not-found "$1"; return 127; }}   # zsh'),
    ], "ok", "✓")
    return 0


def main(argv: list[str] | None = None) -> int:
    name = agent_name()
    parser = argparse.ArgumentParser(
        prog=name, description="Локальный терминальный помощник. Пиши запрос своими словами.",
        epilog=f"Например: {name} установи пж обс студио",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", nargs="*", help="запрос своими словами")
    parser.add_argument("-n", "--dry-run", action="store_true", help="показать команды, но не выполнять")
    parser.add_argument("-m", "--model", help="модель Ollama (переопределяет конфиг)")
    parser.add_argument("-d", "--deep", action="store_true",
                        help="сложная задача: всё делает большая модель (как «подумай хорошенько»)")
    parser.add_argument("--setup", action="store_true", help="заново выбрать стиль общения")
    parser.add_argument("--pick-model", action="store_true", help="проверить железо и подобрать модель")
    parser.add_argument("--color", nargs="?", const="", metavar="ЦВЕТ",
                        help="цвет помощника: тема (shellmate, matrix, sunset…), #hex или без значения — меню")
    parser.add_argument("--add-docs", metavar="DIR", help="добавить папку в источники документов")
    parser.add_argument("--search-setup", action="store_true", help="подключить поисковые API и проверить поиск")
    parser.add_argument("--news", action="store_true", help="новости Arch и CachyOS с прошлого обновления")
    parser.add_argument("--docs", action="store_true", help="показать папки-источники")
    parser.add_argument("--history", action="store_true", help="что я делал")
    parser.add_argument("--undo", action="store_true", help="отменить последнее действие")
    parser.add_argument("--install-shell", action="store_true",
                        help="подсказывать пакет, когда в fish набрана несуществующая команда")
    parser.add_argument("--not-found", metavar="КОМАНДА", help=argparse.SUPPRESS)  # used by the shell hook
    parser.add_argument("--selftest", action="store_true", help="проверить, как я работаю с этим дистрибутивом")
    parser.add_argument("--version", action="store_true", help="версия")
    parser.add_argument("--update", action="store_true", help="обновить меня до последней версии")
    parser.add_argument("--alias", metavar="ИМЯ", help="звать меня ещё и так (создаст команду ~/.local/bin/ИМЯ)")
    parser.add_argument("--debug", action="store_true", help="показать, что распознала модель")
    args = parser.parse_args(argv)

    cfg = config.load(name)
    ui.set_theme(cfg.color)
    web.configure(cfg.search_order, cfg.tavily_key, cfg.brave_key, cfg.searx_url)
    if args.model:
        cfg.model = args.model
    query = " ".join(args.request).strip()
    piped = read_piped()
    if args.selftest:  # read-only, no model, no onboarding: also what CI runs in distro containers
        from shellmate import selftest
        return selftest.run()
    if args.version:
        print(f"{name} {updates.version()}" + (f" · github.com/{updates.repo()}" if updates.repo() else ""))
        return 0
    if args.alias:
        ok, msg = updates.make_alias(args.alias)
        ui.say(name, ui.green(f"теперь меня можно звать и «{args.alias}»: {msg}") if ok else ui.red(msg))
        return 0 if ok else 1
    app = App(cfg, args.dry_run)
    app.deep = args.deep
    if args.not_found:  # shell hook: instant, never the onboarding, never the model
        try:
            return app.not_found(args.not_found)
        except KeyboardInterrupt:
            print()
            return 127
    if args.install_shell:
        return install_shell(name)
    try:
        if args.setup or (not cfg.style and sys.stdin.isatty() and not piped):
            onboarding.run(cfg)
            if args.setup or not query:
                return 0
        if args.pick_model:
            onboarding.model_step(cfg)
            config.save(cfg)
            return 0
        if args.color is not None:
            return app.theme(router.Intent("theme", text=args.color), args.color)
        if args.search_setup:
            return onboarding.search_step(cfg)
        if args.news:
            return app.news(router.Intent("news"), "")
        if args.update:
            return app.self_update()
        if args.history:
            return app.history(router.Intent("history"), "")
        if args.undo:
            app.query = "--undo"
            return app.undo(router.Intent("undo"), "")
        if args.add_docs:
            return app.docs_add(args.add_docs)
        if args.docs:
            files, chunks = docs.stats(name)
            ui.panel("Папки-источники", ([docs.short(d) for d in cfg.docs] or [ui.dim("нет")]) +
                     ["", ui.dim(f"в индексе {files} файлов, {chunks} фрагментов")], "source", "§")
            return 0
        if piped:  # `some-cmd 2>&1 | shellmate explain` — no routing needed
            return app.explain_text(piped, query)
        if not query:
            if sys.stdin.isatty():
                return repl(app, args.debug)
            app.say(app.phrase("hello"))
            return 0
        return app.handle(query, args.debug)
    except llm.LLMError as e:
        ui.problem(str(e), title="Модель недоступна")
        return 2
    except KeyboardInterrupt:
        print()
        return 130
    finally:
        if sys.stdout.isatty() and not args.update:
            tag = updates.newer_available(name)
            if tag:
                print(ui.dim(f"\n   доступна новая версия {tag} (у тебя {updates.version()}) — {name} --update"))
