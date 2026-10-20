"""Known errors -> human explanation + fix. Checked BEFORE asking the model:
instant, exact and never hallucinates. Add new patterns here as you meet them.

{0} in a text is replaced with the first regex group (e.g. the package or key name)."""

import re
from dataclasses import dataclass


@dataclass
class Known:
    pattern: str
    what: str
    fix: str


KNOWN = [
    Known(r"target not found: (\S+)|не найдена цель: (\S+)",
          "Пакета «{0}» нет в репозиториях под таким именем.",
          "Поищи похожие: {agent} search {0}. Возможно, он в AUR или называется иначе."),
    Known(r"(?:could not|unable to) lock database|не удалось заблокировать базу",
          "База pacman заблокирована: либо идёт другая установка, либо осталась блокировка после сбоя.",
          "Проверь: pgrep -a pacman. Если пусто — sudo rm /var/lib/pacman/db.lck"),
    Known(r"'?(/\S+?)'? (?:exists in filesystem|существует в файловой системе)",
          "Файл {0} уже существует и не принадлежит устанавливаемому пакету (конфликт файлов).",
          "Узнай владельца: pacman -Qo {0}. Если владельца нет — файл остался от ручной установки, "
          "его можно переместить. --overwrite используй только если понимаешь последствия."),
    Known(r"signature from .* is (?:unknown trust|marginal trust|invalid)|invalid or corrupted package \(PGP signature\)"
          r"|подпись от .* (?:неизвестна|имеет маргинальное доверие|некорректна)|недействительная PGP-подпись",
          "Подпись пакета не прошла проверку — обычно устарела связка ключей.",
          "sudo pacman -Sy archlinux-keyring cachyos-keyring && sudo pacman -Su"),
    Known(r"(?:unknown public key|неизвестный открытый ключ) ([0-9A-F]+)",
          "makepkg не может проверить исходники AUR-пакета: нет публичного ключа {0}.",
          "gpg --recv-keys {0}   (ключ указан в PKGBUILD/комментариях на странице AUR)"),
    Known(r"failed retrieving file|failed to retrieve some files|error 404|не удалось получить (?:файл|некоторые файлы)",
          "Не удалось скачать пакет: зеркало устарело или база пакетов неактуальна.",
          "sudo pacman -Syu  (обновить базы). Если не помогло — обнови зеркала: sudo cachyos-rate-mirrors"),
    Known(r"unable to satisfy dependency|could not satisfy dependencies|breaks dependency"
          r"|не удается удовлетворить зависимость|не удалось удовлетворить зависимости|нарушает зависимость",
          "Конфликт зависимостей — чаще всего система обновлена частично.",
          "Сделай полное обновление: {agent} update. Частичные обновления (-Sy без -u) на Arch не поддерживаются."),
    Known(r"unresolvable package conflicts detected|are in conflict|неразрешимый конфликт пакетов",
          "Два пакета не могут стоять одновременно (например, обычная и -git версия).",
          "Согласись удалить конфликтующий пакет, если он тебе не нужен, или выбери другой вариант пакета."),
    Known(r"not enough free disk space|No space left on device|недостаточно места на диске",
          "Закончилось место на диске.",
          "Посмотри: df -h. Очистить кэш пакетов: paru -Sc (или sudo pacman -Sc)."),
    Known(r"(?:A failure occurred in|Произошел сбой в) (?:build|package|prepare|check)\(\)",
          "Сборка AUR-пакета упала.",
          "Почитай свежие комментарии на странице пакета в AUR. Часто помогает вариант -bin вместо сборки из исходников."),
    Known(r"you cannot perform this operation unless you are root|требуются права root",
          "Команде нужны права администратора.",
          "Запусти её через sudo."),
    Known(r"Could not resolve host|Temporary failure in name resolution|Network is unreachable",
          "Нет сети или не работает DNS.",
          "Проверь интернет: ping -c3 archlinux.org"),
    Known(r"Trashing on system internal mounts is not supported|Перемещение в корзину на внутренних "
          r"смонтированных ресурсах системы не поддерживается|Unable to find or create trash directory",
          "На этом разделе нет корзины (например, /tmp или системные разделы), поэтому в корзину файл не убрать.",
          "Если файл точно не нужен, его можно удалить насовсем (без возможности отмены): rm ИМЯ_ФАЙЛА"),
    # --- apt / dpkg (Debian, Ubuntu) — shellmate runs apt in the C locale, so messages are English
    Known(r"E: Unable to locate package (\S+)",
          "Пакета «{0}» нет в подключённых репозиториях apt (или списки пакетов устарели).",
          "Обнови списки: sudo apt update. Потом поищи похожие: {agent} search {0} — возможно, он есть "
          "во Flatpak/Snap или называется иначе."),
    Known(r"Could not get lock (/var/lib/dpkg/lock\S*|/var/lib/apt/lists/lock)|Unable to acquire the dpkg frontend lock",
          "apt занят другой программой (обычно это автообновление или открытый центр приложений).",
          "Подожди пару минут и повтори. Проверить, кто держит: ps aux | grep -E 'apt|dpkg'"),
    Known(r"dpkg was interrupted, you must manually run '(sudo dpkg --configure -a)'",
          "Прошлая установка прервалась, и dpkg ждёт, пока её доделают.",
          "Выполни: sudo dpkg --configure -a — потом повтори команду."),
    Known(r"The following packages have unmet dependencies|held broken packages|Unmet dependencies",
          "Конфликт зависимостей apt: часть пакетов не может обновиться или установиться вместе.",
          "Попробуй: sudo apt update && sudo apt --fix-broken install. Если добавлены PPA — проблема часто в них."),
    Known(r"NO_PUBKEY ([0-9A-F]+)",
          "У репозитория нет доверенного ключа подписи ({0}) — apt ему не доверяет.",
          "Ключ ставится по инструкции самого репозитория (файл .gpg в /etc/apt/keyrings). Ключ, найденный "
          "где попало, не добавляй."),
    Known(r"Failed to fetch \S+|Temporary failure resolving '([^']+)'",
          "apt не смог скачать файлы: нет сети, сломан DNS или зеркало недоступно.",
          "Проверь интернет ({agent} есть ли интернет) и повтори sudo apt update."),
    Known(r"Sub-process /usr/bin/dpkg returned an error code \((\d+)\)",
          "dpkg упал при установке пакета (код {0}) — обычно из-за скрипта установки или конфликта файлов.",
          "Выполни sudo dpkg --configure -a, затем sudo apt --fix-broken install; выше в выводе — имя пакета."),
    # --- dnf / rpm (Fedora)
    Known(r"No match for argument: (\S+)|Unable to find a match: (\S+)|No package (\S+) available",
          "Пакета «{0}» нет в подключённых репозиториях dnf.",
          "Поищи похожие: {agent} search {0}. Многие программы (кодеки, Steam, драйверы NVIDIA) лежат в "
          "RPM Fusion — его нужно подключить отдельно."),
    Known(r"Waiting for process with pid (\d+) to finish|Another app is currently holding the (?:dnf|yum) lock",
          "dnf уже занят другим процессом (pid {0}) — часто это фоновое обновление.",
          "Подожди, пока он закончит, и повтори."),
    Known(r"GPG check FAILED|Public key for \S+ is not installed|The GPG keys listed for the \S+ repository",
          "Не прошла проверка подписи пакета.",
          "Обнови ключи и метаданные: sudo dnf upgrade --refresh fedora-gpg-keys. Ключи сторонних "
          "репозиториев ставь только по их официальной инструкции."),
    Known(r"Problem: (?:conflicting requests|package \S+ requires|cannot install both)|nothing provides (\S+)",
          "Конфликт зависимостей dnf.",
          "Часто помогает полное обновление: {agent} update. Если мешает сторонний репозиторий — dnf подскажет "
          "--allowerasing, но сначала посмотри, что он предлагает удалить."),
    Known(r"Failed to download metadata for repo '([^']+)'|Curl error \(\d+\)",
          "dnf не смог скачать данные репозитория {0}: нет сети или зеркало недоступно.",
          "Проверь интернет и повтори: sudo dnf upgrade --refresh"),
    # --- snap
    Known(r"error: This revision of snap \"([^\"]+)\" was published using classic confinement",
          "Snap «{0}» требует classic-режима (полный доступ к системе, как обычная программа).",
          "Если доверяешь издателю: sudo snap install {0} --classic"),
    Known(r"(\S+): command not found|Unknown command: (\S+)",
          "Команды {0} нет в системе.",
          "Узнай, в каком пакете она есть: sudo pacman -Fy && pacman -F {0}"),
]


def match(output: str, agent: str = "shellmate") -> list[tuple[str, str]]:
    """Returns (what, fix) for every known error found, most relevant (last in output) first."""
    hits = []
    for k in KNOWN:
        m = None
        for m in re.finditer(k.pattern, output, re.IGNORECASE):
            pass  # keep the last occurrence
        if m:
            arg = next((g for g in m.groups() if g), "")
            hits.append((m.start(), k.what.format(arg, agent=agent), k.fix.format(arg, agent=agent)))
    hits.sort(key=lambda h: -h[0])
    return [(w, f) for _, w, f in hits]
