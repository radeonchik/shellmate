"""Intent handlers for system state: health, services, processes, packages info, hardware, network.
Mixed into cli.App, so `self` has say/phrase/execute/ask_model/cfg."""

import os
import re
import shutil
from pathlib import Path

from shellmate import disk, errors, packages, system, ui
from shellmate.packages import run_quiet
from shellmate.prompts import explain_task, lang_of, log_task

SERVICE_ACTIONS = [("restart", "перезапустить"), ("start", "запустить"), ("stop", "остановить"),
                   ("enable", "включить автозапуск"), ("disable", "выключить автозапуск"),
                   ("explain", "объяснить статус")]


class SystemSkills:
    # --- health ---------------------------------------------------------------

    def health(self, it, query: str) -> int:
        with ui.working("смотрю сервисы и журнал с момента загрузки…"):
            failed, errs = system.failed_units(), system.boot_errors()
        if not failed and not errs:
            self.say(ui.green(self.phrase("healthy")))
            return 0
        ui.kv("Состояние системы", [
            ("Упавшие сервисы", ui.red(str(len(failed))) if failed else ui.green("нет")),
            ("Ошибки в журнале", f"{ui.yellow(str(len(errs)))} разных с момента загрузки"),
        ], "err" if failed else "warn", "⚑")
        items = [(u, f"{ui.red('✗ упал')} {ui.bold(u.name)} {u.desc}") for u in failed]
        items += [(e, f"{ui.yellow(('×' + str(e.count)).rjust(4))} {ui.bold(e.source)}: {e.message[:110]}")
                  for e in errs]
        while True:
            i = ui.choose([line for _, line in items], "подробнее >")
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
                    self.ask_model(log_task(lang_of(query) if query else "Russian"), text,
                                   title="Что это значит")

    # --- services -------------------------------------------------------------

    def service(self, it, query: str) -> int:
        with ui.working(f"ищу сервис «{it.text or query}»…"):
            units = system.find_units(it.text or query)
        if not units:
            self.say(ui.yellow(self.phrase("not_found", q=it.text or query)))
            return 1
        if len(units) == 1:
            unit = units[0]
        else:
            lines = [f"{ui.bold(u.name)} {ui.dim(u.state)}{ui.dim(' · user') if u.user else ''}  {u.desc}"
                     for u in units]
            i = ui.choose(lines, "сервис >")
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
                 [ui.dim(line) for line in out.strip().splitlines()] or [ui.dim("(нет статуса)")], color, "⚙")
        for what, fix in errors.match(out, self.cfg.name)[:1]:
            ui.problem(what, fix)
        i = ui.choose([label for _, label in SERVICE_ACTIONS], f"{unit.name} >")
        if i is None:
            return 0
        act = SERVICE_ACTIONS[i][0]
        if act == "explain":
            self.ask_model(explain_task("Russian"), out[-3000:], title="Объяснение")
            return 0
        return self._service_do(unit, act)

    def _service_do(self, unit: system.Unit, action: str) -> int:
        inverse = {"enable": "disable", "disable": "enable", "start": "stop", "stop": "start"}
        labels = {"enable": "автозапуск вкл", "disable": "автозапуск выкл", "start": "запуск",
                  "stop": "остановка", "restart": "перезапуск"}
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
        with ui.working("замеряю нагрузку…"):
            groups = system.processes()
        if it.text:
            groups = [g for g in groups if it.text.lower() in g.name.lower()]
            if not groups:
                self.say(ui.yellow(self.phrase("not_found", q=it.text)))
                return 1
        else:
            mem = system.meminfo()
            cpu_total = sum(g.cpu for g in groups) / (os.cpu_count() or 1)
            used = mem["MemTotal"] - mem["MemAvailable"]
            ui.kv("Нагрузка", [
                ("Процессор", f"{ui.bar(round(cpu_total), 16)} {ui.bold(f'{cpu_total:.0f}%')}"),
                ("Память", f"{ui.bar(round(100 * used / mem['MemTotal']), 16)} "
                           f"{ui.bold(disk.human(used))} из {disk.human(mem['MemTotal'])}"),
            ], "accent", "⚡")
        top = sorted(groups, key=lambda g: -g.cpu)[:8]
        top += [g for g in sorted(groups, key=lambda g: -g.rss)[:8] if g not in top]
        if it.action == "kill" and len(top) == 1:
            return self.execute([system.kill_command(top[0])], f"завершение: {top[0].name}")
        lines = [f"{g.cpu:>5.0f}% CPU {disk.human(g.rss):>9}  {ui.bold(g.name)}"
                 f"{ui.dim(f' ×{len(g.pids)}') if len(g.pids) > 1 else ''}" for g in top]
        i = ui.choose(lines, "завершить >")  # Esc = just look
        if i is None:
            return 0
        return self.execute([system.kill_command(top[i])], f"завершение: {top[i].name}")

    # --- package info ---------------------------------------------------------

    def pkginfo(self, it, query: str) -> int:
        target = it.text or (it.packages[0] if it.packages else "")
        if not target:
            self.say(self.phrase("unclear", what="найти"))
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
            ui.panel(path, ["не принадлежит ни одному пакету — его поставили вручную "
                            "(pip, скрипт, make install) или создала программа"], "warn", "?")
            return 0
        what = f"команда {ui.bold(command)} {ui.dim(f'({path})')}" if command else ui.bold(path)
        self.say(f"{what} — из пакета {ui.bold(owner[0])}")
        self._show_pkg(owner[0])
        return 0

    def _show_pkg(self, name: str) -> None:
        ui.kv(name, packages.backend().info(name), "accent", "◆")

    def _provides(self, command: str) -> int:
        be = packages.backend()
        with ui.working(f"ищу, в каком пакете команда {command}…"):
            cands = be.provides(command)
        if cands is None:
            if not be.provides_setup:
                self.say(ui.yellow(self.phrase("not_found", q=command)))
                return 1
            self.say(f"команды {command} нет. Чтобы найти, в каком пакете она есть, {be.provides_hint}:")
            return self.execute([be.provides_setup], "база файлов для поиска команд")
        if not cands:
            self.say(ui.yellow(self.phrase("not_found", q=command)))
            return 1
        self.say(f"команды {ui.bold(command)} нет, но она есть в пакетах:")
        i = ui.choose([f"{ui.bold(p.name)} {ui.dim(p.source + ' ' + p.version)}" for p in cands], "установить >")
        return 0 if i is None else self._install([cands[i]])

    # --- sysinfo / network ----------------------------------------------------

    def sysinfo(self, it, query: str) -> int:
        with ui.working("собираю сведения о системе…"):
            rows = system.sysinfo()
            mounts = disk.mounts()[:1]
        for m in mounts:
            rows.insert(4, ("Диск", f"{ui.bar(m.pct, 16)} {m.pct}% · свободно {disk.human(m.avail)} "
                                    f"из {disk.human(m.size)}"))
        ui.kv("Система", rows, "accent", "⚙")
        return 0

    def network(self, it, query: str) -> int:
        port = re.fullmatch(r"\D*(\d{2,5})\D*", it.text or "")
        if port:
            users = system.port_users(int(port.group(1)))
            if users:
                ui.panel(f"Порт {port.group(1)}", users, "accent", "⇅")
            else:
                self.say(f"порт {port.group(1)} никто не слушает — он свободен.")
            return 0
        public = "public" in (it.text or "").lower() or re.search(r"внешн|публичн|public", query, re.I)
        with ui.working("проверяю сеть…"):
            addrs, gw, wifi = system.addresses(), system.gateway(), system.wifi()
            dns, tcp, ms = system.check_internet()
            ext = system.public_ip() if public else ""
        rows = [("Адреса", ", ".join(f"{ip} ({dev})" for dev, ip in addrs) or ui.red("нет"))]
        if wifi:
            rows.append(("Wi-Fi", wifi))
        rows.append(("Роутер", gw or ui.red("нет")))
        rows.append(("Интернет", ui.green(f"есть, {ms:.0f} мс") if tcp else ui.red("нет")))
        rows.append(("DNS", ui.green("работает") if dns else ui.red("не работает")))
        if public:
            rows.append(("Внешний IP", ext or ui.red("не удалось узнать")))
        ui.kv("Сеть", rows, "ok" if tcp and dns else "err", "⇅")
        if not addrs:
            ui.problem("Нет подключения к сети.", "проверь Wi-Fi или кабель; список сетей: nmcli dev wifi")
        elif not tcp:
            ui.problem("Локальная сеть есть, а интернета нет — проблема у роутера или провайдера.",
                       "попробуй перезагрузить роутер")
        elif not dns:
            ui.problem("Интернет есть, но не работает DNS (имена сайтов не находятся).",
                       "перезапустить systemd-resolved")
            units = system.find_units("systemd-resolved")
            if units:
                return self._service_do(units[0], "restart")
        return 0
