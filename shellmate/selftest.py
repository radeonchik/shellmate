"""`shellmate --selftest`: read-only checks of the distro backend against the live system.
CI runs it in real Arch / Debian / Ubuntu / Fedora containers; users can run it to see what works."""

import traceback
from datetime import datetime

from shellmate import backends, packages, ui


def run() -> int:
    d = backends.distro()
    be = backends.current()
    print(f"дистрибутив: {d.name} · семейство {d.family} · менеджер пакетов {be.tool or '—'}")
    failures = 0

    def check(name: str, fn, ok) -> None:
        nonlocal failures
        try:
            value = fn()
            passed = ok(value)
        except Exception:  # a crash is a failure with its traceback, not an abort
            value, passed = traceback.format_exc(limit=3), False
        failures += not passed
        mark = ui.green("✓") if passed else ui.red("✗")
        shown = repr(value)
        print(f"  {mark} {name}: {shown[:160] + ('…' if len(shown) > 160 else '')}")

    check("семейство определено", lambda: d.family, lambda v: v in ("arch", "debian", "fedora"))
    check("bash установлен", lambda: be.installed_version("bash"), bool)
    check("установленные пакеты", lambda: len(be.installed_names()), lambda v: v > 10)
    check("поиск установленного «bash»", lambda: [p.name for p in be.installed("bash")], lambda v: "bash" in v)
    check("поиск в репозиториях «curl»", lambda: sorted(p.name for p in be.search("curl")), lambda v: "curl" in v)
    check("владелец /usr/bin/env", lambda: be.owner("/usr/bin/env"),
          lambda v: bool(v) and v[0].startswith(("coreutils", "uutils")))
    check("сведения о bash", lambda: [k for k, _ in be.info("bash")], lambda v: "Версия" in v)
    check("сравнение версий", lambda: (be.vercmp("1.2-1", "1.10-1"), be.vercmp("2.0-1", "2.0-1")),
          lambda v: v == (-1, 0))
    check("версии для отката bash", lambda: [v for v, _ in be.versions("bash")][:3], lambda v: isinstance(v, list))
    check("в каком пакете ls", lambda: be.provides("ls"), lambda v: v is None or isinstance(v, list))
    check("чистка (только оценка)", lambda: [c.title[:40] for c in be.cleanup()], lambda v: isinstance(v, list))
    check("последнее обновление", lambda: be.last_upgrade(), lambda v: v is None or isinstance(v, datetime))
    check("команда установки", lambda: packages.install_commands([packages.Pkg("repo", "htop", "")]),
          lambda v: bool(v) and "htop" in v[0])
    check("команда удаления", lambda: packages.remove_commands([packages.Pkg("local", "htop", "")]),
          lambda v: bool(v) and "htop" in v[0])
    check("команды обновления", lambda: packages.update_commands(False), lambda v: bool(v))
    print(ui.green("всё работает") if not failures else ui.red(f"не прошло проверок: {failures}"))
    return 1 if failures else 0
