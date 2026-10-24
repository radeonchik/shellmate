"""Intent handlers for tools: explaining a command before it runs (more follow).
Mixed into cli.App, so `self` has say/phrase/execute/cfg/_translate."""

import shutil
import sys
import urllib.parse

from shellmate import cmdcheck, library, manpages, ui, web
from shellmate.i18n import t
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
