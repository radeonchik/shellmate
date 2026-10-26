"""Intent handlers for system state: health, services, processes, packages info, hardware, network.
Mixed into cli.App, so `self` has say/phrase/execute/ask_model/cfg."""

import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from shellmate import disk, errors, packages, slow, system, ui
from shellmate.i18n import t
from shellmate.packages import run_quiet
from shellmate.prompts import explain_task, lang_of, log_task, ui_language


def service_actions() -> list[tuple[str, str]]:
    return [("restart", t("перезапустить", "restart")), ("start", t("запустить", "start")),
            ("stop", t("остановить", "stop")), ("enable", t("включить автозапуск", "enable autostart")),
            ("disable", t("выключить автозапуск", "disable autostart")), ("explain", t("объяснить статус", "explain the status"))]


class SystemSkills:
    # --- health ---------------------------------------------------------------

    def health(self, it, query: str) -> int:
        # "the log of sshd": the router said health but named a service — that service's status shows its log
        if it.text and system.find_units(it.text):
            return self.service(type(it)("service", text=it.text, action="status"), query)
        with ui.working(t("смотрю сервисы и журнал с момента загрузки…", "checking services and the journal since boot…")):
            failed, errs = system.failed_units(), system.boot_errors()
        if not failed and not errs:
            self.say(ui.green(self.phrase("healthy")))
            return 0
        ui.kv(t("Состояние системы", "System state"), [
            (t("Упавшие сервисы", "Failed services"), ui.red(str(len(failed))) if failed else ui.green(t("нет", "none"))),
            (t("Ошибки в журнале", "Errors in the journal"), ui.yellow(str(len(errs))) + t(" разных с момента загрузки", " distinct since boot")),
        ], "err" if failed else "warn", "⚑")
        items = [(u, f"{ui.red(t('✗ упал', '✗ failed'))} {ui.bold(u.name)} {u.desc}") for u in failed]
        items += [(e, f"{ui.yellow(('×' + str(e.count)).rjust(4))} {ui.bold(e.source)}: {e.message[:110]}")
                  for e in errs]
        while True:
            i = ui.choose([line for _, line in items], t("подробнее >", "details >"))
            if i is None:
                return 0
            obj = items[i][0]
            if isinstance(obj, system.Unit):
                self.service_menu(obj)
            else:
                text = f"{obj.source}: {obj.message}"
                ui.panel(f"{obj.source} · ×{obj.count}", [obj.message], "warn", "⚑")
                hits = errors.match(text, self.cfg.name)
                for what, fix in hits[:1]:
                    ui.problem(what, fix)
                if not hits:
                    self.ask_model(log_task(lang_of(query) if query else ui_language()), text,
                                   title=t("Что это значит", "What it means"))

    # --- services -------------------------------------------------------------

    def service(self, it, query: str) -> int:
        with ui.working(t(f"ищу сервис «{it.text or query}»…", f"looking for the service “{it.text or query}”…")):
            units = system.find_units(it.text or query)
        if not units:
            self.say(ui.yellow(self.phrase("not_found", q=it.text or query)))
            return 1
        if len(units) == 1:
            unit = units[0]
        else:
            lines = [f"{ui.bold(u.name)} {ui.dim(u.state)}{ui.dim(' · user') if u.user else ''}  {u.desc}"
                     for u in units]
            i = ui.choose(lines, t("сервис >", "service >"))
            if i is None:
                return 0
            unit = units[i]
        return self.service_menu(unit, it.action)

    def service_menu(self, unit: system.Unit, action: str = "") -> int:
        if action and action != "status":
            return self._service_do(unit, action)
        out = unit.status()
        state = unit.state.split()[0] if unit.state else ""
        color = "err" if state == "failed" else "ok" if state == "active" else "source"
        ui.panel(f"{unit.name} · {unit.state}{' · user' if unit.user else ''}",
                 [ui.dim(line) for line in out.strip().splitlines()] or [ui.dim(t("(нет статуса)", "(no status)"))], color, "⚙")
        for what, fix in errors.match(out, self.cfg.name)[:1]:
            ui.problem(what, fix)
        actions = service_actions()
        i = ui.choose([label for _, label in actions], f"{unit.name} >")
        if i is None:
            return 0
        act = actions[i][0]
        if act == "explain":
            self.ask_model(explain_task(ui_language()), out[-3000:], title=t("Объяснение", "Explanation"))
            return 0
        return self._service_do(unit, act)

    def _service_do(self, unit: system.Unit, action: str) -> int:
        inverse = {"enable": "disable", "disable": "enable", "start": "stop", "stop": "start"}
        labels = {"enable": t("автозапуск вкл", "autostart on"), "disable": t("автозапуск выкл", "autostart off"),
                  "start": t("запуск", "start"), "stop": t("остановка", "stop"), "restart": t("перезапуск", "restart")}
        undo = {"kind": "service", "unit": unit.name, "user": unit.user, "action": inverse[action]} \
            if action in inverse else None
        rc = self.execute([unit.cmd(action)], f"{labels.get(action, action)}: {unit.name}", undo)
        if rc == 0 and not self.dry:
            scope = ["--user"] if unit.user else []
            state = run_quiet(["systemctl", *scope, "is-active", unit.name]).strip()
            self.say(f"{unit.name}: {ui.green(state) if state == 'active' else ui.yellow(state)}")
        return rc

    # --- processes ------------------------------------------------------------

    def processes(self, it, query: str) -> int:
        focus = it.text if it.text in ("heat", "battery") else ""
        if focus or (not it.text and it.action != "kill"):
            with ui.working(t("замеряю процессор, память, диск и температуру…", "measuring CPU, memory, disk and temperature…")):
                sample = slow.measure(focus)
            groups = sample.groups
            self._diagnosis(slow.diagnose(sample, focus), focus)
        else:
            with ui.working(t("замеряю нагрузку…", "measuring the load…")):
                groups = system.processes()
        if it.text and not focus:
            groups = [g for g in groups if it.text.lower() in g.name.lower()]
            if not groups:
                self.say(ui.yellow(self.phrase("not_found", q=it.text)))
                return 1
        else:
            mem = system.meminfo()
            cpu_total = sum(g.cpu for g in groups) / (os.cpu_count() or 1)
            used = mem["MemTotal"] - mem["MemAvailable"]
            ui.kv(t("Нагрузка", "Load"), [
                (t("Процессор", "CPU"), f"{ui.bar(round(cpu_total), 16)} {ui.bold(f'{cpu_total:.0f}%')}"),
                (t("Память", "Memory"), f"{ui.bar(round(100 * used / mem['MemTotal']), 16)} "
                                        f"{ui.bold(disk.human(used))} {t('из', 'of')} {disk.human(mem['MemTotal'])}"),
            ], "accent", "⚡")
        top = sorted(groups, key=lambda g: -g.cpu)[:8]
        top += [g for g in sorted(groups, key=lambda g: -g.rss)[:8] if g not in top]
        if it.action == "kill" and len(top) == 1:
            return self.execute([system.kill_command(top[0])], t("завершение: ", "kill: ") + top[0].name)
        lines = [f"{g.cpu:>5.0f}% CPU {disk.human(g.rss):>9}  {ui.bold(g.name)}"
                 f"{ui.dim(f' ×{len(g.pids)}') if len(g.pids) > 1 else ''}" for g in top]
        i = ui.choose(lines, t("завершить >", "kill >"))  # Esc = just look
        if i is None:
            return 0
        return self.execute([system.kill_command(top[i])], t("завершение: ", "kill: ") + top[i].name)

    def _diagnosis(self, findings: list, focus: str) -> None:
        marks = {"bad": ui.red("✗"), "warn": ui.yellow("!"), "info": ui.dim("·"), "ok": ui.green("✓")}
        lines = []
        for f in findings:
            lines.append(f"{marks[f.level]} {f.text}")
            if f.hint:
                lines.append(ui.dim(f"    {f.hint}"))
        title = {"heat": t("Почему греется", "Why it's hot"), "battery": t("Что тратит батарею", "What drains the battery")}
        ui.panel(title.get(focus, t("Почему тормозит", "Why it's slow")), lines, "accent", "⚡")
        for f in findings:
            if f.fix and sys.stdin.isatty() and self.ask_yes(t(f"Исправить: {shlex.join(f.fix)}?", f"Fix it: {shlex.join(f.fix)}?")):
                self.execute([f.fix], t("режим питания", "power mode"))

    # --- package info ---------------------------------------------------------

    def pkginfo(self, it, query: str) -> int:
        target = it.text or (it.packages[0] if it.packages else "")
        if not target:
            self.say(self.phrase("unclear", what=t("найти", "to find")))
            return 1
        if target.startswith(("/", "~")):
            return self._owner(str(Path(target).expanduser()))
        path = shutil.which(target)
        if path:
            return self._owner(path, command=target)
        if packages.installed_version(target):
            self._show_pkg(target)
            return 0
        return self._provides(target)

    def _owner(self, path: str, command: str = "") -> int:
        owner = packages.backend().owner(path)
        if not owner:
            ui.panel(path, [t("не принадлежит ни одному пакету — его поставили вручную "
                              "(pip, скрипт, make install) или создала программа",
                              "belongs to no package — it was installed by hand (pip, a script, "
                              "make install) or created by a program")], "warn", "?")
            return 0
        what = t("команда ", "the command ") + f"{ui.bold(command)} {ui.dim(f'({path})')}" if command else ui.bold(path)
        self.say(what + t(" — из пакета ", " comes from the package ") + ui.bold(owner[0]))
        self._show_pkg(owner[0])
        return 0

    def _show_pkg(self, name: str) -> None:
        ui.kv(name, packages.backend().info(name), "accent", "◆")

    def _provides(self, command: str) -> int:
        be = packages.backend()
        with ui.working(t(f"ищу, в каком пакете команда {command}…", f"looking for the package with the command {command}…")):
            cands = be.provides(command)
        if cands is None:
            if not be.provides_setup:
                self.say(ui.yellow(self.phrase("not_found", q=command)))
                return 1
            self.say(t(f"команды {command} нет. Чтобы найти, в каком пакете она есть, {be.provides_hint}:",
                       f"there's no command {command}. To find which package has it, {be.provides_hint}:"))
            return self.execute([be.provides_setup], t("база файлов для поиска команд", "file database for command lookup"))
        if not cands:
            self.say(ui.yellow(self.phrase("not_found", q=command)))
            return 1
        self.say(t(f"команды {ui.bold(command)} нет, но она есть в пакетах:", f"there's no command {ui.bold(command)}, but these packages have it:"))
        i = ui.choose([f"{ui.bold(p.name)} {ui.dim(p.source + ' ' + p.version)}" for p in cands], t("установить >", "install >"))
        return 0 if i is None else self._install([cands[i]])

    # --- sysinfo / network ----------------------------------------------------

    def sysinfo(self, it, query: str) -> int:
        with ui.working(t("собираю сведения о системе…", "collecting system information…")):
            rows = system.sysinfo()
            mounts = disk.mounts()[:1]
        for m in mounts:
            rows.insert(4, (t("Диск", "Disk"), f"{ui.bar(m.pct, 16)} {m.pct}% · " +
                            t(f"свободно {disk.human(m.avail)} из {disk.human(m.size)}",
                              f"free {disk.human(m.avail)} of {disk.human(m.size)}")))
        ui.kv(t("Система", "System"), rows, "accent", "⚙")
        return 0

    def network(self, it, query: str) -> int:
        port = re.fullmatch(r"\D*(\d{2,5})\D*", it.text or "")
        if port:
            users = system.port_users(int(port.group(1)))
            if users:
                ui.panel(t("Порт ", "Port ") + port.group(1), users, "accent", "⇅")
            else:
                self.say(t(f"порт {port.group(1)} никто не слушает — он свободен.", f"nothing listens on port {port.group(1)} — it's free."))
            return 0
        public = "public" in (it.text or "").lower() or re.search(r"внешн|публичн|public", query, re.I)
        with ui.working(t("проверяю сеть…", "checking the network…")):
            addrs, gw, wifi = system.addresses(), system.gateway(), system.wifi()
            dns, tcp, ms = system.check_internet()
            ext = system.public_ip() if public else ""
        no = ui.red(t("нет", "no"))
        rows = [(t("Адреса", "Addresses"), ", ".join(f"{ip} ({dev})" for dev, ip in addrs) or no)]
        if wifi:
            rows.append(("Wi-Fi", wifi))
        rows.append((t("Роутер", "Router"), gw or no))
        rows.append((t("Интернет", "Internet"), ui.green(t(f"есть, {ms:.0f} мс", f"yes, {ms:.0f} ms")) if tcp else no))
        rows.append(("DNS", ui.green(t("работает", "works")) if dns else ui.red(t("не работает", "doesn't work"))))
        if public:
            rows.append((t("Внешний IP", "Public IP"), ext or ui.red(t("не удалось узнать", "couldn't find out"))))
        ui.kv(t("Сеть", "Network"), rows, "ok" if tcp and dns else "err", "⇅")
        if not addrs:
            ui.problem(t("Нет подключения к сети.", "No network connection."),
                       t("проверь Wi-Fi или кабель; список сетей: nmcli dev wifi", "check Wi-Fi or the cable; list networks: nmcli dev wifi"))
        elif not tcp:
            ui.problem(t("Локальная сеть есть, а интернета нет — проблема у роутера или провайдера.",
                         "The local network works but the internet doesn't — a router or provider problem."),
                       t("попробуй перезагрузить роутер", "try restarting the router"))
        elif not dns:
            ui.problem(t("Интернет есть, но не работает DNS (имена сайтов не находятся).",
                         "The internet works but DNS doesn't (site names don't resolve)."),
                       t("перезапустить systemd-resolved", "restart systemd-resolved"))
            units = system.find_units("systemd-resolved")
            if units:
                return self._service_do(units[0], "restart")
        return 0
