"""Server-side context wash: capture the tail, /clear, verify, paste the tail back.

WHAT MOVED, AND WHAT CAME BACK
  The wash was originally the watch's: capture the pane tail, send /clear, poll
  to verify, then PASTE THE TAIL BACK. Moving it bridge-side dropped that last
  step for three stated reasons. Two had expired and the third was never
  universal:
    (a) "round-trips pane content through a wrist device" — gone. This runs
        bridge-side; the text never leaves the pane it was captured from.
    (b) "lands as ONE space-joined line" — described send(), which collapses
        newlines BY DESIGN so dictated text cannot submit early. Not true of
        pasting: tmux.paste() uses load-buffer plus `paste-buffer -p`, whose
        bracketed-paste markers tell the TUI "this is one paste". Multi-line
        survives; verified against a real pane.
    (c) "a lossy imitation of a re-seed a slash-command does properly" — only
        where a slash-command applies. On a repo with no .foreman/cycle.json
        there is no "/brief" to be lossy against.

ONE LANE, ON PURPOSE (operator call, 2026-08-08)
  This briefly forked: configured slash-command where its probe passed, paste
  otherwise. That fork is GONE. The operator's reasoning, and it is the right
  one for a file like this: one lane of work is one lane of failure to test. A
  second re-seed path that only fires on repos carrying a marker file is a path
  that is exercised rarely, reviewed rarely, and trusted anyway — in code whose
  every branch ends in an Enter pressed into a live Claude session.

  So: always paste the tail. CLAWATCH_RESEED_COMMAND and CLAWATCH_RESEED_PROBE
  are no longer read and can be deleted from clawatch.env.

  The paste is NOT context recovery, and the preamble tells the model so in as
  many words. It is rendered screen output — a record of what happened, not
  restored memory. It beats an empty pane, which is the only comparison left.

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
  4. NEVER ENTER BLIND, in two checks. parse_prompt() is the safety-critical
     one: never press Enter while a permission menu is up, or the Enter answers
     the menu instead of submitting. It runs on the paste path too — a paste
     cannot open a menu, but it can land while one is already on screen. The
     second check is that the input went from empty to non-empty; without it a
     paste that silently failed to land would submit a BLANK prompt into a
     session that was just cleared.
     (The old second check compared the typed line against the exact command,
     guarding "/brief" submitting as "/batch-brief". It went with the command
     path — there is no fixed string to compare against now, and Claude Code
     collapses a large paste to a "[Pasted text #N]" placeholder anyway.)

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
        # ONE path: paste the pre-clear tail back. `raw` was captured at the top
        # of CLEAR, before anything was typed, so there is nothing left to
        # capture by this point -- by construction.
        _set(wash_id, stage="RESEED")
        if not settings.reseed_tail_enabled:
            _set(wash_id, reseed="none")
            events.emit("wash.reseeded", key, washId=wash_id, reseed="none",
                        probe="unconfigured", submitted=False, guard="tail_disabled")
            _finish(wash_id, "cleared_not_reseeded", ctx_after=_ctx_now(index))
            return

        tail = _reseed_tail_text(raw)
        guard, submitted = _paste_and_submit(index, tail, w)
        _set(wash_id, reseed="tail" if submitted else "none")
        events.emit("wash.reseeded", key, washId=wash_id,
                    reseed="tail" if submitted else "none", probe="unconfigured",
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


def _ctx_now(index: int) -> int | None:
    try:
        t = _thread_by_index(index)
    except tmux.TmuxError:
        return None
    return (t or {}).get("ctxTokens")


RESEED_TAIL_PREAMBLE = (
    "[context wash] The context above was cleared. What follows is the tail of "
    "this pane's rendered output from before the clear, pasted back for "
    "continuity. It is screen output, not a restored conversation -- treat it "
    "as a record of what happened, not as your own memory.\n\n"
)


def _reseed_tail_text(raw: list[str]) -> str:
    """Build the paste payload from the pre-clear capture. Bounded twice.

    The preamble is not decoration. Without it the model receives a wall of its
    own rendered output with no indication of what it is, and the failure mode
    is that it reads a transcript of finished work as work still in progress.
    Saying plainly what the text IS costs two lines and removes that.
    """
    lines = tmux._clean_tail(raw)
    if settings.reseed_tail_lines > 0:
        lines = lines[-settings.reseed_tail_lines:]
    body = "\n".join(lines).strip()
    if not body:
        return ""
    # Trim from the FRONT: the end of the tail is the most recent and the most
    # worth keeping.
    budget = settings.reseed_tail_max_chars - len(RESEED_TAIL_PREAMBLE)
    if budget > 0 and len(body) > budget:
        body = body[-budget:]
    elif budget <= 0:
        return ""
    return RESEED_TAIL_PREAMBLE + body


def _paste_and_submit(index: int, text: str, w: dict) -> tuple[str, bool]:
    """Guard 4 for the paste path. Returns (guard_code, submitted).

    The second check cannot be an exact-content match: Claude Code collapses a
    large paste to a placeholder like "[Pasted text #1 +N lines]", so the text
    is not on screen to compare against. The check that IS available -- and is
    the one that matters -- is that the input went from empty to non-empty. If
    the paste did not land, pressing Enter would submit an empty prompt into a
    freshly cleared session.
    """
    if not text:
        return "empty_tail", False

    if not _still_same_pane(w):
        return "completion_drift_aborted", False

    before = _input_line(tmux.capture(index, lines=40, scrollback=False, clean=False))

    tmux.paste(index, text)
    time.sleep(0.45)

    raw = tmux.capture(index, lines=40, scrollback=False, clean=False)

    # 4a — the safety-critical check, unchanged: never press Enter while a menu
    # is up. A paste cannot open a slash-command popup, but it can land while
    # one is already on screen.
    if tmux.parse_prompt(tmux._clean_tail(raw[-25:])):
        tmux.send_key(index, "clear")
        return "menu_present_aborted", False

    # Pane identity again: the paste is the slowest step in the wash, and the
    # Enter that follows is the irreversible one.
    if not _still_same_pane(w):
        tmux.send_key(index, "clear")
        return "completion_drift_aborted", False

    after = _input_line(raw)
    if not after or after == before:
        tmux.send_key(index, "clear")
        return "paste_not_rendered", False

    tmux.send_key(index, "enter")
    return "clean", True


def _input_line(lines: list[str]) -> str:
    """The bottom-most non-empty input line, stripped of prompt chrome.

    The prompt glyph set is the same one parse_status scans for; if Claude Code
    ever changes it, this returns "" and the paste check fails closed rather
    than silently passing.
    """
    for ln in reversed(lines):
        t = ln.strip().strip("│").strip()
        if not t:
            continue
        t = t.lstrip("❯>▸▶› ").strip()
        if not t:
            continue
        return t
    return ""


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
