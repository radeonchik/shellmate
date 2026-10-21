"""Entry point. Usage: <agent-name> <request in natural language>

The agent's name is whatever the executable is called (install.sh makes a symlink),
so `shellmate install obs` and `friday install obs` are the same program with separate configs."""

import argparse
import dataclasses
import difflib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

from shellmate import (config, disk, docs, errors, i18n, llm, news, onboarding, packages, persona, router, runner,
                    state, system, ui, updates, web)
from shellmate.i18n import plural, t
from shellmate.packages import Pkg
from shellmate.prompts import (CHAT_TASK, TRANSLATE_AGAIN, TRANSLATE_SCHEMA, TRANSLATE_TASK, capabilities,
                               defang, explain_task, lang_of, ui_language)
from shellmate.skills_knowledge import KnowledgeSkills
from shellmate.skills_system import SystemSkills


def pick_color(name: str, current: str) -> str | None:
    """Menu of presets rendered in their own colors; returns the chosen spec or None."""
    keys = list(ui.PRESETS)
    lines = [f"{ui.swatch(name, k)}  {ui.preset_label(k)}{ui.dim(t('  ← сейчас', '  ← current')) if k == current else ''}"
             for k in keys] + [f"{ui.dim('#')}  " + t(f"свой цвет: напиши «{name} сделай себя #ff8800»",
                                                     f"your own color: type “{name} make yourself #ff8800”")]
    i = ui.choose(lines, t("цвет >", "color >"))
    return keys[i] if i is not None and i < len(keys) else None


def thinking(model: str, fast: str) -> str:
    return t("думаю…", "thinking…") if model == fast else t(f"думаю внимательнее ({model})…", f"thinking harder ({model})…")


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
    mark = ui.green(t(" ✓ установлен", " ✓ installed")) if p.installed else ""
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
        return capabilities().format(name=self.cfg.name, sources=packages.sources_label(self.cfg.use_flatpak))

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
                self.say(ui.dim(t("(dry-run, не выполняю)", "(dry run, not executing)")))
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
            ask = input(f"  {t('Поискать, что это значит?', 'Look up what it means?')} {ui.dim('[y/N]')} ").strip().lower()
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
        with ui.working(thinking(model, self.cfg.model)):
            reply = llm.chat(self.cfg.ollama_url, model, msgs).strip()
        reply, removed = defang(reply)
        if removed:
            reply += "\n\n" + t("Я убрал из ответа команду, которая может удалить данные. Такие вещи "
                                "проверяй по документации, а не по моим догадкам.",
                                "I removed a command that could delete data from this answer. Check such "
                                "things in the documentation, not by my guesses.")
        if title:
            ui.panel(title, ui.render_markdown(reply).split("\n"), "accent", "✦")
        else:
            self.say(ui.render_markdown(reply))
        return reply

    # --- packages -----------------------------------------------------------

    def install(self, it: router.Intent, query: str) -> int:
        wants = it.packages or ([it.text] if it.text else [])
        if not wants:
            self.say(self.phrase("unclear", what=t("установить", "to install")))
            return 1
        chosen = []
        for want in wants:
            with ui.working(t(f"ищу «{want}» в репозиториях, AUR и Flatpak…", f"looking for “{want}” in the repos, AUR and Flatpak…")):
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
            self.say(self.phrase("unclear", what=t("удалить", "to remove")))
            return 1
        chosen = []
        for want in it.packages:
            p = self.pick(want, packages.installed_matching(want, self.cfg.use_flatpak))
            if p:
                chosen.append(p)
        if not chosen:
            return 1
        self.outcome["packages"] = [p.name for p in chosen]
        return self.execute(packages.remove_commands(chosen), t("удаление: ", "remove: ") + ", ".join(p.name for p in chosen),
                            {"kind": "install", **self._by_kind(chosen)})

    @staticmethod
    def _by_kind(pkgs: list[Pkg]) -> dict:
        return {"native": [p.name for p in pkgs if p.is_native], "flatpak": [p.name for p in pkgs if p.is_flatpak],
                "snap": [p.name for p in pkgs if p.is_snap]}

    def _install(self, chosen: list[Pkg]) -> int:
        self.outcome["packages"] = [p.name for p in chosen]
        return self.execute(packages.install_commands(chosen), t("установка: ", "install: ") + ", ".join(p.name for p in chosen),
                            {"kind": "remove", **self._by_kind(chosen)})

    def search(self, it: router.Intent, query: str) -> int:
        want = " ".join(it.packages) or it.text
        if not want:
            self.say(self.phrase("unclear", what=t("искать", "to look for")))
            return 1
        with ui.working(t(f"ищу «{want}»…", f"looking for “{want}”…")):
            found = packages.search(want, self.cfg.use_flatpak, self.cfg.max_results)
        p = self.pick(want, found)
        if not p:
            return 0
        return self._install([p])

    def show_news(self, items: list, when, empty_ok: bool = True) -> list:
        """Prints the news panel; returns the items that need action before updating."""
        months = t("янв фев мар апр мая июн июл авг сен окт ноя дек", "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec").split()
        day = lambda d: f"{d.day} {months[d.month - 1]}"  # noqa: E731
        since = t(f" с прошлого обновления ({day(when)})", f" since the last update ({day(when)})") if when else ""
        if not items:
            if empty_ok:
                self.say(ui.dim(t(f"новостей Arch и CachyOS{since} нет", f"no Arch or CachyOS news{since}")))
            return []
        lines = []
        for i in items:
            mark = ""
            if i.manual:
                mark = ui.red(t("  ⚠ нужно вмешательство", "  ⚠ manual intervention")) if i.affects else \
                    ui.dim(t("  (нужно вмешательство, но пакета у тебя нет)", "  (manual intervention, but you don't have the package)"))
            who = ui.yellow(t("  касается: ", "  affects: ") + ", ".join(i.affects)) if i.affects else ""
            lines.append(f"{ui.dim(day(i.date).rjust(6))}  {ui.dim(i.source)}  "
                         f"{ui.link(ui.bold(i.title) if i.affects else i.title, i.link)}{mark}{who}")
        ui.panel(t("Новости", "News") + since if since else t("Последние новости", "Latest news"), lines, "accent", "✉")
        return [i for i in items if i.manual and i.affects]

    def news(self, it: router.Intent, query: str) -> int:
        with ui.working(t("читаю новости Arch и CachyOS…", "reading Arch and CachyOS news…")):
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
        with ui.working(t("читаю новости Arch и CachyOS с прошлого обновления…", "reading Arch and CachyOS news since the last update…")):
            when = news.last_upgrade()
            items = news.since(when)
        urgent = self.show_news(items, when)
        for i in urgent:
            ui.panel(t("Перед обновлением: ", "Before updating: ") + i.title, [*i.text[:900].split("\n"), "",
                     ui.dim(ui.link(i.link, i.link))], "err", "⚠")
        if urgent:
            try:
                ok = input(f"  {t('Прочитал, продолжаем обновление?', 'Read it, continue the update?')} {ui.dim('[y/N]')} ").strip().lower()
            except EOFError:
                ok = ""
            if ok not in ("y", "yes", "д", "да"):
                self.say(self.phrase("cancelled"))
                return 1
        note, snapshot = packages.snapshot_plan()
        self.say(ui.dim(note))
        # no undo entry: an update is rolled back with a btrfs snapshot, not command by command
        return self.execute(([snapshot] if snapshot else []) + packages.update_commands(self.cfg.use_flatpak),
                            t("обновление системы", "system update"))

    def downgrade(self, it: router.Intent, query: str) -> int:
        if not it.packages:
            self.say(self.phrase("unclear", what=t("откатить", "to roll back")))
            if packages.installed_version("snapper"):
                ui.panel(t("Откат всей системы", "Rolling back the whole system"),
                         [t("снимком btrfs: открой btrfs-assistant или выбери снимок в меню загрузчика",
                            "with a btrfs snapshot: open btrfs-assistant or pick a snapshot in the boot menu")],
                         "accent", "🛟")
            return 1
        files, names = [], []
        for name in it.packages:
            cur = packages.installed_version(name)
            if not cur:
                self.say(ui.yellow(self.phrase("not_found", q=name)))
                continue
            versions = [(v, f) for v, f in packages.cached_versions(name) if v != cur]
            if not versions:
                self.say(ui.yellow(t(f"в кэше нет других версий {name} (сейчас {cur}).",
                                     f"no other versions of {name} in the cache (now {cur}).")))
                continue
            lines = [f"{ui.bold(v)}  {ui.dim(t('старее', 'older') if packages.vercmp(v, cur) < 0 else t('новее', 'newer'))}"
                     for v, _ in versions]
            i = ui.choose(lines, f"{name} {cur} →")
            if i is not None:
                files.append(str(versions[i][1]))
                names.append(name)
        if not files:
            return 1
        self.outcome["packages"] = names
        rc = self.execute([packages.backend().downgrade_cmd(files)], t("откат: ", "downgrade: ") + ", ".join(names),
                          {"kind": "upgrade", "names": names})
        hold = packages.backend().hold_hint(names)
        if rc == 0 and hold:
            ui.panel(t("Дальше", "Next"), [
                t(f"• чтобы следующее обновление не вернуло новую версию, {hold}",
                  f"• so that the next update doesn't bring the new version back, {hold}"),
                t("• связанные пакеты (например nvidia-utils и nvidia-open) откатывай вместе — "
                  "иначе pacman сообщит о конфликте",
                  "• roll related packages back together (e.g. nvidia-utils and nvidia-open), "
                  "otherwise pacman reports a conflict")], "warn", "!")
        return rc

    # --- disk -----------------------------------------------------------------

    def disk(self, it: router.Intent, query: str) -> int:
        mounts = disk.mounts()
        tw = max(len(m.target) for m in mounts)
        ui.panel(t("Диски", "Disks"), [f"{ui.bold(m.target.ljust(tw))}  {ui.bar(m.pct)} {m.pct:>3}%  "
                           + t(f"свободно {ui.bold(disk.human(m.avail))} из {disk.human(m.size)}",
                               f"free {ui.bold(disk.human(m.avail))} of {disk.human(m.size)}")
                           + f"  {ui.dim(m.fstype)}"
                           for m in mounts], "accent", "◔")
        with ui.working(t("ищу, что можно почистить…", "looking for things to clean up…")):
            cands = disk.cleanup_candidates()
        if not cands:
            self.say(self.phrase("done"))
            return 0
        while cands:
            i = ui.choose([f"{ui.bold(disk.human(c.size) if c.size is not None else '?'):>10}  {c.title}"
                           for c in cands], t("почистить >", "clean up >"))
            if i is None:
                return 0
            c = cands.pop(i)
            if self.execute([c.cmd], t("очистка: ", "cleanup: ") + c.title) != 0:
                return 1
        return 0

    def files(self, it: router.Intent, query: str) -> int:
        """What is in a folder: counted and grouped by code, the model only names the folder."""
        root = disk.resolve_path(it.text)
        kinds, folders, hidden = disk.folder_summary(root)
        total = sum(len(k.names) for k in kinds)
        def sample(names: list[str]) -> str:
            return ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
        groups = [(k.label, k.names) for k in kinds] + ([(t("папки", "folders"), folders)] if folders else [])
        nw = max((len(str(len(names))) for _, names in groups), default=1)
        rows = [(label, f"{ui.bold(str(len(names)).rjust(nw))}  {ui.dim(sample(names))}") for label, names in groups]
        head = plural(total, ("файл", "файла", "файлов"), ("file", "files")) + ", " + \
            plural(len(folders), ("папка", "папки", "папок"), ("folder", "folders")) + \
            (ui.dim(t(f" · скрытых: {hidden}", f" · hidden: {hidden}")) if hidden else "")
        self.say(f"{docs.short(str(root))}: {head}")
        if rows:
            ui.kv(t("Что лежит", "What's inside"), rows, "accent", "▤")
            print(ui.dim(t("   только сама папка, без вложенных", "   this folder only, not inside subfolders")))
        return 0

    def bigfiles(self, it: router.Intent, query: str) -> int:
        root = disk.resolve_path(it.text)
        with ui.working(t("сканирую ", "scanning ") + f"{docs.short(str(root))}…"):
            entries = disk.biggest(root)
        if not entries:
            self.say(self.phrase("not_found", q=str(root)))
            return 0
        while entries:
            lines = [f"{ui.bold(disk.human(e.size)):>10}  {'📁' if e.is_dir else '  '} {docs.short(e.path)}"
                     for e in entries]
            i = ui.choose(lines, t("в корзину >", "to trash >"))  # Esc = just look and leave
            if i is None:
                return 0
            # trash, not rm: a mistake stays recoverable
            path = entries.pop(i).path
            if self.execute([["gio", "trash", path]], t("в корзину: ", "to trash: ") + docs.short(path),
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
                       title=t("Объяснение · без источников, может ошибаться", "Explanation · no sources, may be wrong"))
        return 0

    def translate(self, it: router.Intent, query: str, text: str = "") -> int:
        """Free-form, but harmless: no commands come out of it. The big model writes it when there is one.
        The model names the target language first (JSON), then translates; a copy of the original
        (9B did that for "переведи «спасибо»") is caught by code and redone with the target spelled out."""
        source = text or it.text or query
        default = "English" if lang_of(source) == ui_language() else ui_language()
        model = self.model_for(risky=True)
        same = lambda a, b: re.sub(r"\W+", "", a.lower()) == re.sub(r"\W+", "", b.lower())  # noqa: E731
        with ui.working(thinking(model, self.cfg.model)):
            raw = llm.chat(self.cfg.ollama_url, model,
                           [{"role": "system", "content": TRANSLATE_TASK.format(target=default)},
                            {"role": "user", "content": f"Request: {query}\nText:\n{source}"}], schema=TRANSLATE_SCHEMA)
            try:
                out = json.loads(raw)
                target, result = out["target_language"], out["translation"].strip()
            except (ValueError, KeyError, AttributeError):
                target, result = default, ""
            if not result or same(result, source):
                if not result or target.lower()[:2] == lang_of(source).lower()[:2]:  # "Russian"/"ru" for Russian text
                    target = default
                result = llm.chat(self.cfg.ollama_url, model,
                                  [{"role": "system", "content": TRANSLATE_AGAIN},
                                   {"role": "user", "content": f"Translate into {target}:\n{source}"}]).strip()
        result, _ = defang(result)
        ui.panel(t("Перевод", "Translation"), ui.render_markdown(result).split("\n"), "accent", "✦")
        return 0

    def theme(self, it: router.Intent, query: str) -> int:
        wanted = next((t for t in (it.text, query) if ui.resolve_theme(t)), "")
        spec = ui.theme_name(wanted) if wanted else pick_color(self.cfg.name, self.cfg.color)
        if spec is None:
            return 0
        self.cfg.color = spec
        config.save(self.cfg)
        ui.set_theme(spec)
        self.say(t("теперь я выгляжу так: ", "this is how I look now: ") + ui.swatch(self.cfg.name, spec))
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
            self.say(t("отменять нечего: последних действий с откатом нет.", "nothing to undo: no recent actions that can be reverted."))
            if packages.installed_version("snapper"):
                print(f"  {ui.dim('→')} " + t("обновление системы откатывается снимком: btrfs-assistant или меню загрузчика",
                                             "a system update is rolled back with a snapshot: btrfs-assistant or the boot menu"))
            return 1
        cmds = self._undo_cmds(e["undo"])
        when = time.strftime("%d.%m %H:%M", time.localtime(e["time"]))
        if not cmds:
            ui.problem(t(f"не получается отменить «{e['label']}»", f"can't undo “{e['label']}”"),
                       t("файл уже убран из корзины?", "the file is no longer in the trash?") if
                       e["undo"].get("kind") == "untrash" else "")
            return 1
        ui.panel(t("Отменяю", "Undoing"), [f"{ui.bold(e['label'])}  {ui.dim(when)}",
                 ui.dim(t("по запросу: ", "requested as: ") + e["query"]) if e.get("query") else ""], "warn", "↶")
        rc = self.execute(cmds, t("отмена: ", "undo: ") + e["label"])
        if rc == 0 and not self.dry:
            state.mark_undone(self.cfg.name, e["id"])
        return rc

    def history(self, it: router.Intent, query: str) -> int:
        entries = state.history(self.cfg.name)
        if not entries:
            self.say(t("я пока ничего не делал.", "I haven't done anything yet."))
            return 0
        lines = []
        for e in reversed(entries):
            mark = ui.dim(t(" ↶ отменено", " ↶ undone")) if e.get("undone") else \
                (ui.dim(t(" · можно отменить", " · can be undone")) if e.get("undo") else "")
            lines.append(f"{ui.dim(time.strftime('%d.%m %H:%M', time.localtime(e['time'])))}  "
                         f"{ui.bold(e['label'])}{mark}")
        ui.panel(t("Что я делал", "What I did"), lines, "accent", "↶")
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
            lines.append(t("может, имел в виду: ", "did you mean: ") + ", ".join(ui.bold(n) for n in near))
        be = packages.backend()
        found = be.provides(cmd)
        if found is None:
            if be.provides_setup:
                lines.append(ui.dim(t(f"чтобы я подсказывал, в каком пакете команда, {be.provides_hint} — "
                                      f"один раз: {be.provides_setup_text}",
                                      f"to let me suggest which package has a command, {be.provides_hint}, "
                                      f"once: {be.provides_setup_text}")))
        elif found:
            cands = found
            lines.append(t("есть в пакетах: ", "available in packages: ") + ", ".join(f"{ui.bold(p.name)} {ui.dim(p.source)}" for p in cands))
        ui.panel(t(f"Команды «{cmd}» нет", f"No command “{cmd}”"),
                 lines or [ui.dim(t("и похожих тоже нет", "and nothing similar"))], "warn", "?")
        if cands:
            p = cands[0] if len(cands) == 1 else None
            if p is None:
                i = ui.choose([fmt(c) for c in cands], t("установить >", "install >"))
                p = cands[i] if i is not None else None
            if p:
                self.query = f"{cmd} (command not found)"
                self._install([p])
        return 127

    def self_update(self) -> int:
        cmd, how = updates.update_command()
        ui.kv(t("Обновление", "Update"), [(t("Сейчас", "Now"), updates.version()), (t("Установлен через", "Installed with"), how),
                                          (t("Последний релиз", "Latest release"),
                                           updates.latest_release(self.cfg.name, force=True) or "—")],
              "accent", "↻")
        if not cmd:
            self.say(t("обнови так же, как ставил (uv tool upgrade shellmate / pipx upgrade shellmate).",
                       "update the same way you installed me (uv tool upgrade shellmate / pipx upgrade shellmate)."))
            return 1
        rc = self.execute([cmd], t("обновление shellmate", "shellmate update"))
        if rc == 0 and cmd[0] == "git":
            print(ui.dim(t("   рабочая копия обновлена; если менялись зависимости: uv tool install --editable --force .",
                           "   working copy updated; if dependencies changed: uv tool install --editable --force .")))
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
            self.say(ui.dim(t(f"хорошо, подумаю внимательнее — отвечает {self.big}",
                              f"okay, I'll think harder — {self.big} answers")))
        with ui.working(thinking(model, self.cfg.model)):
            it = router.classify(self.cfg.ollama_url, model, query, context)
        first = it
        if model != self.big and self.big:
            reasons = router.doubts(it, query, self.cfg.escalate_intent, self.cfg.escalate_packages)
            if reasons:  # the fast model is unsure (or confidently contradicts the request)
                with ui.working(f"{reasons[0]} — " + t("перепроверяю", "double-checking") + f" ({self.big})…"):
                    it = router.classify(self.cfg.ollama_url, self.big, query, context)
                if debug:
                    print(ui.dim(t("  перепроверка: ", "  double-check: ") + "; ".join(reasons)))
        if debug:
            via = f"{first.model} → {it.model}" if first is not it else it.model
            print(ui.dim(f"[{via}] {it.fields()} · " + t("уверенность", "confidence") + f" {it.intent_p:.2f}/{it.packages_p:.2f}"
                         + (t(" · контекст: ", " · context: ") + str(len(context)) if context else "")))
        self.query, self.outcome = query, {}
        try:
            return getattr(self, it.intent)(it, query)
        finally:
            state.save_context(self.cfg.name, query, it.fields(), self.outcome)
            self.deep = deep_before  # "подумай" applies to this request, not the whole session

    def unknown(self, it: router.Intent, query: str) -> int:
        self.say(self.phrase("unknown"))
        ui.panel(t("Что я умею", "What I can do"), [f"• {line.strip()}" for line in self.capabilities().splitlines()], "accent", "◆")
        # the router may misjudge a plain question as "can't do": the web is one confirmation away
        if sys.stdin.isatty() and query and self.ask_yes(t("Поискать это в интернете?", "Search the web for it?")):
            return 0 if self.from_web(query, query, loud=True) else 1
        return 1

    @staticmethod
    def ask_yes(question: str) -> bool:
        try:
            return input(f"  {question} {ui.dim('[y/N]')} ").strip().lower() in ("y", "yes", "д", "да")
        except EOFError:
            return False


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
    print(ui.dim(t("   пиши запросы как обычно · выход: Ctrl+D или «выход»\n", "   type requests as usual · quit: Ctrl+D or “exit”\n")))
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
                ui.problem(str(e), title=t("Модель недоступна", "Model unavailable"))
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
    ui.panel(t("Готово: fish будет спрашивать меня о ненайденных командах", "Done: fish will ask me about commands it can't find"), [
        t(f"файл: {docs.short(str(hook))} — удали его, чтобы вернуть как было",
          f"file: {docs.short(str(hook))} — delete it to get the old behavior back"),
        ui.dim(t("открой новый терминал или выполни: source ", "open a new terminal or run: source ") + docs.short(str(hook))),
        "",
        ui.dim(t("для bash/zsh добавь в ~/.bashrc или ~/.zshrc:", "for bash/zsh add to ~/.bashrc or ~/.zshrc:")),
        ui.dim(f'  command_not_found_handle()  {{ {name} --not-found "$1"; return 127; }}   # bash'),
        ui.dim(f'  command_not_found_handler() {{ {name} --not-found "$1"; return 127; }}   # zsh'),
    ], "ok", "✓")
    return 0


def main(argv: list[str] | None = None) -> int:
    name = agent_name()
    cfg = config.load(name)
    argv = sys.argv[1:] if argv is None else argv
    # the language is needed before the parser: its help texts are translated
    lang_arg = next((a.split("=", 1)[1] for a in argv if a.startswith("--lang=")), None) or \
        next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--lang"), None)
    i18n.set_lang(lang_arg or cfg.lang)
    parser = argparse.ArgumentParser(
        prog=name, description=t("Локальный терминальный помощник. Пиши запрос своими словами.",
                                 "A local terminal assistant. Type your request in your own words."),
        epilog=t(f"Например: {name} установи пж обс студио", f"For example: {name} install obs studio please"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request", nargs="*", help=t("запрос своими словами", "the request in your own words"))
    parser.add_argument("-n", "--dry-run", action="store_true", help=t("показать команды, но не выполнять", "show the commands, don't run them"))
    parser.add_argument("-m", "--model", help=t("модель Ollama (переопределяет конфиг)", "Ollama model (overrides the config)"))
    parser.add_argument("-d", "--deep", action="store_true",
                        help=t("сложная задача: всё делает большая модель (как «подумай хорошенько»)",
                               "a hard task: the big model does everything (like “think hard”)"))
    parser.add_argument("--setup", action="store_true", help=t("заново выбрать стиль общения", "choose the communication style again"))
    parser.add_argument("--lang", choices=list(i18n.LANGS), help=t("язык интерфейса (запомню)", "interface language (remembered)"))
    parser.add_argument("--pick-model", action="store_true", help=t("проверить железо и подобрать модель", "check the hardware and pick a model"))
    parser.add_argument("--color", nargs="?", const="", metavar=t("ЦВЕТ", "COLOR"),
                        help=t("цвет помощника: тема (classic, matrix, sunset…), #hex или без значения — меню",
                               "the assistant's color: a theme (classic, matrix, sunset…), #hex, or nothing for a menu"))
    parser.add_argument("--add-docs", metavar="PATH", help=t("добавить папку или файл в источники документов",
                                                             "add a folder or a file to the document sources"))
    parser.add_argument("--search-setup", action="store_true", help=t("подключить поисковые API и проверить поиск", "connect search APIs and test the search"))
    parser.add_argument("--news", action="store_true", help=t("новости Arch и CachyOS с прошлого обновления", "Arch and CachyOS news since the last update"))
    parser.add_argument("--docs", action="store_true", help=t("показать папки-источники", "show the source folders"))
    parser.add_argument("--history", action="store_true", help=t("что я делал", "what I did"))
    parser.add_argument("--undo", action="store_true", help=t("отменить последнее действие", "undo the last action"))
    parser.add_argument("--install-shell", action="store_true",
                        help=t("подсказывать пакет, когда в fish набрана несуществующая команда",
                               "suggest a package when a missing command is typed in fish"))
    parser.add_argument("--not-found", metavar="CMD", help=argparse.SUPPRESS)  # used by the shell hook
    parser.add_argument("--selftest", action="store_true", help=t("проверить, как я работаю с этим дистрибутивом", "check how I work with this distribution"))
    parser.add_argument("--version", action="store_true", help=t("версия", "version"))
    parser.add_argument("--update", action="store_true", help=t("обновить меня до последней версии", "update me to the latest version"))
    parser.add_argument("--alias", metavar=t("ИМЯ", "NAME"), help=t("звать меня ещё и так (создаст команду ~/.local/bin/ИМЯ)",
                                                                    "call me this name too (creates the command ~/.local/bin/NAME)"))
    parser.add_argument("--debug", action="store_true", help=t("показать, что распознала модель", "show what the model recognized"))
    args = parser.parse_args(argv)

    if args.lang and args.lang != cfg.lang:
        cfg.lang = args.lang
        if cfg.style:  # first run: the onboarding saves it together with everything else
            config.save(cfg)
        if not args.request:
            ui.say(name, t("теперь говорю по-русски", "now speaking English"))
            return 0
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
        ui.say(name, ui.green(t(f"теперь меня можно звать и «{args.alias}»: {msg}", f"now you can also call me “{args.alias}”: {msg}"))
               if ok else ui.red(msg))
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
            ui.panel(t("Папки-источники", "Source folders"), ([docs.short(d) for d in cfg.docs] or [ui.dim(t("нет", "none"))]) +
                     ["", ui.dim(t(f"в индексе {files} файлов, {chunks} фрагментов", f"{files} files, {chunks} chunks in the index"))],
                     "source", "§")
            return 0
        if piped:  # `some-cmd 2>&1 | shellmate explain`, `cat notes.txt | shellmate translate to English`
            if query and router.classify(cfg.ollama_url, cfg.model, query).intent == "translate":
                return app.translate(router.Intent("translate"), query, piped)
            return app.explain_text(piped, query)
        if not query:
            if sys.stdin.isatty():
                return repl(app, args.debug)
            app.say(app.phrase("hello"))
            return 0
        return app.handle(query, args.debug)
    except llm.LLMError as e:
        ui.problem(str(e), title=t("Модель недоступна", "Model unavailable"))
        return 2
    except KeyboardInterrupt:
        print()
        return 130
    finally:
        if sys.stdout.isatty() and not args.update:
            tag = updates.newer_available(name)
            if tag:
                print(ui.dim(t(f"\n   доступна новая версия {tag} (у тебя {updates.version()}) — {name} --update",
                               f"\n   a new version {tag} is out (you have {updates.version()}) — {name} --update")))
