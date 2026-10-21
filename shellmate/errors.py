"""Known errors -> human explanation + fix. Checked BEFORE asking the model:
instant, exact and never hallucinates. Add new patterns here as you meet them.

{0} in a text is replaced with the first regex group (e.g. the package or key name)."""

import re
from dataclasses import dataclass

from shellmate.i18n import en


@dataclass
class Known:
    pattern: str
    what: str
    fix: str
    what_en: str = ""
    fix_en: str = ""


KNOWN = [
    Known(r"target not found: (\S+)|не найдена цель: (\S+)",
          "Пакета «{0}» нет в репозиториях под таким именем.",
          "Поищи похожие: {agent} search {0}. Возможно, он в AUR или называется иначе.",
          "Package “{0}” isn't in the repositories under this name.",
          "Look for similar ones: {agent} search {0}. It may be in the AUR or have another name."),
    Known(r"(?:could not|unable to) lock database|не удалось заблокировать базу",
          "База pacman заблокирована: либо идёт другая установка, либо осталась блокировка после сбоя.",
          "Проверь: pgrep -a pacman. Если пусто — sudo rm /var/lib/pacman/db.lck",
          "The pacman database is locked: either another installation is running, or a lock was left after a crash.",
          "Check: pgrep -a pacman. If it prints nothing — sudo rm /var/lib/pacman/db.lck"),
    Known(r"'?(/\S+?)'? (?:exists in filesystem|существует в файловой системе)",
          "Файл {0} уже существует и не принадлежит устанавливаемому пакету (конфликт файлов).",
          "Узнай владельца: pacman -Qo {0}. Если владельца нет — файл остался от ручной установки, "
          "его можно переместить. --overwrite используй только если понимаешь последствия.",
          "The file {0} already exists and doesn't belong to the package being installed (file conflict).",
          "Find the owner: pacman -Qo {0}. If there's no owner, the file was left by a manual install and can be moved away. Use --overwrite only if you understand the consequences."),
    Known(r"signature from .* is (?:unknown trust|marginal trust|invalid)|invalid or corrupted package \(PGP signature\)"
          r"|подпись от .* (?:неизвестна|имеет маргинальное доверие|некорректна)|недействительная PGP-подпись",
          "Подпись пакета не прошла проверку — обычно устарела связка ключей.",
          "sudo pacman -Sy archlinux-keyring cachyos-keyring && sudo pacman -Su",
          "The package signature failed to verify — usually the keyring is outdated.",
          "sudo pacman -Sy archlinux-keyring cachyos-keyring && sudo pacman -Su"),
    Known(r"(?:unknown public key|неизвестный открытый ключ) ([0-9A-F]+)",
          "makepkg не может проверить исходники AUR-пакета: нет публичного ключа {0}.",
          "gpg --recv-keys {0}   (ключ указан в PKGBUILD/комментариях на странице AUR)",
          "makepkg can't verify the AUR package's sources: the public key {0} is missing.",
          "gpg --recv-keys {0}   (the key is named in the PKGBUILD / comments on the AUR page)"),
    Known(r"failed retrieving file|failed to retrieve some files|error 404|не удалось получить (?:файл|некоторые файлы)",
          "Не удалось скачать пакет: зеркало устарело или база пакетов неактуальна.",
          "sudo pacman -Syu  (обновить базы). Если не помогло — обнови зеркала: sudo cachyos-rate-mirrors",
          "Couldn't download a package: the mirror is outdated or the package database is stale.",
          "sudo pacman -Syu  (refresh the databases). If that doesn't help, refresh the mirrors: sudo cachyos-rate-mirrors"),
    Known(r"unable to satisfy dependency|could not satisfy dependencies|breaks dependency"
          r"|не удается удовлетворить зависимость|не удалось удовлетворить зависимости|нарушает зависимость",
          "Конфликт зависимостей — чаще всего система обновлена частично.",
          "Сделай полное обновление: {agent} update. Частичные обновления (-Sy без -u) на Arch не поддерживаются.",
          "Dependency conflict — most often the system is partially updated.",
          "Do a full update: {agent} update. Partial updates (-Sy without -u) are not supported on Arch."),
    Known(r"unresolvable package conflicts detected|are in conflict|неразрешимый конфликт пакетов",
          "Два пакета не могут стоять одновременно (например, обычная и -git версия).",
          "Согласись удалить конфликтующий пакет, если он тебе не нужен, или выбери другой вариант пакета.",
          "Two packages can't be installed at the same time (for example a regular and a -git version).",
          "Agree to remove the conflicting package if you don't need it, or pick another variant of the package."),
    Known(r"not enough free disk space|No space left on device|недостаточно места на диске",
          "Закончилось место на диске.",
          "Посмотри: df -h. Очистить кэш пакетов: paru -Sc (или sudo pacman -Sc).",
          "The disk is full.",
          "Look: df -h. Clean the package cache: paru -Sc (or sudo pacman -Sc)."),
    Known(r"(?:A failure occurred in|Произошел сбой в) (?:build|package|prepare|check)\(\)",
          "Сборка AUR-пакета упала.",
          "Почитай свежие комментарии на странице пакета в AUR. Часто помогает вариант -bin вместо сборки из исходников.",
          "The AUR package build failed.",
          "Read the latest comments on the package's AUR page. A -bin variant instead of building from source often helps."),
    Known(r"you cannot perform this operation unless you are root|требуются права root",
          "Команде нужны права администратора.",
          "Запусти её через sudo.",
          "The command needs administrator rights.",
          "Run it with sudo."),
    Known(r"Could not resolve host|Temporary failure in name resolution|Network is unreachable",
          "Нет сети или не работает DNS.",
          "Проверь интернет: ping -c3 archlinux.org",
          "No network, or DNS doesn't work.",
          "Check the internet: ping -c3 archlinux.org"),
    Known(r"Trashing on system internal mounts is not supported|Перемещение в корзину на внутренних "
          r"смонтированных ресурсах системы не поддерживается|Unable to find or create trash directory",
          "На этом разделе нет корзины (например, /tmp или системные разделы), поэтому в корзину файл не убрать.",
          "Если файл точно не нужен, его можно удалить насовсем (без возможности отмены): rm ИМЯ_ФАЙЛА",
          "This partition has no trash (for example /tmp or system partitions), so the file can't be moved to the trash.",
          "If you're sure the file isn't needed, it can be deleted for good (no undo): rm FILE_NAME"),
    # --- apt / dpkg (Debian, Ubuntu) — shellmate runs apt in the C locale, so messages are English
    Known(r"E: Unable to locate package (\S+)",
          "Пакета «{0}» нет в подключённых репозиториях apt (или списки пакетов устарели).",
          "Обнови списки: sudo apt update. Потом поищи похожие: {agent} search {0} — возможно, он есть "
          "во Flatpak/Snap или называется иначе.",
          "Package “{0}” isn't in the enabled apt repositories (or the package lists are outdated).",
          "Refresh the lists: sudo apt update. Then look for similar ones: {agent} search {0} — it may be in Flatpak/Snap or have another name."),
    Known(r"Could not get lock (/var/lib/dpkg/lock\S*|/var/lib/apt/lists/lock)|Unable to acquire the dpkg frontend lock",
          "apt занят другой программой (обычно это автообновление или открытый центр приложений).",
          "Подожди пару минут и повтори. Проверить, кто держит: ps aux | grep -E 'apt|dpkg'",
          "apt is busy with another program (usually automatic updates or an open software center).",
          "Wait a couple of minutes and try again. To see who holds it: ps aux | grep -E 'apt|dpkg'"),
    Known(r"dpkg was interrupted, you must manually run '(sudo dpkg --configure -a)'",
          "Прошлая установка прервалась, и dpkg ждёт, пока её доделают.",
          "Выполни: sudo dpkg --configure -a — потом повтори команду.",
          "A previous installation was interrupted, and dpkg is waiting for it to be finished.",
          "Run: sudo dpkg --configure -a — then repeat the command."),
    Known(r"The following packages have unmet dependencies|held broken packages|Unmet dependencies",
          "Конфликт зависимостей apt: часть пакетов не может обновиться или установиться вместе.",
          "Попробуй: sudo apt update && sudo apt --fix-broken install. Если добавлены PPA — проблема часто в них.",
          "apt dependency conflict: some packages can't be upgraded or installed together.",
          "Try: sudo apt update && sudo apt --fix-broken install. If PPAs are added, they are often the cause."),
    Known(r"NO_PUBKEY ([0-9A-F]+)",
          "У репозитория нет доверенного ключа подписи ({0}) — apt ему не доверяет.",
          "Ключ ставится по инструкции самого репозитория (файл .gpg в /etc/apt/keyrings). Ключ, найденный "
          "где попало, не добавляй.",
          "The repository has no trusted signing key ({0}), so apt doesn't trust it.",
          "Install the key following the repository's own instructions (a .gpg file in /etc/apt/keyrings). Don't add a key found just anywhere."),
    Known(r"Failed to fetch \S+|Temporary failure resolving '([^']+)'",
          "apt не смог скачать файлы: нет сети, сломан DNS или зеркало недоступно.",
          "Проверь интернет ({agent} есть ли интернет) и повтори sudo apt update.",
          "apt couldn't download files: no network, broken DNS or the mirror is unavailable.",
          "Check the internet ({agent} is the internet working) and repeat sudo apt update."),
    Known(r"Sub-process /usr/bin/dpkg returned an error code \((\d+)\)",
          "dpkg упал при установке пакета (код {0}) — обычно из-за скрипта установки или конфликта файлов.",
          "Выполни sudo dpkg --configure -a, затем sudo apt --fix-broken install; выше в выводе — имя пакета.",
          "dpkg failed while installing a package (code {0}) — usually because of an install script or a file conflict.",
          "Run sudo dpkg --configure -a, then sudo apt --fix-broken install; the package name is higher up in the output."),
    # --- dnf / rpm (Fedora)
    Known(r"No match for argument: (\S+)|Unable to find a match: (\S+)|No package (\S+) available",
          "Пакета «{0}» нет в подключённых репозиториях dnf.",
          "Поищи похожие: {agent} search {0}. Многие программы (кодеки, Steam, драйверы NVIDIA) лежат в "
          "RPM Fusion — его нужно подключить отдельно.",
          "Package “{0}” isn't in the enabled dnf repositories.",
          "Look for similar ones: {agent} search {0}. Many programs (codecs, Steam, NVIDIA drivers) live in RPM Fusion, which has to be enabled separately."),
    Known(r"Waiting for process with pid (\d+) to finish|Another app is currently holding the (?:dnf|yum) lock",
          "dnf уже занят другим процессом (pid {0}) — часто это фоновое обновление.",
          "Подожди, пока он закончит, и повтори.",
          "dnf is already busy with another process (pid {0}) — often a background update.",
          "Wait until it finishes and try again."),
    Known(r"GPG check FAILED|Public key for \S+ is not installed|The GPG keys listed for the \S+ repository",
          "Не прошла проверка подписи пакета.",
          "Обнови ключи и метаданные: sudo dnf upgrade --refresh fedora-gpg-keys. Ключи сторонних "
          "репозиториев ставь только по их официальной инструкции.",
          "The package signature check failed.",
          "Refresh the keys and metadata: sudo dnf upgrade --refresh fedora-gpg-keys. Install keys of third-party repositories only following their official instructions."),
    Known(r"Problem: (?:conflicting requests|package \S+ requires|cannot install both)|nothing provides (\S+)",
          "Конфликт зависимостей dnf.",
          "Часто помогает полное обновление: {agent} update. Если мешает сторонний репозиторий — dnf подскажет "
          "--allowerasing, но сначала посмотри, что он предлагает удалить.",
          "dnf dependency conflict.",
          "A full update often helps: {agent} update. If a third-party repository is in the way, dnf suggests --allowerasing, but first look at what it wants to remove."),
    Known(r"Failed to download metadata for repo '([^']+)'|Curl error \(\d+\)",
          "dnf не смог скачать данные репозитория {0}: нет сети или зеркало недоступно.",
          "Проверь интернет и повтори: sudo dnf upgrade --refresh",
          "dnf couldn't download data of the repository {0}: no network or the mirror is unavailable.",
          "Check the internet and try again: sudo dnf upgrade --refresh"),
    # --- snap
    Known(r"error: This revision of snap \"([^\"]+)\" was published using classic confinement",
          "Snap «{0}» требует classic-режима (полный доступ к системе, как обычная программа).",
          "Если доверяешь издателю: sudo snap install {0} --classic",
          "Snap “{0}” needs classic confinement (full access to the system, like a regular program).",
          "If you trust the publisher: sudo snap install {0} --classic"),
    Known(r"(\S+): command not found|Unknown command: (\S+)",
          "Команды {0} нет в системе.",
          "Узнай, в каком пакете она есть: {agent} в каком пакете {0}",
          "There's no command {0} on the system.",
          "Find which package has it: {agent} which package has {0}"),
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
            what, fix = (k.what_en, k.fix_en) if en() else (k.what, k.fix)
            hits.append((m.start(), what.format(arg, agent=agent), fix.format(arg, agent=agent)))
    hits.sort(key=lambda h: -h[0])
    return [(w, f) for _, w, f in hits]
