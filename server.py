#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fastapi",
#     "uvicorn",
#     "iterm2",
# ]
# ///
"""Browser viewer for Claude Code sessions with iTerm jump-to-pane."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil as _shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

PROJECTS_DIR = Path.home() / ".claude" / "projects"
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_broadcast_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(lifespan=lifespan)


# ---------- live process detection ----------

import datetime as _dt


@dataclass
class LiveClaude:
    pid: int
    tty: str
    cwd: str
    start_epoch: float
    term_program: str  # "iTerm.app" | "ghostty" | other/unknown
    iterm_uuid: Optional[str]  # iTerm session UUID from ITERM_SESSION_ID env
    ghostty_term_id: Optional[str]  # Ghostty terminal AppleScript id
    session_id: Optional[str]  # claude session id (jsonl filename stem)
    tmux_target: Optional[str] = None  # e.g. "main:0.1" — set if claude runs inside tmux


def _ps_claude() -> list[tuple[int, str, float]]:
    """Return [(pid, tty_short, start_epoch)] for all `claude` processes."""
    out = subprocess.run(
        ["ps", "-ax", "-o", "pid=,tty=,lstart=,command="],
        capture_output=True, text=True, check=False,
    ).stdout
    rows = []
    for line in out.splitlines():
        # lstart spans 5 whitespace-separated tokens: "Thu 21 May 16:29:40 2026"
        parts = line.strip().split(None, 7)
        if len(parts) < 8:
            continue
        pid_s, tty, dow, dom, mon, hms, year, cmd = parts
        if tty == "??" or not cmd.startswith("claude"):
            continue
        head = cmd.split(None, 1)[0]
        if head != "claude":
            continue
        try:
            dt = _dt.datetime.strptime(
                f"{dow} {dom} {mon} {hms} {year}", "%a %d %b %H:%M:%S %Y"
            )
            start_epoch = dt.timestamp()
        except ValueError:
            start_epoch = 0.0
        try:
            rows.append((int(pid_s), tty, start_epoch))
        except ValueError:
            continue
    return rows


def _proc_ppid(pid: int) -> Optional[int]:
    r = subprocess.run(["ps", "-p", str(pid), "-o", "ppid="], capture_output=True, text=True, check=False)
    s = r.stdout.strip()
    try:
        return int(s) if s else None
    except ValueError:
        return None


def _proc_command(pid: int) -> str:
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
    return r.stdout.strip()


def _proc_tty(pid: int) -> Optional[str]:
    r = subprocess.run(["ps", "-p", str(pid), "-o", "tty="], capture_output=True, text=True, check=False)
    s = r.stdout.strip()
    return s or None


def _ancestor_pids(pid: int, depth: int = 25) -> list[int]:
    out = []
    cur = pid
    for _ in range(depth):
        ppid = _proc_ppid(cur)
        if ppid is None or ppid <= 1:
            break
        out.append(ppid)
        cur = ppid
    return out


def _tmux_target_for_pid(pid: int) -> Optional[dict]:
    """If `pid` runs inside tmux, return target info.

    Returns dict with keys: target (session:win.pane), session, window, pane,
    client_pid, client_tty (outer terminal's tty), client_env.
    """
    tmux = _shutil.which("tmux")
    if not tmux:
        return None
    # Cheap pre-check: does any ancestor look like tmux server?
    ancestors = _ancestor_pids(pid)
    has_tmux = False
    for ap in ancestors:
        cmd = _proc_command(ap)
        if cmd.startswith("tmux") or cmd.startswith("/usr/local/bin/tmux") or "tmux:" in cmd:
            has_tmux = True
            break
    if not has_tmux:
        return None

    # Look up which pane this pid (or one of its ancestors) is the shell of.
    r = subprocess.run(
        [tmux, "list-panes", "-a", "-F",
         "#{pane_pid}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_tty}"],
        capture_output=True, text=True, check=False, timeout=2,
    )
    if r.returncode != 0:
        return None
    pane_row = None
    chain = [pid] + ancestors
    for line in r.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) < 5:
            continue
        try:
            pane_pid = int(cols[0])
        except ValueError:
            continue
        if pane_pid in chain:
            pane_row = {"pane_pid": pane_pid, "session": cols[1], "window": cols[2],
                        "pane": cols[3], "pane_tty": cols[4]}
            break
    if not pane_row:
        return None
    target = f"{pane_row['session']}:{pane_row['window']}.{pane_row['pane']}"

    # Find an attached client (the one whose terminal we should focus).
    r2 = subprocess.run(
        [tmux, "list-clients", "-t", pane_row["session"], "-F", "#{client_pid}\t#{client_tty}"],
        capture_output=True, text=True, check=False, timeout=2,
    )
    client_pid: Optional[int] = None
    client_tty: Optional[str] = None
    if r2.returncode == 0:
        for line in r2.stdout.splitlines():
            cols = line.split("\t")
            if len(cols) < 2:
                continue
            try:
                client_pid = int(cols[0])
            except ValueError:
                client_pid = None
            client_tty = cols[1]
            break  # first attached client wins
    client_env = _env_for_pid(client_pid) if client_pid else {}
    return {
        "target": target,
        "session": pane_row["session"],
        "window": pane_row["window"],
        "pane": pane_row["pane"],
        "client_pid": client_pid,
        "client_tty": client_tty,
        "client_env": client_env,
    }


def _tmux_select(target: str) -> None:
    tmux = _shutil.which("tmux")
    if not tmux or not target:
        return
    session_window = target.rsplit(".", 1)[0]
    subprocess.run([tmux, "select-window", "-t", session_window], check=False, timeout=2)
    subprocess.run([tmux, "select-pane", "-t", target], check=False, timeout=2)


def _tmux_session_exists(session: str) -> bool:
    tmux = _shutil.which("tmux")
    if not tmux or not session:
        return False
    r = subprocess.run(
        [tmux, "has-session", "-t", session],
        capture_output=True, text=True, check=False, timeout=2,
    )
    return r.returncode == 0


def _reattach_tmux(target: str, cwd: str, term_program: str) -> None:
    """Open a fresh terminal tab and `tmux attach` to the target session+pane."""
    session = target.split(":", 1)[0]
    # attach, then once attached, switch to the right window/pane
    cmd = (
        f"tmux attach -t {shlex.quote(session)} \\; "
        f"select-window -t {shlex.quote(target.rsplit('.', 1)[0])} \\; "
        f"select-pane -t {shlex.quote(target)}"
    )
    if term_program == "ghostty":
        _ghostty_run_in_new_tab(cwd, cmd)
    else:
        # iTerm path — replicate the inner shell construction without resume semantics
        script_cmd = f"cd {shlex.quote(cwd)} && {cmd}"
        applescript = f'''
        tell application "iTerm"
            activate
            if (count of windows) = 0 then
                create window with default profile
            else
                tell current window to create tab with default profile
            end if
            tell current session of current window to write text {json.dumps(script_cmd)}
        end tell
        '''
        subprocess.run(["osascript", "-e", applescript], check=False)


def _lsof_cwd(pid: int) -> Optional[str]:
    out = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd"],
        capture_output=True, text=True, check=False,
    ).stdout
    for line in out.splitlines()[1:]:
        cols = line.split(None, 8)
        if len(cols) >= 9:
            return cols[8]
    return None


def _env_for_pid(pid: int) -> dict[str, str]:
    """Best-effort parse of a process's env via `ps -E`. Only safe for KEY=VALUE
    pairs without internal spaces (env values with spaces will be truncated).
    Good enough for the env vars we care about (TERM_PROGRAM, ITERM_SESSION_ID)."""
    out = subprocess.run(
        ["ps", "-E", "-p", str(pid), "-o", "command="],
        capture_output=True, text=True, check=False,
    ).stdout
    env: dict[str, str] = {}
    for tok in out.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            if k and k.isupper() and k[0].isalpha():
                env[k] = v
    return env


def _iterm_uuid_for_pid(pid: int, env: dict[str, str]) -> Optional[str]:
    val = env.get("ITERM_SESSION_ID")
    if not val:
        return None
    return val.split(":", 1)[1] if ":" in val else val


def cwd_to_project_dir(cwd: str) -> str:
    """Replicate Claude Code's project-dir naming: any non-alphanumeric char becomes '-'."""
    encoded = re.sub(r"[^A-Za-z0-9-]", "-", cwd)
    if not encoded.startswith("-"):
        encoded = "-" + encoded
    return encoded


def _jsonl_birth(path: Path) -> float:
    try:
        return path.stat().st_birthtime  # type: ignore[attr-defined]
    except AttributeError:
        return path.stat().st_ctime


def _match_pids_to_sessions(
    pids: list[tuple[int, float]],  # (pid, start_epoch) restricted to one project dir
    project_dir: Path,
) -> dict[int, str]:
    """For each PID, find the jsonl whose birth time best matches the PID's start.

    A claude session writes its first line ~0-60s after the process starts, so we
    match each PID to the jsonl with the smallest non-negative (birth - start)
    offset. Greedy by ascending start time; each jsonl claimed once.
    """
    if not project_dir.is_dir():
        return {}
    jsonls = [(p.stem, _jsonl_birth(p)) for p in project_dir.glob("*.jsonl")]
    if not jsonls:
        return {}
    pids_sorted = sorted(pids, key=lambda x: x[1])
    used: set[str] = set()
    out: dict[int, str] = {}
    for pid, start in pids_sorted:
        best_sid = None
        best_score = float("inf")
        for sid, birth in jsonls:
            if sid in used:
                continue
            offset = birth - start
            # accept small negative slack (clock skew); penalize anything > 5 min
            if offset < -5 or offset > 600:
                continue
            score = abs(offset)
            if score < best_score:
                best_score = score
                best_sid = sid
        if best_sid is None:
            # fallback: pick the unused jsonl with the closest birth time
            for sid, birth in jsonls:
                if sid in used:
                    continue
                score = abs(birth - start)
                if score < best_score:
                    best_score = score
                    best_sid = sid
        if best_sid is not None:
            used.add(best_sid)
            out[pid] = best_sid
    return out


def _ghostty_terminals() -> list[dict]:
    """Return [{id, name, cwd}] for all Ghostty terminals via AppleScript."""
    script = '''
    on tildeQuote(s)
        return s
    end tildeQuote
    set out to ""
    tell application "Ghostty"
        repeat with w in windows
            repeat with tb in tabs of w
                repeat with term in terminals of tb
                    set out to out & (id of term) & "\\t" & (name of term) & "\\t" & (working directory of term) & "\\n"
                end repeat
            end repeat
        end repeat
    end tell
    return out
    '''
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, check=False, timeout=3,
        ).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append({"id": parts[0], "name": parts[1], "cwd": parts[2]})
    return rows


def _ghostty_term_id_for_pid(cwd: str, ghostty_terms: list[dict]) -> Optional[str]:
    """Match a Claude PID's cwd to a Ghostty terminal by working_directory.

    Prefers terminals whose name indicates Claude Code is running.
    If multiple match, returns the first (we can't disambiguate further)."""
    candidates = [t for t in ghostty_terms if t["cwd"] == cwd]
    if not candidates:
        return None
    claude_named = [t for t in candidates if "claude" in t["name"].lower() or "✳" in t["name"]]
    chosen = claude_named[0] if claude_named else candidates[0]
    return chosen["id"]


def get_live_claudes() -> list[LiveClaude]:
    claudes = _ps_claude()
    ghostty_terms: Optional[list[dict]] = None  # lazy

    enriched = []
    for pid, tty, start in claudes:
        cwd = _lsof_cwd(pid)
        if not cwd:
            continue
        env = _env_for_pid(pid)
        term_program = env.get("TERM_PROGRAM", "")
        iterm_uuid = _iterm_uuid_for_pid(pid, env) if term_program == "iTerm.app" else None
        ghostty_id: Optional[str] = None
        tmux_target: Optional[str] = None

        tmux_info = _tmux_target_for_pid(pid)
        if tmux_info:
            tmux_target = tmux_info["target"]
            # Inside tmux, claude's own env/tty point at the tmux pane, not
            # the outer terminal. Discard them — only the attached client's
            # info is useful for focusing. If no client is attached, leave
            # everything empty so api_focus falls through to the reattach
            # path (which spawns a fresh terminal running `tmux attach`).
            iterm_uuid = None
            ghostty_id = None
            tty = ""
            term_program = ""

            ce = tmux_info.get("client_env", {}) or {}
            client_tty = tmux_info.get("client_tty") or ""
            outer_term = ce.get("TERM_PROGRAM", "")
            if client_tty and outer_term:
                # Normalize "/dev/ttysNNN" → "ttysNNN" so it matches our tty format.
                if client_tty.startswith("/dev/"):
                    client_tty = client_tty[5:]
                tty = client_tty
                term_program = outer_term
                if outer_term == "iTerm.app":
                    iterm_uuid = _iterm_uuid_for_pid(pid, ce)
                elif outer_term == "ghostty":
                    if ghostty_terms is None:
                        ghostty_terms = _ghostty_terminals()
                    client_cwd = (
                        _lsof_cwd(tmux_info["client_pid"])
                        if tmux_info.get("client_pid") else None
                    )
                    if client_cwd:
                        ghostty_id = _ghostty_term_id_for_pid(client_cwd, ghostty_terms)
        else:
            if term_program == "ghostty":
                if ghostty_terms is None:
                    ghostty_terms = _ghostty_terminals()
                ghostty_id = _ghostty_term_id_for_pid(cwd, ghostty_terms)

        enriched.append((pid, tty, start, cwd, term_program, iterm_uuid, ghostty_id, tmux_target))

    # Group by project dir for session matching
    by_project: dict[str, list[tuple[int, float]]] = {}
    for pid, _tty, start, cwd, *_ in enriched:
        by_project.setdefault(cwd_to_project_dir(cwd), []).append((pid, start))

    pid_to_sid: dict[int, str] = {}
    for project_dir_name, pid_starts in by_project.items():
        matches = _match_pids_to_sessions(pid_starts, PROJECTS_DIR / project_dir_name)
        pid_to_sid.update(matches)

    return [
        LiveClaude(
            pid=pid, tty=tty, cwd=cwd, start_epoch=start,
            term_program=term_program, iterm_uuid=iterm_uuid,
            ghostty_term_id=ghostty_id, session_id=pid_to_sid.get(pid),
            tmux_target=tmux_target,
        )
        for pid, tty, start, cwd, term_program, iterm_uuid, ghostty_id, tmux_target in enriched
    ]


# ---------- session listing ----------

@dataclass
class SessionInfo:
    session_id: str
    project_dir: str  # the encoded dir name
    project_path: str  # decoded cwd if known
    file_mtime: float
    first_prompt: str
    summary: str
    message_count: int
    git_branch: str
    jsonl_path: str
    recap: str = ""
    recap_ts: str = ""
    last_turn_role: str = ""  # "user" | "assistant" | ""
    last_stop_reason: str = ""  # "end_turn" | "tool_use" | ""
    activity: str = "dead"  # "working" | "idle" | "dead"
    is_live: bool = False
    live_tty: Optional[str] = None
    live_pid: Optional[int] = None
    live_term_program: str = ""
    live_iterm_uuid: Optional[str] = None
    live_ghostty_term_id: Optional[str] = None
    live_tmux_target: Optional[str] = None


# (jsonl_path, mtime) -> (recap_text, recap_ts, last_msg_text, last_msg_role, last_turn_role, last_stop_reason)
_recap_cache: dict[tuple[str, float], tuple[str, str, str, str, str, str]] = {}


def _latest_recap(jsonl_path: Path, mtime: float) -> tuple[str, str, str, str, str, str]:
    """Return (recap, recap_ts, last_msg_text, last_msg_role, last_turn_role, last_stop_reason).

    Single-pass scan that finds:
      - latest away_summary (recap)
      - last user/assistant message with non-empty text (for fallback display)
      - last conversation turn role + stop_reason (for working/idle detection)
    Cached by file mtime.
    """
    key = (str(jsonl_path), mtime)
    if key in _recap_cache:
        return _recap_cache[key]
    recap = ""
    recap_ts = ""
    last_text = ""
    last_role = ""
    last_turn_role = ""
    last_stop_reason = ""
    try:
        with jsonl_path.open() as f:
            for line in f:
                if "away_summary" not in line and '"type":"user"' not in line and '"type":"assistant"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = o.get("type")
                if t == "system" and o.get("subtype") == "away_summary":
                    ts = o.get("timestamp", "")
                    if ts >= recap_ts:
                        recap = o.get("content", "")
                        recap_ts = ts
                elif t in ("user", "assistant"):
                    msg = o.get("message", {}) or {}
                    last_turn_role = msg.get("role") or t
                    last_stop_reason = msg.get("stop_reason") or ""
                    text = _extract_text(msg.get("content"))
                    if text.strip():
                        last_text = text
                        last_role = last_turn_role
    except OSError:
        pass
    result = (recap, recap_ts, last_text, last_role, last_turn_role, last_stop_reason)
    _recap_cache[key] = result
    return result


def _activity_state(s: "SessionInfo", now: float) -> str:
    """Classify a live session as 'working' or 'idle'.

    Heuristic (cheap, no extra IO):
      - working if jsonl was written in the last 5s (claude is actively producing)
      - working if the last conversation turn is `user` (claude is processing input)
      - working if the last assistant turn ended with stop_reason='tool_use'
        (claude is between tool call and continuation)
      - otherwise idle (alive but waiting on the user)
    """
    if now - s.file_mtime < 5.0:
        return "working"
    if s.last_turn_role == "user":
        return "working"
    if s.last_turn_role == "assistant" and s.last_stop_reason == "tool_use":
        return "working"
    return "idle"


def _read_first_prompt(jsonl_path: Path, max_bytes: int = 32768) -> tuple[str, str, int]:
    """Return (first_user_prompt, project_path_guess, line_count_estimate)."""
    first_prompt = ""
    project_path = ""
    count = 0
    try:
        with jsonl_path.open("rb") as f:
            chunk = f.read(max_bytes)
        text = chunk.decode("utf-8", errors="replace")
        for line in text.splitlines():
            count += 1
            if not first_prompt or not project_path:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not first_prompt and obj.get("type") == "user":
                    msg = obj.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        first_prompt = content
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                first_prompt = c.get("text", "")
                                break
                if not project_path and obj.get("cwd"):
                    project_path = obj["cwd"]
    except OSError:
        pass
    return first_prompt[:300], project_path, count


def list_sessions() -> list[SessionInfo]:
    sessions: dict[str, SessionInfo] = {}
    if not PROJECTS_DIR.is_dir():
        return []

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        index_data = {}
        idx_path = project_dir / "sessions-index.json"
        if idx_path.exists():
            try:
                with idx_path.open() as f:
                    raw = json.load(f)
                for entry in raw.get("entries", []):
                    sid = entry.get("sessionId")
                    if sid:
                        index_data[sid] = entry
            except (OSError, json.JSONDecodeError):
                pass

        for jsonl in project_dir.glob("*.jsonl"):
            sid = jsonl.stem
            mtime = jsonl.stat().st_mtime
            entry = index_data.get(sid, {})
            first_prompt = entry.get("firstPrompt", "")
            project_path = entry.get("projectPath", "")
            msg_count = entry.get("messageCount", 0)
            summary = entry.get("summary", "")
            git_branch = entry.get("gitBranch", "")
            if not first_prompt:
                fp, pp, cnt = _read_first_prompt(jsonl)
                first_prompt = fp or ""
                if not project_path:
                    project_path = pp
                if not msg_count:
                    msg_count = cnt
            recap, recap_ts, last_text, last_role, last_turn_role, last_stop_reason = _latest_recap(jsonl, mtime)
            sessions[sid] = SessionInfo(
                session_id=sid,
                project_dir=project_dir.name,
                project_path=project_path,
                file_mtime=mtime,
                first_prompt=first_prompt,
                summary=summary,
                message_count=msg_count,
                git_branch=git_branch,
                jsonl_path=str(jsonl),
                recap=recap,
                recap_ts=recap_ts,
                last_turn_role=last_turn_role,
                last_stop_reason=last_stop_reason,
            )
            if not recap and last_text:
                sessions[sid].recap = f"[last {last_role}] {last_text[:600]}"

    # mark live and surface any live claude that has no jsonl yet
    now = _dt.datetime.now().timestamp()
    for live in get_live_claudes():
        if live.session_id and live.session_id in sessions:
            s = sessions[live.session_id]
            s.is_live = True
            s.live_tty = live.tty
            s.live_pid = live.pid
            s.live_term_program = live.term_program
            s.live_iterm_uuid = live.iterm_uuid
            s.live_ghostty_term_id = live.ghostty_term_id
            s.live_tmux_target = live.tmux_target
            s.activity = _activity_state(s, now)
        else:
            # Synthetic entry — claude is running but hasn't created a jsonl yet
            synthetic_id = f"pid-{live.pid}"
            sessions[synthetic_id] = SessionInfo(
                session_id=synthetic_id,
                project_dir=cwd_to_project_dir(live.cwd),
                project_path=live.cwd,
                file_mtime=max(live.start_epoch, now - 1),
                first_prompt="",
                summary="",
                message_count=0,
                git_branch="",
                jsonl_path="",
                recap="(new session — no messages yet)",
                recap_ts="",
                activity="idle",
                is_live=True,
                live_tty=live.tty,
                live_pid=live.pid,
                live_term_program=live.term_program,
                live_iterm_uuid=live.iterm_uuid,
                live_ghostty_term_id=live.ghostty_term_id,
                live_tmux_target=live.tmux_target,
            )

    return sorted(sessions.values(), key=lambda s: s.file_mtime, reverse=True)


# ---------- iTerm control ----------

async def _iterm_focus(uuid_: Optional[str], tty_short: Optional[str]) -> bool:
    """Focus an iTerm session by UUID (preferred) or tty fallback.

    Wraps the iterm2 Python API in an overall timeout so a stuck connection
    can't hang the HTTP handler indefinitely.
    """
    try:
        import iterm2  # noqa
    except ImportError:
        return False

    target_dev = f"/dev/{tty_short}" if tty_short else None

    async def _do_focus() -> bool:
        connection = await iterm2.Connection.async_create()
        app_ = await iterm2.async_get_app(connection)
        if app_ is None:
            return False

        for window in app_.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    hit = False
                    if uuid_ and getattr(session, "session_id", None) == uuid_:
                        hit = True
                    elif target_dev:
                        try:
                            tty = await asyncio.wait_for(
                                session.async_get_variable("tty"), timeout=1.0
                            )
                        except Exception:
                            tty = None
                        if tty == target_dev:
                            hit = True
                    if hit:
                        await window.async_activate()
                        await tab.async_select()
                        await session.async_activate(order_window_front=True)
                        return True
        return False

    try:
        matched = await asyncio.wait_for(_do_focus(), timeout=5.0)
    except asyncio.TimeoutError:
        print(f"[focus] iTerm Python API timed out (uuid={uuid_}, tty={tty_short})")
        return False
    except Exception as e:
        print(f"[focus] iTerm Python API error: {e!r}")
        return False

    if matched:
        # Force iTerm.app to be the frontmost application on macOS, overriding
        # any browser/notification source app that may have just stolen focus.
        subprocess.run(
            ["osascript", "-e", 'tell application "iTerm" to activate'],
            check=False, timeout=2,
        )
    return matched


def _iterm_new_tab(cwd: str, session_id: str) -> None:
    """Open a new iTerm tab in `cwd` running `claude --resume <id>`."""
    cmd = f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)}"
    script = f'''
    tell application "iTerm"
        activate
        if (count of windows) = 0 then
            create window with default profile
        else
            tell current window to create tab with default profile
        end if
        tell current session of current window to write text {json.dumps(cmd)}
    end tell
    '''
    subprocess.run(["osascript", "-e", script], check=False)


def _ghostty_focus(term_id: str) -> bool:
    """Focus a Ghostty terminal by its AppleScript id."""
    script = f'''
    tell application "Ghostty"
        activate
        repeat with w in windows
            repeat with tb in tabs of w
                repeat with term in terminals of tb
                    if (id of term as string) is {json.dumps(term_id)} then
                        focus term
                        return "ok"
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return "miss"
    '''
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, check=False, timeout=3,
        )
        return r.stdout.strip() == "ok"
    except subprocess.TimeoutExpired:
        return False


def _ghostty_run_in_new_tab(cwd: str, shell_cmd: str) -> None:
    """Open a new Ghostty tab and run an arbitrary shell command in `cwd`."""
    cmd = f"cd {shlex.quote(cwd)} && {shell_cmd}\n"
    script = f'''
    tell application "Ghostty"
        activate
        set cfg to new surface configuration
        if (count of windows) > 0 then
            set newTab to new tab in front window with configuration cfg
            set newTerm to focused terminal of newTab
        else
            set newWin to new window with configuration cfg
            set newTerm to focused terminal of selected tab of newWin
        end if
        input text {json.dumps(cmd)} to newTerm
    end tell
    '''
    subprocess.run(["osascript", "-e", script], check=False)


# ---------- routes ----------

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/sessions")
async def api_sessions():
    sessions = await asyncio.to_thread(list_sessions)
    return JSONResponse([asdict(s) for s in sessions])


def _is_noise_text(t: str) -> bool:
    s = t.strip()
    return (
        not s
        or s.startswith("<")
        or s.startswith("[Request interrupted")
        or s.startswith("[Tool use rejected")
        or s.startswith("Caveat:")
    )


def _extract_text(content) -> str:
    """Extract real text only. Skips tool calls/results/thinking and interrupt markers."""
    if isinstance(content, str):
        return "" if _is_noise_text(content) else content
    if isinstance(content, list):
        parts = []
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                t = c.get("text", "")
                if not _is_noise_text(t):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def session_recap(session_id: str) -> dict:
    """Return the latest Claude Code 'away_summary' (the ※ recap)."""
    sessions = list_sessions()
    target = next((s for s in sessions if s.session_id == session_id), None)
    if target is None:
        return {"error": "not found"}
    path = Path(target.jsonl_path)
    latest = None
    latest_ts = ""
    with path.open() as f:
        for line in f:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("type") == "system" and o.get("subtype") == "away_summary":
                ts = o.get("timestamp", "")
                if ts >= latest_ts:
                    latest = o.get("content", "")
                    latest_ts = ts
    return {
        "session_id": session_id,
        "recap": latest,
        "recap_ts": latest_ts,
        "summary": target.summary,
        "first_prompt": target.first_prompt,
    }


@app.get("/api/recap/{session_id}")
async def api_recap(session_id: str):
    return JSONResponse(await asyncio.to_thread(session_recap, session_id))


_TERMINAL_NOTIFIER = _shutil.which("terminal-notifier")


@app.post("/api/delete/{session_id}")
async def api_delete(session_id: str):
    """Move a dead session's jsonl to the macOS Trash. Refuses if session is live."""
    sessions = await asyncio.to_thread(list_sessions)
    target = next((s for s in sessions if s.session_id == session_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="session not found")
    if target.is_live:
        raise HTTPException(status_code=409, detail="cannot delete a live session")
    if not target.jsonl_path:
        raise HTTPException(status_code=400, detail="no file to delete")
    path = Path(target.jsonl_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="file already gone")
    # Move to Trash via Finder (recoverable, unlike `rm`)
    script = f'tell application "Finder" to delete POSIX file {json.dumps(str(path))}'
    r = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, check=False, timeout=5,
    )
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=f"trash failed: {r.stderr.strip()}")
    # Invalidate the recap cache for this file so the deleted entry vanishes promptly
    for k in list(_recap_cache.keys()):
        if k[0] == str(path):
            _recap_cache.pop(k, None)
    return {"action": "trashed", "path": str(path)}


@app.post("/api/focus/{session_id}")
async def api_focus(session_id: str):
    sessions = await asyncio.to_thread(list_sessions)
    target = next((s for s in sessions if s.session_id == session_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="session not found")

    if target.is_live:
        focused = False
        if target.live_term_program == "ghostty" and target.live_ghostty_term_id:
            if _ghostty_focus(target.live_ghostty_term_id):
                focused = True
        elif target.live_iterm_uuid or target.live_tty:
            if await _iterm_focus(target.live_iterm_uuid, target.live_tty):
                focused = True
        if focused:
            # If claude lives in a tmux pane, also switch tmux to it.
            if target.live_tmux_target:
                _tmux_select(target.live_tmux_target)
            return {
                "action": "focused",
                "term": target.live_term_program,
                "tmux": target.live_tmux_target,
            }

        # Focus failed. If claude is in a tmux session that's still alive,
        # we can recover by spawning a new terminal that re-attaches.
        if target.live_tmux_target:
            session = target.live_tmux_target.split(":", 1)[0]
            if _tmux_session_exists(session):
                cwd = target.project_path or os.path.expanduser("~")
                _reattach_tmux(target.live_tmux_target, cwd, target.live_term_program)
                return {
                    "action": "reattached",
                    "term": target.live_term_program,
                    "tmux": target.live_tmux_target,
                }

        # Live but truly unreachable — don't auto-resume (would create a
        # duplicate claude process). Return an error so the user can decide.
        raise HTTPException(
            status_code=409,
            detail=(
                "session is alive but its terminal is gone "
                f"(pid {target.live_pid}). Kill it via `kill {target.live_pid}` "
                "or attach to it manually."
            ),
        )

    if session_id.startswith("pid-"):
        raise HTTPException(status_code=409, detail="session has no jsonl yet — cannot resume")

    cwd = target.project_path or os.path.expanduser("~")
    if target.live_term_program == "ghostty":
        _ghostty_run_in_new_tab(cwd, f"claude --resume {shlex.quote(session_id)}")
        return {"action": "resumed", "term": "ghostty", "cwd": cwd}
    _iterm_new_tab(cwd, session_id)
    return {"action": "resumed", "term": "iterm", "cwd": cwd}


# ---------- SSE broadcast ----------

import uuid as _uuid

_BOOT_ID = _uuid.uuid4().hex
_subscribers: set[asyncio.Queue] = set()
_last_payload: Optional[str] = None

# ---------- settings (server-side, persisted) ----------

SETTINGS_PATH = Path.home() / ".cc-switchboard.json"
_settings: dict = {"notify_enabled": False}

try:
    _settings.update(json.loads(SETTINGS_PATH.read_text()))
except (OSError, json.JSONDecodeError):
    pass


def _save_settings() -> None:
    try:
        SETTINGS_PATH.write_text(json.dumps(_settings))
    except OSError as e:
        print(f"[settings] save failed: {e}")


# previous-activity tracker for notification transitions
_prev_activity: dict[str, str] = {}


def _fire_idle_notification_for(session) -> None:
    """Server-side: fire a terminal-notifier popup for a working→idle transition."""
    if not _TERMINAL_NOTIFIER:
        return
    sid = session.session_id
    project = session.project_path or session.project_dir
    short_project = project.replace(os.path.expanduser("~"), "~", 1) if project else ""
    body = (session.recap or "").strip()[:220] or "session is idle"
    cmd = [
        _TERMINAL_NOTIFIER,
        "-message", body,
        "-title", "Claude idle",
        "-subtitle", short_project,
        "-sound", "Glass",
        "-group", sid,
        "-execute",
        f"/usr/bin/curl -fsS -X POST http://127.0.0.1:8765/api/focus/{shlex.quote(sid)} >/dev/null",
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"[notify] failed: {e}")


def _state_fingerprint(sessions_payload: list[dict]) -> str:
    """Cheap hash of the session list — used to detect changes."""
    parts = []
    for s in sessions_payload:
        parts.append((
            s.get("session_id"), s.get("file_mtime"), s.get("recap"),
            s.get("activity"),
            s.get("is_live"), s.get("live_pid"),
            s.get("live_iterm_uuid"), s.get("live_ghostty_term_id"),
        ))
    return json.dumps(parts, sort_keys=True, default=str)


async def _broadcast_loop():
    """Snapshot every ~1s; push to subscribers on change. Also fires
    working→idle desktop notifications server-side (single source of truth)."""
    global _last_payload
    last_fp = ""
    while True:
        try:
            # Heavy: many subprocess.run calls. Run on a thread so the event
            # loop stays responsive to HTTP/SSE clients.
            sessions = await asyncio.to_thread(list_sessions)

            # Fire notifications for working → idle transitions.
            if _settings.get("notify_enabled"):
                for s in sessions:
                    prev = _prev_activity.get(s.session_id)
                    if prev == "working" and s.activity == "idle":
                        _fire_idle_notification_for(s)
            # Update tracker (always, regardless of notify flag, so toggling
            # on doesn't burst-fire stale transitions).
            seen_ids = set()
            for s in sessions:
                _prev_activity[s.session_id] = s.activity
                seen_ids.add(s.session_id)
            # Garbage-collect entries for sessions that disappeared.
            for sid in list(_prev_activity.keys()):
                if sid not in seen_ids:
                    _prev_activity.pop(sid, None)

            payload = {
                "boot": _BOOT_ID,
                "settings": dict(_settings),
                "sessions": [asdict(s) for s in sessions],
            }
            fp = _state_fingerprint(payload["sessions"])
            settings_fp = json.dumps(_settings, sort_keys=True)
            full_fp = f"{fp}|{settings_fp}"
            if full_fp != last_fp:
                last_fp = full_fp
                _last_payload = json.dumps(payload, default=str)
                dead = []
                for q in list(_subscribers):
                    try:
                        q.put_nowait(_last_payload)
                    except asyncio.QueueFull:
                        dead.append(q)
                for q in dead:
                    _subscribers.discard(q)
        except Exception as e:
            print(f"[broadcast] error: {e}")
        await asyncio.sleep(1.0)


@app.get("/api/settings")
async def api_get_settings():
    return _settings


@app.post("/api/settings")
async def api_set_settings(payload: dict):
    changed = False
    if "notify_enabled" in payload:
        new_val = bool(payload["notify_enabled"])
        if new_val != _settings.get("notify_enabled"):
            _settings["notify_enabled"] = new_val
            changed = True
    if changed:
        _save_settings()
    return _settings


@app.get("/api/events")
async def api_events():
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    _subscribers.add(q)

    async def gen():
        try:
            # Send current state immediately so the client has data without waiting
            if _last_payload is not None:
                yield f"data: {_last_payload}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    # heartbeat to keep proxies/clients alive
                    yield ": keepalive\n\n"
        finally:
            _subscribers.discard(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _pids_for_viewer() -> list[int]:
    """Running cc-switchboard server processes, excluding self.

    Matches the process's argv[0] (its actual executable path) — not any
    string in argv — so unrelated shells/editors that mention the name in
    their arguments don't get killed.
    """
    self_pid = os.getpid()
    r = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True, text=True, check=False,
    )
    pids: list[int] = []
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == self_pid:
            continue
        argv0 = parts[1].split(None, 1)[0]
        if "/uv/tools/cc-switchboard/" in argv0:
            pids.append(pid)
    return pids


def main():
    import argparse
    import signal

    parser = argparse.ArgumentParser(prog="cc-switchboard")
    parser.add_argument(
        "--stop", action="store_true",
        help="Kill any cc-switchboard process holding the port, then exit.",
    )
    parser.add_argument("--port", type=int, default=8765, help="Port to bind / signal (default 8765)")
    parser.add_argument(
        "--no-open", action="store_true",
        help="Don't auto-open the browser to the viewer URL on startup.",
    )
    parser.add_argument(
        "-d", "--background", action="store_true",
        help="Run detached in the background; logs to /tmp/cc-switchboard.log",
    )
    args = parser.parse_args()

    if args.stop:
        import time
        pids = _pids_for_viewer()
        if not pids:
            print("no server running")
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"sent SIGTERM to pid {pid}")
            except ProcessLookupError:
                print(f"pid {pid} already gone")
        # Escalate to SIGKILL for any survivor after uvicorn's graceful
        # shutdown window (2s) expires. SSE clients keep their connection
        # open indefinitely, so uvicorn always needs the full window.
        time.sleep(3.0)
        for pid in pids:
            try:
                os.kill(pid, 0)  # probe
            except ProcessLookupError:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                print(f"sent SIGKILL to pid {pid} (didn't exit on SIGTERM)")
            except ProcessLookupError:
                pass
        return

    if args.background:
        # Re-exec self without --background, fully detached, logging to /tmp.
        import sys
        new_argv = [a for a in sys.argv if a not in ("-d", "--background")]
        log_path = "/tmp/cc-switchboard.log"
        log_fp = open(log_path, "a")
        proc = subprocess.Popen(
            new_argv,
            stdout=log_fp, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from terminal session
        )
        print(f"started in background (pid {proc.pid}), logs: {log_path}")
        return

    # If a server is already up on this port, skip starting another one and
    # just open the browser to it (avoids "address already in use" + lets the
    # user re-trigger the open without restarting).
    already_running = False
    try:
        import socket
        with socket.create_connection(("127.0.0.1", args.port), timeout=0.3):
            already_running = True
    except OSError:
        pass

    if already_running:
        print(f"server already running on :{args.port}")
        if not args.no_open:
            subprocess.run(["open", f"http://127.0.0.1:{args.port}"], check=False)
        return

    if not args.no_open:
        # Open browser only after uvicorn has actually bound the port,
        # so the page doesn't flash "connection refused".
        import socket
        import threading
        import time

        def _wait_and_open():
            deadline = time.time() + 10.0
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", args.port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                return  # timed out, give up silently
            subprocess.run(["open", f"http://127.0.0.1:{args.port}"], check=False)

        threading.Thread(target=_wait_and_open, daemon=True).start()

    import uvicorn
    uvicorn.run(
        app, host="127.0.0.1", port=args.port,
        # Don't wait forever for SSE connections to drain on shutdown.
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()
