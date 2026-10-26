"""`shellmate --selftest`: read-only checks of the distro backend against the live system.
CI runs it in real Arch / Debian / Ubuntu / Fedora containers; users can run it to see what works."""

import traceback
from datetime import datetime

from shellmate import backends, cmdcheck, convert, library, packages, slow, ui
from shellmate.backends.base import info_labels
from shellmate.i18n import t


def run() -> int:
    d = backends.distro()
    be = backends.current()
    print(t(f"дистрибутив: {d.name} · семейство {d.family} · менеджер пакетов {be.tool or '—'}",
            f"distribution: {d.name} · family {d.family} · package manager {be.tool or '—'}"))
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

    check(t("семейство определено", "family detected"), lambda: d.family, lambda v: v in ("arch", "debian", "fedora"))
    check(t("bash установлен", "bash installed"), lambda: be.installed_version("bash"), bool)
    check(t("установленные пакеты", "installed packages"), lambda: len(be.installed_names()), lambda v: v > 10)
    check(t("поиск установленного «bash»", "search installed “bash”"), lambda: [p.name for p in be.installed("bash")], lambda v: "bash" in v)
    check(t("поиск в репозиториях «curl»", "search the repos for “curl”"), lambda: sorted(p.name for p in be.search("curl")), lambda v: "curl" in v)
    check(t("владелец /usr/bin/env", "owner of /usr/bin/env"), lambda: be.owner("/usr/bin/env"),
          lambda v: bool(v) and v[0].startswith(("coreutils", "uutils")))
    check(t("сведения о bash", "bash info"), lambda: [k for k, _ in be.info("bash")], lambda v: info_labels()["version"] in v)
    check(t("сравнение версий", "version comparison"), lambda: (be.vercmp("1.2-1", "1.10-1"), be.vercmp("2.0-1", "2.0-1")),
          lambda v: v == (-1, 0))
    check(t("версии для отката bash", "bash versions to roll back to"), lambda: [v for v, _ in be.versions("bash")][:3], lambda v: isinstance(v, list))
    check(t("в каком пакете ls", "which package has ls"), lambda: be.provides("ls"), lambda v: v is None or isinstance(v, list))
    check(t("чистка (только оценка)", "cleanup (estimate only)"), lambda: [c.title[:40] for c in be.cleanup()], lambda v: isinstance(v, list))
    check(t("последнее обновление", "last update"), lambda: be.last_upgrade(), lambda v: v is None or isinstance(v, datetime))
    check(t("транзакции в журнале пакетов", "transactions in the package log"), lambda: len(be.transactions()),
          lambda v: isinstance(v, int))
    check(t("разбор команды без запуска", "parsing a command without running it"),
          lambda: [f.level for f in cmdcheck.risks(cmdcheck.parse("curl -fsSL https://x.io/i.sh | sudo bash"))],
          lambda v: v[:1] == ["danger"])
    check(t("справка без интернета (для этой системы)", "offline documentation (for this system)"),
          lambda: [s.id for s in library.wanted()], lambda v: "archwiki" in v and "tldr" in v)
    check(t("замеры «почему тормозит»", "measurements for “why is it slow”"),
          lambda: [f.level for f in slow.diagnose(slow.measure())], lambda v: bool(v))
    check(t("пакеты для конвертации", "packages for converting"),
          lambda: [convert.package_for(k, d.family) for k in (*convert.TOOLS, "heic")], lambda v: all(v))
    check(t("команда установки", "install command"), lambda: packages.install_commands([packages.Pkg("repo", "htop", "")]),
          lambda v: bool(v) and "htop" in v[0])
    check(t("команда удаления", "remove command"), lambda: packages.remove_commands([packages.Pkg("local", "htop", "")]),
          lambda v: bool(v) and "htop" in v[0])
    check(t("команды обновления", "update commands"), lambda: packages.update_commands(False), lambda v: bool(v))
    print(ui.green(t("всё работает", "everything works")) if not failures else
          ui.red(t(f"не прошло проверок: {failures}", f"checks failed: {failures}")))
    return 1 if failures else 0
