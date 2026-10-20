#!/usr/bin/env bash
# Installs the assistant from this checkout under a name of your choice: ./install.sh friday
# (Without cloning: `uv tool install git+https://github.com/radeonchik/shellmate`, then `shellmate --alias friday`.)
set -euo pipefail

NAME="${1:-}"
if [[ -z "$NAME" ]]; then
    read -rp "Как назовём помощника? [shellmate] " NAME
    NAME="${NAME:-shellmate}"
fi
if [[ ! "$NAME" =~ ^[a-z][a-z0-9_-]*$ ]]; then
    echo "Имя должно быть латиницей в нижнем регистре (a-z, 0-9, -, _)" >&2
    exit 1
fi
if [[ "$NAME" != shellmate ]] && command -v "$NAME" >/dev/null && [[ "$(command -v "$NAME")" != "$HOME/.local/bin/$NAME" ]]; then
    echo "Команда «$NAME» уже существует в системе ($(command -v "$NAME")), выбери другое имя" >&2
    exit 1
fi

# package manager of this distro, for the hints below
if command -v pacman >/dev/null; then PM="sudo pacman -S"
elif command -v apt-get >/dev/null; then PM="sudo apt install"
elif command -v dnf >/dev/null; then PM="sudo dnf install"
else PM=""; fi

if ! command -v uv >/dev/null; then
    echo "Нужен uv (ставит shellmate и нужный Python): curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "или через пакеты: ${PM:-<менеджер пакетов>} uv   — потом запусти этот скрипт ещё раз."
    exit 1
fi

cd "$(dirname "$0")"
uv tool install --editable --force .
BIN="$HOME/.local/bin"
[[ "$NAME" != shellmate ]] && ln -sf "$BIN/shellmate" "$BIN/$NAME"

echo
echo "Готово: $NAME установи обс студио"
missing=()
command -v fzf >/dev/null || missing+=("fzf (красивые меню выбора)")
command -v gio >/dev/null || missing+=("gio (корзина; пакет glib2 / libglib2.0-bin)")
if ((${#missing[@]})); then
    echo "Желательно доставить: ${missing[*]}"
    [[ -n "$PM" ]] && echo "  $PM fzf"
fi
if ! command -v ollama >/dev/null; then
    if [[ "$PM" == "sudo pacman -S" ]]; then
        echo "Нужна Ollama: sudo pacman -S ollama-cuda (NVIDIA) / ollama-rocm (AMD) / ollama && sudo systemctl enable --now ollama"
    else
        echo "Нужна Ollama: curl -fsSL https://ollama.com/install.sh | sh"
    fi
fi
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Добавь ~/.local/bin в PATH (uv tool update-shell)";; esac
echo "Проверка совместимости с этой системой: $NAME --selftest"
