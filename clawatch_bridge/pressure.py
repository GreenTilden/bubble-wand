"""Context-pressure levels, transition firing, and the ctx.sample throttle.

WHY OPERATOR THRESHOLDS AND NOT THE VENDOR'S ctxTier
  The vendor's status bar only prints a tier (HARD) near its OWN ~1M-context
  limit. Real panes here sit at 98k / 107k / 232k and carry no tier at all, so a
  tier-driven nudge would never fire on the sessions that actually need it. The
  watch's existing meter reddened at `tier == HARD` or 400k tokens — roughly 2.7x
  past the operator's own hard stop of 150k. It was not merely unwired; it was
  calibrated to a different policy. So pressure is computed here, from thresholds
  the operator sets.

UPWARD TRANSITIONS ONLY, WITH HYSTERESIS
  A thread parked at 121k against a 120k threshold would otherwise buzz on every
  poll, forever, which trains the operator to ignore the buzz — the exact failure
  mode this is meant to prevent. So a level fires ONCE on the way up and re-arms
  only after the thread falls back below `rearm_ratio x threshold` (default 0.8).

DEDUPE IS BRIDGE-SIDE, NOT CLIENT-SIDE
  Three client poll loops across two watches hit /api/threads. If firing were
  decided by the client, the same crossing would fire up to three times, and the
  event log — the thing the whole measurement rests on — would count one event as
  three. The bridge owns the transition; clients only render.

BASELINE SEEDING
  A thread first seen ABOVE a threshold starts DISARMED. Without this, every
  bridge restart would fire a burst of nudges for threads that had been sitting
  high for hours, and a restart is not an event. Set CLAWATCH_CTX_SEED_BASELINE=0
  to disable — which is exactly what the threshold test does, so that a scratch
  instance with tiny thresholds trips on the first poll instead of seeding itself
  into silence.

State is in-memory and per-process, deliberately. It is a debounce, not a record:
the durable facts go to the event log. A restart re-seeds baselines (see above),
which is the correct behaviour rather than a limitation.
"""
from __future__ import annotations

import threading
import time

from . import events
from .config import settings

NONE = "NONE"
SOFT = "SOFT"
HARD = "HARD"

_LOCK = threading.Lock()
# threadKey -> per-thread debounce state
_STATE: dict[str, dict] = {}


def level_for(tokens: int | None) -> str:
    """Map a context size to an operator pressure level."""
    if tokens is None:
        return NONE
    if tokens >= settings.ctx_hard_tokens:
        return HARD
    if tokens >= settings.ctx_soft_tokens:
        return SOFT
    return NONE


def _threshold(level: str) -> int:
    return settings.ctx_hard_tokens if level == HARD else settings.ctx_soft_tokens


def _rank(level: str) -> int:
    return {NONE: 0, SOFT: 1, HARD: 2}[level]


def _st(key: str) -> dict:
    s = _STATE.get(key)
    if s is None:
        s = {
            "armed": {SOFT: True, HARD: True},
            "armed_since": {SOFT: None, HARD: None},
            "seeded": False,
            "last_sample_ms": None,
            "last_tokens": None,
            "unparsed_streak": 0,
            "unparsed_since_ms": None,
        }
        _STATE[key] = s
    return s


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def observe(thread: dict) -> str:
    """Record one poll of one thread. Returns its pressure level.

    Emits ctx.sample (throttled), nudge.fired on an upward crossing, and
    nudge.rearmed when a thread drops back below the re-arm band.
    """
    key = events.thread_key(thread.get("paneId"))
    tokens = thread.get("ctxTokens")
    level = level_for(tokens)
    if key is None:
        # No durable identity (no paneId) — still report the level to the client,
        # but never fire or log: an event we cannot attribute is worse than none.
        return level

    with _LOCK:
        s = _st(key)
        now = _now_ms()
        first = not s["seeded"]

        if tokens is None:
            _note_unparsed(key, thread, s, now)
            return level
        _note_reparsed(key, thread, s, now)

        # ── seed on first sight ──────────────────────────────────────────────
        if first:
            s["seeded"] = True
            if settings.ctx_seed_baseline:
                # Disarm anything already breached, so a restart is silent.
                for lv in (SOFT, HARD):
                    if _rank(level) >= _rank(lv):
                        s["armed"][lv] = False
                        s["armed_since"][lv] = now

        # ── sample throttle ──────────────────────────────────────────────────
        # |delta| >= ctx_sample_delta OR ctx_sample_interval elapsed. That
        # continuous curve is the real prize: it turns a wash's before/after into
        # a JOIN over a series rather than a fragile two-point measurement, and it
        # is the only thing that makes durability measurable at all.
        last_ms = s["last_sample_ms"]
        last_tok = s["last_tokens"]
        due = (
            last_ms is None
            or (now - last_ms) >= settings.ctx_sample_interval * 1000
            or last_tok is None
            or abs(tokens - last_tok) >= settings.ctx_sample_delta
        )
        if due:
            events.emit(
                "ctx.sample", key,
                paneIndex=thread.get("index"), repo=thread.get("repo"),
                model=thread.get("model"), ctxTokens=tokens,
                ctxResolution=thread.get("ctxResolution"),
                ctxTier=_tier(thread.get("ctxTier")),
                pressure=level, status=thread.get("status"),
                sinceLastMs=(0 if last_ms is None else now - last_ms),
                baseline=first, forced=False,
            )
            s["last_sample_ms"] = now
            s["last_tokens"] = tokens

        # Return early ONLY when seeding actually disarmed us. Guarding this on
        # `first` alone was a bug: with CLAWATCH_CTX_SEED_BASELINE=0 the first poll
        # is supposed to be able to fire, and that is exactly the configuration the
        # low-threshold verification run depends on.
        if first and settings.ctx_seed_baseline:
            return level

        # ── re-arm on the way down (hysteresis) ──────────────────────────────
        for lv in (SOFT, HARD):
            if s["armed"][lv]:
                continue
            if tokens < _threshold(lv) * settings.ctx_rearm_ratio:
                since = s["armed_since"][lv]
                s["armed"][lv] = True
                s["armed_since"][lv] = None
                events.emit("nudge.rearmed", key, level=lv, ctxTokens=tokens,
                            armedForMs=(now - since) if since else 0)

        # ── fire on the way up ───────────────────────────────────────────────
        # Highest breached level only: crossing straight from NONE to HARD fires
        # one HARD, not a SOFT and a HARD.
        if level != NONE and s["armed"][level]:
            s["armed"][level] = False
            s["armed_since"][level] = now
            if level == HARD:
                # A HARD crossing subsumes SOFT — do not leave SOFT armed to fire
                # a second, lesser nudge on the next poll.
                s["armed"][SOFT] = False
                s["armed_since"][SOFT] = now
            events.emit(
                "nudge.fired", key,
                paneIndex=thread.get("index"), repo=thread.get("repo"),
                level=level, ctxTokens=tokens,
                ctxResolution=thread.get("ctxResolution"),
                status=thread.get("status"), model=thread.get("model"),
            )
        return level


def _tier(raw):
    if not raw:
        return None
    return "HARD" if str(raw).upper() == "HARD" else "OTHER"


def _note_unparsed(key, thread, s, now) -> None:
    """The monitor monitoring itself.

    Only for panes running a command we would ever wash — a bash pane legitimately
    has no status bar, and logging that would bury the real signal in noise.
    """
    cmd = (thread.get("command") or "").lower()
    if cmd not in settings.wash_pane_commands:
        return
    s["unparsed_streak"] += 1
    if s["unparsed_streak"] == 1:
        s["unparsed_since_ms"] = now
    # Fire on the opening edge, then at most once per sample interval while it
    # persists — otherwise a permanent format drift would write a line per poll.
    interval_ms = settings.ctx_sample_interval * 1000
    if s["unparsed_streak"] == 1 or (now - (s["last_sample_ms"] or 0)) >= interval_ms:
        events.emit(
            "ctx.unparsed", key,
            paneIndex=thread.get("index"), repo=thread.get("repo"),
            command=cmd, paneLines=thread.get("_paneLines") or 0,
            consecutive=s["unparsed_streak"],
        )
        s["last_sample_ms"] = now


def _note_reparsed(key, thread, s, now) -> None:
    if s["unparsed_streak"] <= 0:
        return
    since = s["unparsed_since_ms"]
    events.emit("ctx.reparsed", key, paneIndex=thread.get("index"),
                outageMs=(now - since) if since else 0,
                consecutive=s["unparsed_streak"])
    s["unparsed_streak"] = 0
    s["unparsed_since_ms"] = None


def sample_forced(thread: dict, *, pressure: str | None = None) -> None:
    """An unthrottled sample, taken around a wash so before/after always exist."""
    key = events.thread_key(thread.get("paneId"))
    if key is None or thread.get("ctxTokens") is None:
        return
    with _LOCK:
        s = _st(key)
        now = _now_ms()
        last_ms = s["last_sample_ms"]
        events.emit(
            "ctx.sample", key,
            paneIndex=thread.get("index"), repo=thread.get("repo"),
            model=thread.get("model"), ctxTokens=thread.get("ctxTokens"),
            ctxResolution=thread.get("ctxResolution"),
            ctxTier=_tier(thread.get("ctxTier")),
            pressure=pressure or level_for(thread.get("ctxTokens")),
            status=thread.get("status"),
            sinceLastMs=(0 if last_ms is None else now - last_ms),
            baseline=False, forced=True,
        )
        s["last_sample_ms"] = now
        s["last_tokens"] = thread.get("ctxTokens")


def reset_for_tests() -> None:
    with _LOCK:
        _STATE.clear()
