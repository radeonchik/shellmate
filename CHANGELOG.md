# Changelog

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
