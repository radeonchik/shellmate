# Changelog

## 0.3.3

- **Offline documentation.** The setup (or `--library update`) downloads ArchWiki — the best general
  Linux reference, on any distribution, from the arch-wiki-docs package without sudo — tldr command
  examples (English, plus Russian for a Russian interface), the manual of your shell (bash, fish or
  zsh) and your distribution's docs (CachyOS Wiki, Debian Reference, Ubuntu Server docs, Fedora Quick
  Docs). About 50 MB, searched locally in milliseconds: on 10 Linux how-to questions the median answer
  time went from 9.0 s to 4.5 s. `--library` shows what is downloaded; after updating from an older
  version run `shellmate --library update` once.
- **What does this command do?** `--check 'command'`, `--check` alone for the clipboard, Alt+E in
  fish (bash/zsh lines in `--install-shell`), or just ask. The command is never run: code splits it
  (pipes, substitutions, sudo/xargs wrappers) and describes each program and option verbatim from
  tldr and its man page. Rules flag what people paste without reading — a downloaded script piped into
  a shell, `rm -rf ~`, `dd` onto a disk, ssh keys sent to a server, a reverse shell, third-party
  repositories, a partial upgrade on Arch, a fork bomb — and list what it would change. For
  `curl … | sh` the script can be downloaded and checked line by line, still without running it.
- **What broke after the update.** The last update session from the package manager's own log
  (pacman, apt, rpm), the journal errors that appeared after it, and the updated packages they point
  to (the program's package, a driver named in the message, the subsystem: sound, graphics, network,
  bluetooth, boot, desktop). Notes when only a reboot helps, the distribution news of those days, and
  a menu to roll the suspects back.
- **Games.** “Will Elden Ring run?” — Steam and GOG, a native version, the ProtonDB rating, Steam Deck
  status, anti-cheat support (AreWeAntiCheatYet), and real reports from people with the same GPU
  generation (the ProtonDB export, ODbL, downloaded on the first question): how many it works for,
  which Proton and launch options they use, what they wrote. “Is my computer ready for games?” checks
  Vulkan, 32-bit libraries, Steam, GameMode, MangoHud, gamescope and offers to install what's missing.
- Routing: “оболочка” is the shell, not a terminal emulator; “the logs of a service” go to its status.

## 0.3.2

- Answers to general questions much better: on 30 questions from history, philosophy, science,
  geography, sports, cooking and Linux (`eval/web_eval.py`, the real path end to end) 21 → 28 right,
  median time ~6 s → 4.6 s.
  - Wikipedia is a favored source for facts: its own search (no search credits, no keys), searched by
    the whole phrase and by the meaningful words, the article's lead always considered. Linux how-to
    still goes to ArchWiki / Hyprland Wiki first; news and prices only to the web.
  - “какая …” was taken for a how-to question (“как”) and sent to the Linux wikis.
  - The answer sketch used for ranking is always in English (9B wrote it in the question's language).
  - Recipes, formulas and other everyday questions go to the search, not “can't do that”.
  - A hesitant “off” for the sentence the model itself picked first no longer drops a right answer.
- Ollama's memory: llama.cpp inside it keeps up to 8 GB of prompt cache by default, and with web
  answers systemd-oomd killed it. The setup (and `get.sh`) offers a 1 GB limit through the service's
  environment; a dropped connection now says what happened instead of a Python traceback.
- Wikipedia API calls name shellmate in their User-Agent and are cached for three days.

## 0.3.1

- Questions about your own files that don't say "documents": “take a look at my timetable, what do I
  have on Wednesday”, “что у меня в пятницу по расписанию”. Searching also by the file name (“my
  timetable” finds `timetable.txt`) and by English keywords, so Russian questions find English files.
- What is in a folder: “сколько файлов на рабочем столе и какие их виды”, “what's in my downloads” —
  files counted and grouped by kind (documents, images, shortcuts, programs, archives…) by code. A
  localized desktop folder is found through xdg-user-dirs.
- `--add-docs` takes a single file too, not only a folder.
- “label: value” lines (a timetable, a list of settings, “Born: April 1, 1815”) are separate quotes
  instead of being glued together, and short ones aren't dropped as junk.
- Weekdays and months named in a request are passed on by code: the models dropped or swapped them
  (“в четверг” → “wednesday”).
- A quote's translation is shown only if it keeps the original's numbers, weekdays and months (9B
  rendered “friday” as “пн”); otherwise only the original.

## 0.3.0

**Install and languages**
- Install in one command: `curl -fsSL https://raw.githubusercontent.com/radeonchik/shellmate/main/get.sh | sh`
  — uv and shellmate without sudo, git/fzf/Ollama only after asking, then the setup. Checked in CI
  under dash.
- English interface (and Russian, as before): chosen by the desktop locale, in the setup's first step
  or with `--lang en|ru`. Messages, the error dictionary, communication styles, `--help`, `get.sh` and
  the README (`README.md` in English, `README.ru.md` in Russian). Requests are understood in both.
- Translation: “переведи на английский «доброе утро»”, “how do you say cat in German”, or a file:
  `cat notes.txt | shellmate translate to Russian`. Without a named language it translates into the
  interface language (into English when the text is already in it); a copy of the original is caught
  and redone with the target language named.

**Answers from the web**
- Factual questions about the world (history, people, places, science: “years of Pushkin's life”,
  “capital of Australia”) go to the web search instead of “can't do that”; if the model still says
  “can't do that”, shellmate offers to search the web. Routing (`eval/run_eval.py`, 106 requests):
  bonsai-2:4b 92% → 98%, bonsai-2:9b 99% → 100%; the cascade stays at 98% with 20% sent to the big model.
- Pages rarely use the question's words (“years of life” is answered by “born … died …”), so while the
  search runs the model sketches what an answer usually looks like, and page chunks are ranked by the
  question and by that sketch in turn. The sketch is never shown — answers are still only verbatim
  quotes. `eval/quotes_eval.py --shape`: 12/13 → 13/13 cases, precision 80–90% → 97%.
- Video pages (YouTube…) are no longer read as sources; the same quote is no longer shown twice.
- A cached web search that gives no answer is repeated live once, so a provider's bad moment no longer
  sticks for a day.
- ArchWiki: when its clean article view (`action=render`) fails (it answered 502), the full page is read.

**Hardware**
- Older AMD GPUs (Polaris, Vega — e.g. Radeon Pro 555X/560X, RX 470–590) get Ollama's Vulkan build
  instead of ROCm, which doesn't support them (and is a 3 GB download on Arch).
- The second (9B) model is only offered when it fits in video memory.

**Fixes**
- The config saved fractional numbers (`escalate_intent = 0.5`) as strings, which broke the first
  request after the setup when the cascade was on. Such configs now load correctly.
- Dim and italic text (originals under translated quotes, “also on topic”) kept its style only on the
  first line of a wrapped quote.
- If Ollama is installed but its service is stopped, the setup says so instead of failing on download.

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
