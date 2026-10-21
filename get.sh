#!/bin/sh
# shellmate in one command (Arch/CachyOS, Debian, Ubuntu, Fedora):
#   curl -fsSL https://raw.githubusercontent.com/radeonchik/shellmate/main/get.sh | sh
# with your own name for the assistant:
#   curl -fsSL https://raw.githubusercontent.com/radeonchik/shellmate/main/get.sh | sh -s -- friday
#
# Installs uv (user-level), then shellmate with `uv tool install`. Anything that needs sudo
# (git, fzf, Ollama) is asked first. SHELLMATE_SOURCE overrides where shellmate comes from.
# Everything is inside main(): sh reads the whole script before running it, so commands that
# read stdin can't swallow the rest of a `curl | sh` pipe.
set -eu

REPO="radeonchik/shellmate"
SOURCE="${SHELLMATE_SOURCE:-git+https://github.com/$REPO}"
BIN="$HOME/.local/bin"

if [ -t 1 ]; then C='\033[1;36m' Y='\033[1;33m' R='\033[1;31m' Z='\033[0m'; else C='' Y='' R='' Z=''; fi
say() { printf '%b◆%b %s\n' "$C" "$Z" "$*"; }
warn() { printf '%b!%b %s\n' "$Y" "$Z" "$*" >&2; }
die() { printf '%b✗%b %s\n' "$R" "$Z" "$*" >&2; exit 1; }
has() { command -v "$1" >/dev/null 2>&1; }

# Questions go to the terminal even when the script itself comes from a pipe.
# Without a terminal (CI, ssh without -t) nothing is installed with sudo: only hints are shown.
TTY=""
if (exec </dev/tty) 2>/dev/null; then TTY=/dev/tty; fi
ask() {  # ask "question" Y|N
    [ -n "$TTY" ] || return 1
    if [ "$2" = Y ]; then hint="[Y/n]"; else hint="[y/N]"; fi
    printf '%b?%b %s %s ' "$C" "$Z" "$1" "$hint" >"$TTY"
    read -r reply <"$TTY" || reply=""
    case "$reply" in
        y*|Y*|д*|Д*) return 0 ;;
        n*|N*|н*|Н*) return 1 ;;
        *) [ "$2" = Y ] ;;
    esac
}
on_tty() { if [ -n "$TTY" ]; then "$@" <"$TTY"; else "$@"; fi; }

pick_pm() {
    if has pacman; then PM=pacman
    elif has apt-get; then PM=apt
    elif has dnf; then PM=dnf
    else PM=""; fi
}

pm_install() {  # pm_install pkg...
    case "$PM" in
        pacman) on_tty sudo pacman -S --needed --noconfirm "$@" ;;
        apt) on_tty sudo apt-get update -qq && on_tty sudo apt-get install -y "$@" ;;
        dnf) on_tty sudo dnf install -y "$@" ;;
    esac
}

# Arch ships Ollama per GPU vendor; elsewhere the official script picks the build itself
arch_ollama_pkg() {
    if has nvidia-smi || [ -e /proc/driver/nvidia/version ]; then echo ollama-cuda
    elif has lspci && lspci 2>/dev/null | grep -Ei 'vga|3d|display' | grep -qi 'amd|ati'; then echo ollama-rocm
    else echo ollama; fi
}

main() {
    NAME="${1:-}"
    [ "$(uname -s)" = Linux ] || die "shellmate работает только на Linux."
    [ "$(id -u)" -ne 0 ] || die "Запусти от обычного пользователя, не от root: sudo спрошу сам, когда понадобится."
    if [ -n "$NAME" ] && ! printf '%s' "$NAME" | grep -Eq '^[a-z][a-z0-9_-]*$'; then
        die "Имя должно быть латиницей в нижнем регистре (a-z, 0-9, -, _): $NAME"
    fi
    pick_pm
    [ -n "$PM" ] || die "Не нашёл pacman, apt или dnf — этот дистрибутив пока не поддерживается."
    has curl || die "Нужен curl."

    say "Ставлю shellmate (менеджер пакетов: $PM)"

    # 1. system packages: git (uv fetches shellmate with it), fzf (selection menus)
    need=""
    has git || need="git"
    has fzf || need="$need fzf"
    need="${need# }"
    if [ -n "$need" ]; then
        if ask "Поставить через sudo: $need?" Y; then
            # shellcheck disable=SC2086  # word splitting of the package list is intended
            pm_install $need || warn "не получилось поставить $need — поставь вручную"
        fi
    fi
    if ! has git && [ "${SOURCE#git+}" != "$SOURCE" ]; then
        die "Без git не скачать shellmate. Поставь git и запусти скрипт ещё раз."
    fi

    # 2. Ollama runs the models
    if has ollama; then
        say "Ollama уже есть"
    elif [ "$PM" = pacman ]; then
        pkg=$(arch_ollama_pkg)
        if ask "Поставить Ollama ($pkg, нужен sudo)?" Y; then
            if pm_install "$pkg"; then
                on_tty sudo systemctl enable --now ollama || warn "запусти службу: sudo systemctl enable --now ollama"
            else
                warn "не получилось — поставь вручную: sudo pacman -S $pkg"
            fi
        fi
    elif ask "Поставить Ollama официальным скриптом с ollama.com (нужен sudo, ~1–2 ГБ)?" Y; then
        curl -fsSL https://ollama.com/install.sh | sh || warn "не получилось — см. https://ollama.com/download/linux"
    fi
    has ollama || warn "Без Ollama shellmate не заработает: https://ollama.com/download/linux"

    # 3. uv: installs shellmate and, if the system one is older than 3.10, its own Python
    export PATH="$BIN:$PATH"
    if ! has uv; then
        say "Ставлю uv (в ~/.local/bin, без sudo)"
        curl -LsSf https://astral.sh/uv/install.sh | sh
        has uv || die "uv не установился — https://docs.astral.sh/uv/getting-started/installation/"
    fi

    # 4. shellmate itself (--force: running the script again reinstalls cleanly)
    say "Скачиваю shellmate"
    uv tool install --force "$SOURCE"
    if [ -n "$NAME" ] && [ "$NAME" != shellmate ]; then
        "$BIN/shellmate" --alias "$NAME" || warn "имя «$NAME» не получилось: shellmate --alias ИМЯ"
    fi
    CMD="${NAME:-shellmate}"

    # 5. PATH for new terminals
    new_terminal=""
    case ":$ORIG_PATH:" in
        *":$BIN:"*) ;;
        *) uv tool update-shell >/dev/null 2>&1 || warn "добавь ~/.local/bin в PATH"
           new_terminal=1 ;;
    esac

    echo
    say "Готово: $("$BIN/shellmate" --version)"
    if [ -n "$TTY" ] && ask "Познакомимся сейчас (стиль, цвет, подбор и загрузка модели)?" Y; then
        on_tty "$BIN/shellmate" --setup || warn "знакомство не завершилось — запусти $CMD ещё раз"
    fi
    echo
    [ -n "$new_terminal" ] && echo "  Открой новый терминал (добавил ~/.local/bin в PATH), потом:"
    echo "  $CMD установи обс студио    — пример запроса"
    echo "  $CMD                        — диалог"
    echo "  $CMD --selftest             — проверка совместимости с системой"
    echo "  $CMD --update               — обновление"
}

ORIG_PATH="$PATH"
main "$@"
