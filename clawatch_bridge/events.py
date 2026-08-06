"""Append-only event log for context pressure, nudges, and washes.

THE PRIVACY INVARIANT IS THIS MODULE'S ENTIRE REASON FOR EXISTING.

  This log records METRICS AND PANE IDENTITY ONLY, NEVER PANE CONTENT.

  That is not a rule someone has to remember — it is enforced by the shape of the
  API. `emit()` accepts exactly six value types and THERE IS NO STRING TYPE AMONG
  THEM. A developer who wants to log a message must first add a new type
  constructor to this file, which is a visible, reviewable diff in the one file
  whose whole docstring is the invariant. They cannot do it by adding a kwarg at a
  call site, and that difference is the point.

  Consequences worth stating explicitly, because each one closed a real hole:
    * `reason` is a CLOSED ENUM everywhere it appears. TmuxError carries
      `proc.stderr` — arbitrary text from a subprocess — so every `except
      TmuxError` in wash.py maps to an enum code and DISCARDS `str(e)`. A single
      careless `reason=str(e)` would otherwise have put terminal output on disk.
    * `model` is SLUGIFIED, not validated. parse_status derives it from an
      unbounded slice of a rendered line (`t.split("·", 1)[0]`). If the bar parser
      ever mis-picks a conversation line, the worst thing that reaches disk is 40
      characters of [a-z0-9-]. Validation would reject and lose the sample;
      slugification bounds the blast radius instead.
    * `ctxTier` is ENUM(HARD, OTHER), never the raw vendor word.
    * The envelope (v, ts, event, threadKey) is SERVER-GENERATED. Callers cannot
      pass it.
    * Unknown keys are dropped, so a typo'd kwarg cannot smuggle a payload.
    * A 512-byte line cap, as belt and braces.
    * costUsd and spendTokens are excluded from EVERY event. Nothing downstream
      needs them, and their absence means this file contains no dollar figure at
      all — which is what keeps it out of the Darren-only spend class.

  threadKey = host:tmuxServerPid:paneId. The Claude Code session UUID was
  REJECTED as an identifier: it IS the transcript filename, so using it would put
  this module one `head -c` away from persisting conversation content. Net effect
  of this whole increment: it REDUCES content exposure, because the wash it
  replaces round-tripped full pane text through a watch.

NO `GET /api/events`. Nothing in main.py reads this file. The downstream collector
reads it from local disk as the owning user. Adding a route would put a log of
every thread's context curve on an internet-reachable surface to save a `cat`.
A test asserts no route path contains "event".

Default path: ${XDG_STATE_HOME:-~/.local/state}/bubble-wand/wash-events.jsonl, 0600.
Deliberately NOT /var/log: this bridge is an unprivileged `systemd --user` unit,
and a fleet path cannot be a committed default in a repo meant to be cloned.

Dependency-free (stdlib only) — config.py's "the only install is fastapi +
uvicorn" stays true.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import sys
import threading
from datetime import datetime, timezone

from .config import settings

SCHEMA_VERSION = 1

# ── the six value types. There is deliberately no TEXT/STR among them. ────────
INT = "int"
FLOAT = "float"
BOOL = "bool"
IDENT = "ident"        # threadKey, washId — bounded charset, no whitespace
BASENAME = "basename"  # a path basename; rejects anything containing "/"
MODEL = "model"        # slugified and truncated, never passed through

_IDENT_RE = re.compile(r"^[A-Za-z0-9._:%-]{1,64}$")
_BASENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

MAX_LINE_BYTES = 512


class _Enum:
    __slots__ = ("allowed",)

    def __init__(self, *allowed):
        self.allowed = frozenset(allowed)


def ENUM(*allowed):
    return _Enum(*allowed)


_STATUS = ENUM("NEEDS_INPUT", "WORKING", "IDLE")
_PRESSURE = ENUM("NONE", "SOFT", "HARD")
_LEVEL = ENUM("SOFT", "HARD")
_TIER = ENUM("HARD", "OTHER")
_TRIGGER = ENUM("manual", "auto")

# ── the schema. Every field maps to a type; nothing else is accepted. ────────
SCHEMA: dict[str, dict[str, object]] = {
    "ctx.sample": {
        "paneIndex": INT, "repo": BASENAME, "model": MODEL,
        "ctxTokens": INT, "ctxResolution": INT, "ctxTier": _TIER,
        "pressure": _PRESSURE, "status": _STATUS,
        "sinceLastMs": INT, "baseline": BOOL, "forced": BOOL,
    },
    # Monitors the monitor. Without this, vendor format drift would silently
    # disable every nudge forever and nothing anywhere would say so.
    "ctx.unparsed": {
        "paneIndex": INT, "repo": BASENAME, "command": MODEL,
        "paneLines": INT, "consecutive": INT,
    },
    # The closing edge. Without it an outage's DURATION is unmeasurable and
    # ctx.unparsed can only ever say "it broke", never "it broke for 40 minutes".
    "ctx.reparsed": {
        "paneIndex": INT, "outageMs": INT, "consecutive": INT,
    },
    # The bridge's DECISION, recorded whether or not any watch was listening.
    "nudge.fired": {
        "paneIndex": INT, "repo": BASENAME, "level": _LEVEL,
        "ctxTokens": INT, "ctxResolution": INT, "status": _STATUS, "model": MODEL,
    },
    "nudge.rearmed": {
        "paneIndex": INT, "level": _LEVEL, "ctxTokens": INT, "armedForMs": INT,
    },
    "wash.requested": {
        "washId": IDENT, "paneIndex": INT, "repo": BASENAME, "trigger": _TRIGGER,
        "pressureAtRequest": _PRESSURE, "ctxBefore": INT, "ctxResolution": INT,
        "status": _STATUS,
    },
    "wash.guard_blocked": {
        "washId": IDENT, "paneIndex": INT,
        "reason": ENUM("not_claude_pane", "pane_missing", "pane_changed",
                       "wash_in_flight", "wash_disabled", "too_many_inflight",
                       "index_invalid"),
    },
    "wash.cleared": {"washId": IDENT, "attempts": INT, "elapsedMs": INT},
    "wash.clear_failed": {
        "washId": IDENT, "attempts": INT, "elapsedMs": INT,
        "reason": ENUM("sentinel_survived", "no_sentinel", "tmux_error", "pane_changed"),
    },
    "wash.reseeded": {
        "washId": IDENT,
        "reseed": ENUM("command", "none"),
        "probe": ENUM("present", "absent", "unconfigured"),
        "submitted": BOOL,
        "guard": ENUM("clean", "menu_present_aborted", "completion_drift_aborted",
                      "invalid_command"),
    },
    "wash.completed": {
        "washId": IDENT,
        "outcome": ENUM("ok", "cleared_not_reseeded", "failed", "blocked"),
        "ctxBefore": INT, "ctxAfter": INT, "ctxResolution": INT,
        "durationMs": INT, "reseed": ENUM("command", "none"), "trigger": _TRIGGER,
    },
    "wash.failed": {
        "washId": IDENT,
        "reason": ENUM("tmux_error", "timeout", "pane_changed", "internal"),
        "stage": ENUM("QUEUED", "CLEAR", "VERIFY", "RESEED"),
        "durationMs": INT,
    },
    # Emitted when a line is rejected for size. Carries a byte count, nothing else.
    "evt.oversize": {"bytes": INT},
}

# EXPLICITLY REFUSED: `nudge.delivered`. The watch PULLS; the bridge cannot observe
# delivery. Inventing it would fabricate the denominator of the downstream
# nudgeToBathConversion metric. That metric is instead the join
#   nudge.fired -> the next wash.requested on the same threadKey within a window
# — recorded here so the collector does not have to guess the intended join.

# NEVER PRESENT IN ANY SCHEMA, and each absence is deliberate: the wash sentinel,
# any captured line, cwd, pane title, pane label, an exception message, a Claude
# Code session UUID, a dollar figure.

_FD: int | None = None
_LOCK = threading.Lock()
_WARNED = False


def _warn_once(msg: str) -> None:
    global _WARNED
    if not _WARNED:
        _WARNED = True
        print(f"[events] disabled after error: {msg}", file=sys.stderr)


def _slug(v) -> str | None:
    s = _SLUG_RE.sub("-", str(v).strip().lower()).strip("-")
    return s[:40] or None


def _coerce(kind, v):
    """Return the coerced value, or raise ValueError. None passes through."""
    if v is None:
        return None
    if isinstance(kind, _Enum):
        s = str(v)
        if s not in kind.allowed:
            raise ValueError(f"not in enum: {s!r}")
        return s
    if kind is INT:
        if isinstance(v, bool):
            raise ValueError("bool is not an int here")
        return int(v)
    if kind is FLOAT:
        return float(v)
    if kind is BOOL:
        return bool(v)
    if kind is IDENT:
        s = str(v)
        if not _IDENT_RE.match(s):
            raise ValueError(f"bad ident: {s!r}")
        return s
    if kind is BASENAME:
        s = str(v)
        if not _BASENAME_RE.match(s):
            raise ValueError(f"bad basename: {s!r}")
        return s
    if kind is MODEL:
        return _slug(v)
    raise ValueError(f"unknown kind {kind!r}")


def thread_key(pane_id: str | None) -> str | None:
    """host:tmuxServerPid:paneId — stable across a pane's whole life."""
    if not pane_id:
        return None
    from . import tmux  # local import: avoids a cycle at module load

    pid = tmux.server_pid()
    host = socket.gethostname().split(".")[0]
    key = f"{host}:{pid if pid is not None else 'x'}:{pane_id.lstrip('%')}"
    return key if _IDENT_RE.match(key) else None


def _path() -> str:
    return settings.event_log


def _fd() -> int:
    global _FD
    if _FD is None:
        p = _path()
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        # Unconditional: repairs a pre-existing wide mode rather than trusting the
        # creation flags of whoever made the file first.
        os.fchmod(fd, 0o600)
        _FD = fd
    return _FD


def _rotate_if_needed() -> None:
    global _FD
    if _FD is None:
        return
    try:
        if os.fstat(_FD).st_size < settings.event_max_bytes:
            return
    except OSError:
        return
    p = _path()
    try:
        # flock only around the rename sequence, so a second bridge process (the
        # scratch test instance) cannot rename the file out from under this one.
        fcntl.flock(_FD, fcntl.LOCK_EX)
        try:
            if os.fstat(_FD).st_size < settings.event_max_bytes:
                return
            for n in range(settings.event_keep, 0, -1):
                src = p if n == 1 else f"{p}.{n - 1}"
                dst = f"{p}.{n}"
                if os.path.exists(src):
                    os.replace(src, dst)
        finally:
            fcntl.flock(_FD, fcntl.LOCK_UN)
    except OSError as e:
        _warn_once(f"rotate failed: {e}")
        return
    try:
        os.close(_FD)
    except OSError:
        pass
    _FD = None


def emit(event: str, thread_key_: str | None = None, **fields) -> None:
    """Append one event. Fail-soft: telemetry must never break a poll or a wash.

    Under CLAWATCH_EVENT_STRICT=1 it raises instead of dropping — that mode exists
    for the tests, so a schema violation is a loud test failure rather than a
    silently missing line.
    """
    strict = settings.event_strict
    try:
        spec = SCHEMA.get(event)
        if spec is None:
            raise ValueError(f"unknown event {event!r}")

        row = {
            "v": SCHEMA_VERSION,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
        }
        if thread_key_ is not None:
            row["threadKey"] = _coerce(IDENT, thread_key_)

        for k, v in fields.items():
            kind = spec.get(k)
            if kind is None:
                # A typo'd kwarg cannot smuggle a payload.
                if strict:
                    raise ValueError(f"unknown field {k!r} for {event!r}")
                continue
            try:
                row[k] = _coerce(kind, v)
            except ValueError:
                if strict:
                    raise
                row[k] = None

        line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        b = line.encode()
        if len(b) > MAX_LINE_BYTES:
            if strict:
                raise ValueError(f"line too large: {len(b)}B")
            emit("evt.oversize", None, bytes=len(b))
            return

        with _LOCK:
            _rotate_if_needed()
            fd = _fd()
            # O_APPEND + a single write of a sub-PIPE_BUF line is atomic on a local
            # filesystem, so concurrent writers cannot interleave a partial line.
            os.write(fd, b)
    except Exception as e:  # noqa: BLE001
        if strict:
            raise
        _warn_once(str(e))


def reset_for_tests() -> None:
    """Drop the cached fd so a test can point CLAWATCH_EVENT_LOG somewhere new."""
    global _FD, _WARNED
    with _LOCK:
        if _FD is not None:
            try:
                os.close(_FD)
            except OSError:
                pass
        _FD = None
        _WARNED = False
