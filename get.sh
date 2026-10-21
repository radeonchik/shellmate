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

# messages in the desktop's language: Russian for a Russian locale, English otherwise
case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in ru*) L=ru ;; *) L=en ;; esac
m() { if [ "$L" = ru ]; then printf '%s' "$1"; else printf '%s' "$2"; fi; }

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

# Which Ollama build runs on this GPU: ROCm only covers RDNA2+ (gfx1030 and newer); older
# Radeons (Polaris like the Radeon Pro 555X, Vega...) work through Vulkan instead.
gpu_kind() {
    if has nvidia-smi || [ -e /proc/driver/nvidia/version ]; then echo cuda; return; fi
    [ -e /dev/kfd ] || { echo cpu; return; }
    for p in /sys/class/kfd/kfd/topology/nodes/*/properties; do
        v=$(sed -n 's/^gfx_target_version //p' "$p" 2>/dev/null)
        if [ "${v:-0}" -ge 100300 ]; then echo rocm; return; fi
    done
    echo vulkan
}

# Ollama's Vulkan backend is switched on per service
enable_vulkan() {
    on_tty sudo mkdir -p /etc/systemd/system/ollama.service.d &&
        printf '[Service]\nEnvironment=OLLAMA_VULKAN=1\n' |
        sudo tee /etc/systemd/system/ollama.service.d/vulkan.conf >/dev/null &&
        sudo systemctl daemon-reload && sudo systemctl restart ollama
}

main() {
    NAME="${1:-}"
    [ "$(uname -s)" = Linux ] || die "$(m "shellmate работает только на Linux." "shellmate only works on Linux.")"
    [ "$(id -u)" -ne 0 ] || die "$(m "Запусти от обычного пользователя, не от root: sudo спрошу сам, когда понадобится." "Run this as a regular user, not root: I'll ask for sudo when it's needed.")"
    if [ -n "$NAME" ] && ! printf '%s' "$NAME" | grep -Eq '^[a-z][a-z0-9_-]*$'; then
        die "$(m "Имя должно быть латиницей в нижнем регистре (a-z, 0-9, -, _): $NAME" "The name must be lowercase latin (a-z, 0-9, -, _): $NAME")"
    fi
    pick_pm
    [ -n "$PM" ] || die "$(m "Не нашёл pacman, apt или dnf — этот дистрибутив пока не поддерживается." "No pacman, apt or dnf found — this distribution isn't supported yet.")"
    has curl || die "$(m "Нужен curl." "curl is needed.")"

    say "$(m "Ставлю shellmate (менеджер пакетов: $PM)" "Installing shellmate (package manager: $PM)")"

    # 1. system packages: git (uv fetches shellmate with it), fzf (selection menus)
    need=""
    has git || need="git"
    has fzf || need="$need fzf"
    need="${need# }"
    if [ -n "$need" ]; then
        if ask "$(m "Поставить через sudo: $need?" "Install with sudo: $need?")" Y; then
            # shellcheck disable=SC2086  # word splitting of the package list is intended
            pm_install $need || warn "$(m "не получилось поставить $need — поставь вручную" "couldn't install $need — install it by hand")"
        fi
    fi
    if ! has git && [ "${SOURCE#git+}" != "$SOURCE" ]; then
        die "$(m "Без git не скачать shellmate. Поставь git и запусти скрипт ещё раз." "shellmate can't be downloaded without git. Install git and run the script again.")"
    fi

    # 2. Ollama runs the models
    gpu=$(gpu_kind)
    installed=""
    if has ollama; then
        say "$(m "Ollama уже есть" "Ollama is already installed")"
    elif [ "$PM" = pacman ]; then
        case "$gpu" in cuda | rocm | vulkan) pkg="ollama-$gpu" ;; *) pkg=ollama ;; esac
        if ask "$(m "Поставить Ollama ($pkg, нужен sudo)?" "Install Ollama ($pkg, needs sudo)?")" Y; then
            if pm_install "$pkg"; then
                installed=1
                on_tty sudo systemctl enable --now ollama || warn "$(m "запусти службу: sudo systemctl enable --now ollama" "start the service: sudo systemctl enable --now ollama")"
            else
                warn "$(m "не получилось — поставь вручную: sudo pacman -S $pkg" "that failed — install by hand: sudo pacman -S $pkg")"
            fi
        fi
    elif ask "$(m "Поставить Ollama официальным скриптом с ollama.com (нужен sudo, ~1–2 ГБ)?" "Install Ollama with the official script from ollama.com (needs sudo, ~1–2 GB)?")" Y; then
        if curl -fsSL https://ollama.com/install.sh | sh; then installed=1
        else warn "$(m "не получилось — см. https://ollama.com/download/linux" "that failed — see https://ollama.com/download/linux")"; fi
    fi
    if [ -n "$installed" ] && [ "$gpu" = vulkan ]; then
        say "$(m "Видеокарта AMD старше RDNA2: включаю в Ollama Vulkan (ROCm её не поддерживает)" "An AMD GPU older than RDNA2: enabling Vulkan in Ollama (ROCm doesn't support it)")"
        enable_vulkan || warn "$(m "не получилось включить Vulkan — модель будет работать на процессоре" "couldn't enable Vulkan — the model will run on the CPU")"
    fi
    has ollama || warn "$(m "Без Ollama shellmate не заработает: https://ollama.com/download/linux" "shellmate won't work without Ollama: https://ollama.com/download/linux")"

    # 3. uv: installs shellmate and, if the system one is older than 3.10, its own Python
    export PATH="$BIN:$PATH"
    if ! has uv; then
        say "$(m "Ставлю uv (в ~/.local/bin, без sudo)" "Installing uv (into ~/.local/bin, no sudo)")"
        curl -LsSf https://astral.sh/uv/install.sh | sh
        has uv || die "$(m "uv не установился — https://docs.astral.sh/uv/getting-started/installation/" "uv didn't install — https://docs.astral.sh/uv/getting-started/installation/")"
    fi

    # 4. shellmate itself (--force: running the script again reinstalls cleanly)
    say "$(m "Скачиваю shellmate" "Downloading shellmate")"
    uv tool install --force "$SOURCE"
    if [ -n "$NAME" ] && [ "$NAME" != shellmate ]; then
        "$BIN/shellmate" --alias "$NAME" || warn "$(m "имя «$NAME» не получилось: shellmate --alias ИМЯ" "the name '$NAME' didn't work: shellmate --alias NAME")"
    fi
    CMD="${NAME:-shellmate}"

    # 5. PATH for new terminals
    new_terminal=""
    case ":$ORIG_PATH:" in
        *":$BIN:"*) ;;
        *) uv tool update-shell >/dev/null 2>&1 || warn "$(m "добавь ~/.local/bin в PATH" "add ~/.local/bin to PATH")"
           new_terminal=1 ;;
    esac

    echo
    say "$(m "Готово" "Done"): $("$BIN/shellmate" --version)"
    if [ -n "$TTY" ] && ask "$(m "Познакомимся сейчас (язык, стиль, цвет, подбор и загрузка модели)?" "Set me up now (language, style, color, picking and downloading a model)?")" Y; then
        on_tty "$BIN/shellmate" --setup || warn "$(m "знакомство не завершилось — запусти $CMD ещё раз" "the setup didn't finish — run $CMD again")"
    fi
    echo
    if [ -n "$new_terminal" ]; then
        m "  Открой новый терминал (добавил ~/.local/bin в PATH), потом:" "  Open a new terminal (I added ~/.local/bin to PATH), then:"
        echo
    fi
    m "  $CMD установи обс студио    — пример запроса" "  $CMD install obs studio     — an example request"; echo
    m "  $CMD                        — диалог" "  $CMD                        — a conversation"; echo
    m "  $CMD --selftest             — проверка совместимости с системой" "  $CMD --selftest             — check compatibility with this system"; echo
    m "  $CMD --update               — обновление" "  $CMD --update               — update"; echo
}

ORIG_PATH="$PATH"
main "$@"
