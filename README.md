# Claude Session Viewer

A local browser app that lists every Claude Code session you've ever run, marks the ones that are currently alive, and lets you click any row to jump directly to the iTerm or Ghostty pane (and tmux pane, if applicable) where it lives.

```
http://127.0.0.1:8765
```

## Features

- Lists all sessions from `~/.claude/projects/<dir>/*.jsonl`, grouped by project, newest first.
- **Live detection** — each running `claude` process is mapped to its session via process start time ↔ jsonl birth time.
- **Working / idle** — green pulsing dot if claude is generating, amber dot if waiting on you, gray dot if dead.
- **Recap inline** — shows the most recent `※ recap` (`away_summary`) from each jsonl. Falls back to the last user/assistant message text when no recap exists yet.
- **Click-to-focus** — clicking a live row activates the matching iTerm/Ghostty pane via that terminal's scripting API. Also runs `tmux select-window/select-pane` if claude is inside tmux.
- **Resume** — clicking a dead row spawns a fresh tab in the right cwd running `claude --resume <id>`.
- **Delete** — `×` button on dead rows moves the jsonl to the macOS Trash (recoverable).
- **Notifications** — when a session transitions working → idle, a native macOS notification fires via `terminal-notifier`. Click it to jump straight to the pane (no browser flicker).
- **Live updates** — SSE push; no polling.

## Requirements

- macOS (this app uses AppleScript and the iterm2 Python API)
- [`uv`](https://docs.astral.sh/uv/) — handles Python and deps automatically
- `iTerm.app` and/or `Ghostty.app`
- For iTerm: the **Python API must be enabled** (iTerm → Settings → General → Magic → "Enable Python API")
- For notifications: `terminal-notifier` (Homebrew) — without it, notifications fall back to plain `osascript` with no click handler

## Install

```sh
brew install uv terminal-notifier
git clone <this-repo> cc-switchboard
cd cc-switchboard
```

That's it. `uv` will install the right Python version and the deps (`fastapi`, `uvicorn`, `iterm2`) the first time you run the server — they're declared inline in `server.py` via PEP-723 metadata. The first time `terminal-notifier` fires, macOS will prompt you to allow notifications for it — approve.

## Run

From inside the cloned directory:

```sh
uv run cc-switchboard
```

Then open <http://127.0.0.1:8765>.

Also works:
- `uv run server.py` — same effect, no entry-point lookup
- `./server.py` — uses the `#!/usr/bin/env -S uv run --script` shebang and the inline PEP-723 metadata

### Flags

- `-d`, `--background` — start detached; logs to `/tmp/cc-switchboard.log`
- `--no-open` — skip the browser auto-launch
- `--port N` — bind to a non-default port
- `--stop` — terminate any running viewer (matches by argv[0], won't kill unrelated processes)

### Run from anywhere

Install the project's entry point as a tool:

```sh
uv tool install .
```

Then `cc-switchboard` is available on your `PATH` from any directory.

## How it works

### Listing sessions

Reads `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. For each jsonl, scans for the latest `type=system, subtype=away_summary` line to produce the recap, plus the last user/assistant turn for activity detection. Results are cached by file mtime.

### Detecting live sessions

For every running `claude` process:
1. `ps` → PID + tty + start time
2. `lsof` → cwd (used to find the matching project dir)
3. `ps -E` → environment, looking for `TERM_PROGRAM`, `ITERM_SESSION_ID`, etc.
4. Walk the parent process chain to detect tmux

Each PID is paired to a jsonl by matching the process start time to the jsonl's `st_birthtime`. This works even when several claude processes share a cwd (e.g., multiple shells in `~/`).

### Focusing a pane

- **iTerm**: looks up the session by `ITERM_SESSION_ID` (UUID) via the iterm2 Python API; falls back to tty match. Finally runs `osascript -e 'tell application "iTerm" to activate'` to force iTerm to be the frontmost app.
- **Ghostty**: AppleScript — `tell application "Ghostty" to repeat with term ... focus term`. Match is by terminal id (from cwd + name="✳ Claude Code").
- **tmux**: after focusing the outer terminal, runs `tmux select-window -t <s:w>` and `tmux select-pane -t <s:w.p>` to switch to claude's pane.

### Notifications

Server-side. When the SSE broadcaster sees a session flip from `working` to `idle`, the client posts to `/api/notify`, which spawns:

```
terminal-notifier -title "Claude idle" -subtitle "~/project" -message "<recap>" \
                  -execute "curl -X POST http://127.0.0.1:8765/api/focus/<id>"
```

Because `terminal-notifier` is the originating app (not Chrome), macOS doesn't auto-foreground a browser on click — the `curl` runs and activates iTerm/Ghostty directly.

## Limitations

- macOS only (iTerm Python API, AppleScript, terminal-notifier, `st_birthtime`).
- Inside tmux: if **two** claudes run in the same cwd inside the same project dir AND have nearly identical start times, the PID-to-session matcher can be off.
- Ghostty has no per-session env var equivalent to `ITERM_SESSION_ID`, so multi-claude-same-cwd disambiguation in Ghostty falls back to "first matching terminal."
- Run only one browser tab at a time — SSE subscribers don't dedupe notifications.

## Files

- `server.py` — FastAPI server, SSE broadcaster, focus/delete/notify endpoints.
- `static/index.html` — UI (single file, no build step).
