"""Server-side context wash: /clear, verify, then re-seed with a configured command.

WHAT MOVED AND WHY
  The wash used to be orchestrated entirely by the watch: capture the pane tail,
  send /clear, poll to verify, then PASTE THE TAIL BACK. That last step is the
  reason this moved. Re-pasting the tail
    (a) round-trips full pane content through a wrist device and back,
    (b) lands as ONE space-joined line, because /send collapses newlines, and
    (c) is a lossy imitation of a re-seed that a slash-command does properly.
  It is DELETED here with no fallback. The re-seed is a configured command
  (the operator's env sets "/brief"); when no command is configured, the wash
  stops after a verified clear and records reseed:"none" — which is a FACT the
  collector can count, not a silent degradation.

  Building it bridge-side is also what makes later automation a config flip
  instead of a rewrite: the watch cannot drive an auto-wash, because its polling
  loop suspends whenever the screen goes ambient and the watch is off-wrist half
  the day.

THIS FILE TYPES INTO A LIVE CLAUDE SESSION. THE GUARDS ARE THE FEATURE.
  1. PANE-COMMAND ALLOWLIST. If a pane has dropped to a shell, "/clear" is not a
     Claude command — it is a bash command that does not exist, typed into the
     operator's shell. Allowlist, never denylist.
  2. PANE IDENTITY RE-CHECK. Indices are recomputed every poll and panes have been
     destroyed and recreated. Every stage re-verifies that the index still maps to
     the paneId the wash started on; if it moved, abort rather than type into
     whatever now occupies that slot.
  3. VERIFY BEFORE RE-SEED. If the sentinel survives, the pane did NOT clear, and
     we abort WITHOUT re-seeding. Typing into a live, uncleared context is the
     worse failure.
  4. THE AUTOCOMPLETE GUARD, and it is TWO checks rather than one. The spec said
     "re-run parse_prompt() before the Enter that submits /brief", but
     parse_prompt() CANNOT see a slash-command popup: it returns None unless it
     finds a cursored, NUMBERED option, and Claude Code's "/br…" completion list
     is not numbered. So parse_prompt() stays — it is the safety-critical one, and
     it means "never press Enter while a permission menu is up" — and a second
     render check confirms the typed line is EXACTLY the command before Enter.
     Without it, "/brief" can submit as "/batch-brief" or "/pre-brief", both of
     which share its prefix and both of which do something else.

THE SENTINEL IS NEVER LOGGED. It is computed from pane content, used to verify,
and discarded in memory. Nothing derived from pane text reaches the event log —
see events.py, where there is no field it could go in.
"""
from __future__ import annotations

import threading
import time
import uuid

from . import events, pressure, tmux
from .config import settings

# washId -> {stage, outcome, startedMs, index, paneId, ...}
_WASHES: dict[str, dict] = {}
_INFLIGHT: dict[int, str] = {}       # paneIndex -> washId, one wash per pane
_LOCK = threading.Lock()
MAX_INFLIGHT = 4
_KEEP_RECORDS = 32

STAGES = ("QUEUED", "CLEAR", "VERIFY", "RESEED", "DONE")


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _thread_by_index(index: int) -> dict | None:
    for t in tmux.list_threads():
        if t.get("index") == index:
            return t
    return None


def _sentinel(lines: list[str]) -> str | None:
    """The longest substantial trimmed line, used only to prove the pane cleared.

    Held in memory for the life of one wash and never logged, never returned over
    HTTP, never written anywhere.
    """
    best = None
    for ln in lines:
        t = ln.strip()
        if len(t) >= 12 and (best is None or len(t) > len(best)):
            best = t
    return best


def request_wash(index: int, trigger: str = "manual") -> tuple[str | None, str | None]:
    """Validate and enqueue a wash. Returns (washId, blocked_reason).

    Every rejection is recorded as wash.guard_blocked, so a wash that never ran is
    still a countable fact rather than a gap in the series.
    """
    wash_id = uuid.uuid4().hex[:16]

    if not settings.wash_enabled:
        events.emit("wash.guard_blocked", None, washId=wash_id, paneIndex=index,
                    reason="wash_disabled")
        return None, "wash_disabled"

    try:
        thread = _thread_by_index(index)
    except tmux.TmuxError:
        thread = None
    if thread is None:
        events.emit("wash.guard_blocked", None, washId=wash_id, paneIndex=index,
                    reason="pane_missing")
        return None, "pane_missing"

    key = events.thread_key(thread.get("paneId"))
    cmd = (thread.get("command") or "").lower()
    if cmd not in settings.wash_pane_commands:
        # Guard 1. This is the one that stops "/clear" being typed into a shell.
        events.emit("wash.guard_blocked", key, washId=wash_id, paneIndex=index,
                    reason="not_claude_pane")
        return None, "not_claude_pane"

    with _LOCK:
        if index in _INFLIGHT:
            events.emit("wash.guard_blocked", key, washId=wash_id, paneIndex=index,
                        reason="wash_in_flight")
            return None, "wash_in_flight"
        if len(_INFLIGHT) >= MAX_INFLIGHT:
            events.emit("wash.guard_blocked", key, washId=wash_id, paneIndex=index,
                        reason="too_many_inflight")
            return None, "too_many_inflight"
        _INFLIGHT[index] = wash_id
        _WASHES[wash_id] = {
            "washId": wash_id, "index": index, "paneId": thread.get("paneId"),
            "threadKey": key, "stage": "QUEUED", "outcome": None,
            "startedMs": _now_ms(), "trigger": trigger,
            "ctxBefore": thread.get("ctxTokens"),
            "ctxResolution": thread.get("ctxResolution"),
            "reseed": None, "error": None,
        }
        _prune_locked()

    events.emit(
        "wash.requested", key, washId=wash_id, paneIndex=index,
        repo=thread.get("repo"), trigger=trigger,
        pressureAtRequest=pressure.level_for(thread.get("ctxTokens")),
        ctxBefore=thread.get("ctxTokens"),
        ctxResolution=thread.get("ctxResolution"),
        status=thread.get("status"),
    )
    pressure.sample_forced(thread)

    t = threading.Thread(target=_run_wash, args=(wash_id,), daemon=True,
                         name=f"wash-{wash_id[:6]}")
    t.start()
    return wash_id, None


def status(wash_id: str) -> dict | None:
    with _LOCK:
        w = _WASHES.get(wash_id)
        return dict(w) if w else None


def _prune_locked() -> None:
    if len(_WASHES) <= _KEEP_RECORDS:
        return
    done = [k for k, v in _WASHES.items() if v["stage"] == "DONE"]
    done.sort(key=lambda k: _WASHES[k]["startedMs"])
    for k in done[: max(0, len(_WASHES) - _KEEP_RECORDS)]:
        _WASHES.pop(k, None)


def _set(wash_id: str, **kw) -> None:
    with _LOCK:
        w = _WASHES.get(wash_id)
        if w:
            w.update(kw)


def _still_same_pane(w: dict) -> bool:
    """Guard 2. An index is not an identity — re-verify before every keystroke."""
    try:
        t = _thread_by_index(w["index"])
    except tmux.TmuxError:
        return False
    return bool(t and t.get("paneId") == w["paneId"])


def _finish(wash_id: str, outcome: str, ctx_after=None) -> None:
    with _LOCK:
        w = _WASHES.get(wash_id, {})
        w["stage"] = "DONE"
        w["outcome"] = outcome
        idx = w.get("index")
        if idx is not None and _INFLIGHT.get(idx) == wash_id:
            _INFLIGHT.pop(idx, None)
        snap = dict(w)
    events.emit(
        "wash.completed", snap.get("threadKey"), washId=wash_id, outcome=outcome,
        ctxBefore=snap.get("ctxBefore"), ctxAfter=ctx_after,
        ctxResolution=snap.get("ctxResolution"),
        durationMs=_now_ms() - snap.get("startedMs", _now_ms()),
        reseed=snap.get("reseed"), trigger=snap.get("trigger"),
    )


def _run_wash(wash_id: str) -> None:
    w = status(wash_id)
    if w is None:
        return
    key, index = w["threadKey"], w["index"]
    t0 = _now_ms()

    try:
        # ── CLEAR ────────────────────────────────────────────────────────────
        _set(wash_id, stage="CLEAR")
        raw = tmux.capture(index, lines=200, scrollback=False, clean=False)
        sentinel = _sentinel(raw)

        cleared = False
        attempts = 0
        for attempt in range(1, settings.wash_clear_attempts + 1):
            attempts = attempt
            if not _still_same_pane(w):
                events.emit("wash.clear_failed", key, washId=wash_id,
                            attempts=attempts, elapsedMs=_now_ms() - t0,
                            reason="pane_changed")
                _finish(wash_id, "blocked")
                return
            tmux.send_key(index, "escape")     # stop any in-flight response
            time.sleep(0.3)
            tmux.send_key(index, "clear")      # C-u, wipe a half-typed draft
            time.sleep(0.15)
            tmux.send(index, "/clear", submit=True)
            time.sleep(0.8 if attempt == 1 else 1.5)
            after = tmux.capture(index, lines=200, scrollback=False, clean=False)
            if sentinel is None:
                # Nothing substantial to check against. Treat as cleared but SAY
                # SO in the record — "we could not verify" must never silently
                # become "verified".
                events.emit("wash.clear_failed", key, washId=wash_id,
                            attempts=attempts, elapsedMs=_now_ms() - t0,
                            reason="no_sentinel")
                cleared = True
                break
            if not any(sentinel in ln for ln in after):
                cleared = True
                break

        if not cleared:
            # Guard 3. Do NOT re-seed into a live, uncleared context.
            events.emit("wash.clear_failed", key, washId=wash_id, attempts=attempts,
                        elapsedMs=_now_ms() - t0, reason="sentinel_survived")
            _finish(wash_id, "failed")
            return

        events.emit("wash.cleared", key, washId=wash_id, attempts=attempts,
                    elapsedMs=_now_ms() - t0)

        # ── RESEED ───────────────────────────────────────────────────────────
        _set(wash_id, stage="RESEED")
        cmd = settings.reseed_command
        if not cmd:
            _set(wash_id, reseed="none")
            events.emit("wash.reseeded", key, washId=wash_id, reseed="none",
                        probe="unconfigured", submitted=False, guard="clean")
            _finish(wash_id, "cleared_not_reseeded", ctx_after=_ctx_now(index))
            return

        probe = _probe_state(index)
        if probe == "absent":
            # The re-seed command exists but this repo cannot satisfy it (e.g. no
            # .foreman/cycle.json). Recording reseed:"none" with probe:"absent" is
            # the countable negative case — duckminster produces it naturally.
            _set(wash_id, reseed="none")
            events.emit("wash.reseeded", key, washId=wash_id, reseed="none",
                        probe="absent", submitted=False, guard="clean")
            _finish(wash_id, "cleared_not_reseeded", ctx_after=_ctx_now(index))
            return

        guard, submitted = _type_and_submit(index, cmd, w)
        _set(wash_id, reseed="command" if submitted else "none")
        events.emit("wash.reseeded", key, washId=wash_id,
                    reseed="command" if submitted else "none", probe=probe,
                    submitted=submitted, guard=guard)
        _finish(wash_id, "ok" if submitted else "cleared_not_reseeded",
                ctx_after=_ctx_now(index))

    except tmux.TmuxError:
        # str(e) carries proc.stderr — arbitrary subprocess text. It is DISCARDED
        # here and mapped to an enum; events.py has no field it could go in.
        events.emit("wash.failed", key, washId=wash_id, reason="tmux_error",
                    stage=(status(wash_id) or {}).get("stage", "CLEAR"),
                    durationMs=_now_ms() - t0)
        _finish(wash_id, "failed")
    except Exception:  # noqa: BLE001
        events.emit("wash.failed", key, washId=wash_id, reason="internal",
                    stage=(status(wash_id) or {}).get("stage", "CLEAR"),
                    durationMs=_now_ms() - t0)
        _finish(wash_id, "failed")


def _probe_state(index: int) -> str:
    """Does this pane's repo satisfy the configured re-seed probe?

    The probe is a repo-relative path (the operator sets ".foreman/cycle.json").
    Resolved against the pane's cwd via tmux, so no path from a client is involved.
    """
    if not settings.reseed_probe:
        return "unconfigured"
    try:
        out = tmux._run(
            ["display-message", "-p", "-t", f"{settings.tmux_window}.{index}",
             "#{pane_current_path}"]
        ).strip()
    except tmux.TmuxError:
        return "absent"
    if not out:
        return "absent"
    import os
    return "present" if os.path.exists(os.path.join(out, settings.reseed_probe)) else "absent"


def _ctx_now(index: int) -> int | None:
    try:
        t = _thread_by_index(index)
    except tmux.TmuxError:
        return None
    return (t or {}).get("ctxTokens")


def _type_and_submit(index: int, cmd: str, w: dict) -> tuple[str, bool]:
    """Guard 4, in two parts. Returns (guard_code, submitted)."""
    if not cmd.startswith("/") or len(cmd) > 64 or any(c.isspace() for c in cmd):
        # A re-seed command is a bare slash-command. Anything else is a config
        # error, and typing it blind would be the worst possible response.
        return "invalid_command", False

    if not _still_same_pane(w):
        return "completion_drift_aborted", False

    # Type WITHOUT submitting.
    tmux.send(index, cmd, submit=False)
    time.sleep(0.45)

    raw = tmux.capture(index, lines=40, scrollback=False, clean=False)

    # 4a — the safety-critical check: never press Enter while a menu is up.
    if tmux.parse_prompt(tmux._clean_tail(raw[-25:])):
        tmux.send_key(index, "clear")   # withdraw what we typed
        return "menu_present_aborted", False

    # 4b — the completion check parse_prompt structurally cannot do. The typed
    # line must END with exactly the command: an autocomplete popup that has
    # advanced "/brief" toward "/batch-brief" leaves a different trailing token.
    if not _typed_line_is_exact(raw, cmd):
        tmux.send_key(index, "clear")
        return "completion_drift_aborted", False

    tmux.send_key(index, "enter")
    return "clean", True


def _typed_line_is_exact(lines: list[str], cmd: str) -> bool:
    """True when the bottom-most non-empty input line ends with exactly `cmd`.

    Claude Code renders the input line with a leading prompt glyph and possibly a
    box border, so the check is on the trailing token rather than the whole line.
    """
    for ln in reversed(lines):
        t = ln.strip().strip("│").strip()
        if not t:
            continue
        t = t.lstrip("❯>▸▶› ").strip()
        if not t:
            continue
        return t.split()[-1] == cmd if t.split() else False
    return False


def maybe_autowash(threads: list[dict]) -> None:
    """The automation seam. SHIPS DISABLED — one `if`, one env var.

    Gated on IDLE + hard pressure deliberately: washing a WORKING pane would
    interrupt a running response, and washing one at NEEDS_INPUT would answer a
    question with /clear.
    """
    if not settings.autowash_enabled:
        return
    for t in threads:
        if t.get("status") != "IDLE":
            continue
        if pressure.level_for(t.get("ctxTokens")) != pressure.HARD:
            continue
        idx = t.get("index")
        with _LOCK:
            if idx in _INFLIGHT:
                continue
        request_wash(idx, trigger="auto")


def reset_for_tests() -> None:
    with _LOCK:
        _WASHES.clear()
        _INFLIGHT.clear()
