# Changelog

## Unreleased

- English interface (and Russian, as before): chosen by the desktop locale, in the setup's first step
  or with `--lang en|ru`; every message is written as `t("по-русски", "in English")` in `i18n.py`.
  The error dictionary, communication styles, `--help`, `get.sh` and the README (`README.md` in
  English, `README.ru.md` in Russian) are translated too. Requests are understood in both languages.
- Fixed: the config saved fractional numbers (`escalate_intent = 0.5`) as strings, which broke the
  first request after the setup when the cascade was on. Such configs now load correctly.
- `get.sh`: install in one command (`curl -fsSL …/get.sh | sh`) — uv and shellmate without sudo,
  git/fzf/Ollama only after asking, then the onboarding. Checked in CI under dash.
- Older AMD cards (Polaris, Vega — e.g. Radeon Pro 555X/560X, RX 470–590) get Ollama's Vulkan build
  instead of ROCm, which doesn't support them (and is a 3 GB download on Arch).
- The second (9B) model is only offered when it fits in video memory.

## 0.2.0

- Debian, Ubuntu (+ Snap), Mint, Fedora and relatives, besides Arch: package backends for apt and dnf
  (search, install/remove, downgrade, "which package owns a file / provides a command", cleanup),
  distro-aware prompts, official docs per distro, apt/dnf/snap errors in the dictionary.
- `--selftest` and CI in Arch, Debian, Ubuntu 22.04/24.04 and Fedora containers; Python 3.10+.
- `--update`, `--version`, `--alias`, once-a-day hint about new releases.
- Two-model cascade: the fast model asks the big one when unsure (token probabilities + checks
  against the request); `-d` / «подумай хорошенько» for hard tasks.
- Quotes: section headings, filter for other programs of the same kind, per-quote check.
- Conversation mode, follow-ups, action log with undo, distro news before updating, fish hook for
  unknown commands, themes, search provider chain (Tavily, Brave, SearXNG, DuckDuckGo).

## 0.1.0

- First version: Arch Linux only.
