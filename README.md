# shellmate — a smart terminal

[Русский](README.ru.md) · **English**

Say what you need in your own words: shellmate works out what to do, shows the exact command and
runs it only after your “y”. The model runs locally, on your own computer.

```
shellmate install obs studio              shellmate check if everything is fine with the system
shellmate update the system               shellmate why is there no sound
shellmate roll mesa back to the old version   shellmate what is slowing my computer down
shellmate what takes up so much disk space    shellmate which package has the command convert
shellmate system info                     shellmate who is using port 8080
shellmate how to enable hdr in hyprland   shellmate search the web for when kernel 7.3 comes out
shellmate add ~/Documents to sources      shellmate what do my notes say about the router
shellmate translate “good morning” into Spanish   cat notes.txt | shellmate translate to English
sudo apt install foo 2>&1 | shellmate explain
```

- **It doesn't write commands itself.** A small model only recognizes what you want; ordinary code
  builds the commands, and you see and confirm each one with `y`. There's a log and `shellmate undo`.
- **Runs locally**: the model lives in Ollama on your computer (picked to fit your hardware).
- **Answers with quotes, not retellings**: from official wikis (ArchWiki, Ubuntu/Debian/Fedora Docs,
  Hyprland Wiki), the web or your local documents — verbatim, with links.
- **English and Russian**: the interface follows the system language (chosen during setup or with
  `shellmate --lang en`), and requests are understood in both.

## Supported systems

| | packages | extras |
|---|---|---|
| **Arch**, CachyOS, EndeavourOS, Manjaro | pacman + AUR (paru/yay) | Arch news before updating, rollback from the cache |
| **Debian**, **Ubuntu**, Mint, Pop!_OS | apt (+ Snap on Ubuntu) | rollback to a version from the repository |
| **Fedora**, RHEL, Rocky, Alma | dnf (4 and 5) | rollback with dnf downgrade |

Flatpak everywhere. To check how shellmate works with your system: `shellmate --selftest`.

## Installation

In one command (Arch/CachyOS, Debian, Ubuntu, Fedora):

```sh
curl -fsSL https://raw.githubusercontent.com/radeonchik/shellmate/main/get.sh | sh
```

The script installs [uv](https://docs.astral.sh/uv/) and shellmate without sudo, and git, fzf and
[Ollama](https://ollama.com) only after asking. Older AMD GPUs (Polaris, Vega) get Ollama's Vulkan
build, since ROCm doesn't support them. At the end it offers the setup: language, style, color, and
a model picked for your hardware. Your own name for the assistant: `… | sh -s -- friday`.

Or by hand:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh          # uv (or from your distro's packages)
curl -fsSL https://ollama.com/install.sh | sh             # Ollama (on Arch: sudo pacman -S ollama-cuda)

uv tool install git+https://github.com/radeonchik/shellmate
shellmate                                                    # setup: language, style, color, model
```

Recommended: `fzf` (selection menus) and `gio` (trash, usually already there). Your own name for the
assistant: `shellmate --alias friday`, after which `friday update the system` works too.

**Update:** `shellmate --update` (once a day shellmate reminds you when a new version is out).
**Uninstall:** `uv tool uninstall shellmate`; settings are in `~/.config/shellmate`, cache in
`~/.cache/shellmate`, the log in `~/.local/state/shellmate`.

## Modes and commands

- `shellmate` with no arguments — a **conversation**: the model stays in memory, ↑/↓ browse earlier
  requests, follow-ups work (“install gimp” → “now remove it”, “how do I turn that off?”).
- `-d` or “think hard”, “this is a hard one” — the big model does everything.
- `shellmate --history` / “what did you do” — the log; `shellmate --undo` / “undo” — reverts the last
  action: install ↔ remove, autostart on ↔ off, a file back from the trash, a package downgrade.
- Before updating — **distribution news** (Arch and CachyOS for now): “manual intervention” news about
  packages you have installed is shown in full. News only: `shellmate --news`.
- `shellmate --install-shell` — in fish, a missing command gets a typo suggestion (“gti” → git) and
  the package that has it. No model, ~0.1 s.
- `shellmate --search-setup` — web search (a Tavily key, 1000 free searches a month).
- `shellmate --pick-model`, `--setup` (style), `--lang en|ru`, `--color`, `--add-docs DIR`, `-n` (show
  the commands, don't run them), `--debug`, `--version`, `--selftest`.

Config: `~/.config/<name>/config.toml` (mode 600 — it may hold API keys).

## How it works

### The model doesn't write commands

It turns the request into JSON (`{"intent": "install", "packages": ["obs-studio"], …}`), and Ollama
constrains the output with a JSON schema. Everything else is ordinary code. The model's free-form
advice goes through a filter: lines with `rm -rf`, `dd`, `mkfs`, `chattr`, `chmod -R` and the like are
cut out.

### Two models: a fast one and a careful one

By default `model` (bonsai-2:4b) does everything. `big_model` (bonsai-2:9b) steps in only when the
fast one is unsure. Uncertainty isn't the model's own opinion; it comes from:
- **token probabilities** (Ollama `logprobs`): hesitation over the action or the package name;
- **code checks** that catch confident mistakes: a package name unlike what was typed (“obsidian” →
  obs-studio), “remove” in the request when the action is “install”, a request not understood;
- for quotes — the probability of the “answer / on topic” labels.

| (`eval/cascade_eval.py`, 143 requests, 50 hard ones) | all | hard | sent to 9B | time |
|---|---|---|---|---|
| 4B only | 92% | 80% | — | ~0.5–0.7 s |
| 9B only | 98% | 96% | 100% | ~1.5–1.7 s |
| cascade | 98% | 94% | 22% | ~0.7–0.9 s |

### Answers as quotes (`evidence.py`)

1. Text is split into sentences that keep their page section (“HDR › Hyprland”); commands and code
   blocks after “…:” are attached to their sentence.
2. Code drops sentences about *another* program of the same kind: Sway/KDE for a question about
   Hyprland, apt/dnf for a question about Arch (the default context is your distribution), GDM for SDDM.
3. The model picks up to 5 sentence numbers (the JSON schema only allows existing ones), then grades
   each quote separately: answer / on topic / off.
4. Quotes are shown verbatim; a translation goes sentence by sentence next to the original. When there
   is no answer, shellmate says so.

Sources in order: **favored wikis** for your distribution (each with its own search that doesn't
depend on search engines) → **the web** (Tavily → Brave → your own SearXNG → DuckDuckGo, one-day
cache) → an explanation by the model marked “no sources, may be wrong”.

### Code

| file | what it does |
|---|---|
| `backends/` | pacman / apt / dnf: search, install, rollback, “who owns this file”, cleanup, news |
| `packages.py` | on top of the backend: Flatpak, Snap, ranking, matching by spelling |
| `router.py` | prompt, examples, schema → `Intent`; “doubts” for the cascade |
| `evidence.py`, `rag.py`, `trusted.py`, `web.py`, `docs.py` | quotes, wikis, search, documents |
| `system.py`, `skills_system.py` | services, journal, processes, hardware, network (read-only) |
| `errors.py` | dictionary of pacman / apt / dnf / snap errors |
| `i18n.py` | interface language: `t("по-русски", "in English")` side by side in the code |
| `cli.py` | entry point, confirmations, log, undo, conversation |
| `ui.py` | animation, panels, themes, fzf menus |

## Development

```sh
git clone https://github.com/radeonchik/shellmate && cd shellmate
./install.sh                    # editable: code changes work right away
python3 -m unittest discover -s tests
python3 -m shellmate --selftest
uv run eval/run_eval.py bonsai-2:4b -v              # request recognition (needs Ollama)
uv run eval/cascade_eval.py bonsai-2:4b bonsai-2:9b # cascade calibration
uv run eval/quotes_eval.py --compare bonsai-2:4b    # quote quality
```

CI (`.github/workflows/test.yml`) runs the tests on Python 3.10 and 3.13, `--selftest` in Arch,
Debian 12, Ubuntu 22.04/24.04 and Fedora containers, and the one-line installer under dash.

**New version:** bump `version` in `pyproject.toml`, add to `CHANGELOG.md`, then
`git tag v0.3.0 && git push --tags` — the `release.yml` workflow creates the release, and users get a
hint about `--update`.

A new error → `Known(...)` in `errors.py` (Russian and English texts). A new intent → `INTENTS` and
examples in `router.py`, a method with the same name in `App` or `skills_*.py`. A new distribution →
a class in `backends/` and a line in `backends/__init__.py`. Every user-facing message goes through
`t(ru, en)`.

## License

[GPL-3.0-or-later](LICENSE). You may use, change and share it; derived versions must stay open under
the GPL too.
