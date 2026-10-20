"""Per-agent state in ~/.local/state/<name>/: the action log (for history and undo) and the
last request (so follow-ups like "а теперь удали его" know what "его" is)."""

import json
import os
import time
from pathlib import Path

CONTEXT_TTL = 15 * 60  # a follow-up after 15 minutes is a new conversation


def state_dir(name: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- action log --------------------------------------------------------------------------------
#
# One JSON object per line: {id, time, query, label, cmds, undo, undone}. `undo` is data, not a
# command — it's turned into commands only when the user asks to undo (see cli.App.undo), so
# e.g. a trashed file is looked up in the trash at that moment.


def _log(name: str) -> Path:
    return state_dir(name) / "actions.jsonl"


def log_action(name: str, query: str, label: str, cmds: list[list[str]], undo: dict | None) -> None:
    entry = {"id": time.time_ns(), "time": time.time(), "query": query, "label": label,
             "cmds": cmds, "undo": undo, "undone": False}
    with _log(name).open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def history(name: str, n: int = 15) -> list[dict]:
    try:
        lines = _log(name).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-n * 3:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[-n:]


def last_undoable(name: str) -> dict | None:
    return next((e for e in reversed(history(name, 50)) if e.get("undo") and not e.get("undone")), None)


def mark_undone(name: str, entry_id: int) -> None:
    path = _log(name)
    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for e in entries:
        if e["id"] == entry_id:
            e["undone"] = True
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))


# --- conversation context ---------------------------------------------------------------------


def save_context(name: str, query: str, intent: dict, result: dict | None = None) -> None:
    turns = load_context(name)[-2:]  # keep a short memory: the last three requests
    turns.append({"time": time.time(), "query": query, "intent": intent, "result": result or {}})
    (state_dir(name) / "context.json").write_text(json.dumps(turns, ensure_ascii=False))


def load_context(name: str) -> list[dict]:
    try:
        turns = json.loads((state_dir(name) / "context.json").read_text())
    except (OSError, ValueError):
        return []
    return [t for t in turns if time.time() - t.get("time", 0) < CONTEXT_TTL]
