"""Pressure levels, hysteresis, and the baseline seed.

The behaviour under test is a debounce, and debounces fail in exactly two ways:
firing too often (which trains the operator to ignore the signal) and not firing
at all (which is indistinguishable from a quiet week). Both are pinned here.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "wash-events.jsonl"
    from clawatch_bridge.config import settings
    monkeypatch.setattr(settings, "event_log", str(path), raising=False)
    monkeypatch.setattr(settings, "event_strict", True, raising=False)
    monkeypatch.setattr(settings, "ctx_soft_tokens", 120000, raising=False)
    monkeypatch.setattr(settings, "ctx_hard_tokens", 150000, raising=False)
    monkeypatch.setattr(settings, "ctx_rearm_ratio", 0.8, raising=False)
    monkeypatch.setattr(settings, "ctx_seed_baseline", True, raising=False)
    monkeypatch.setattr(settings, "ctx_sample_delta", 5000, raising=False)
    monkeypatch.setattr(settings, "ctx_sample_interval", 300, raising=False)
    monkeypatch.setattr(settings, "wash_pane_commands", ("claude",), raising=False)
    from clawatch_bridge import events, pressure, tmux
    monkeypatch.setattr(tmux, "server_pid", lambda: 4242)
    events.reset_for_tests()
    pressure.reset_for_tests()
    yield path
    events.reset_for_tests()
    pressure.reset_for_tests()


def rows(path, event=None):
    if not path.exists():
        return []
    out = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return [r for r in out if event is None or r["event"] == event]


def th(tokens, *, idx=1, pane="%7", status="IDLE", command="claude"):
    return {"index": idx, "paneId": pane, "repo": "demo-repo", "model": "opus",
            "ctxTokens": tokens, "ctxResolution": 1000, "ctxTier": None,
            "status": status, "command": command}


def test_levels(env):
    from clawatch_bridge import pressure
    assert pressure.level_for(None) == "NONE"
    assert pressure.level_for(119_999) == "NONE"
    assert pressure.level_for(120_000) == "SOFT"
    assert pressure.level_for(149_999) == "SOFT"
    assert pressure.level_for(150_000) == "HARD"


def test_a_parked_thread_buzzes_once_not_forever(env):
    """The whole point of the hysteresis: 121k against a 120k threshold must not
    fire on every poll.

    Seeded low first, deliberately — a thread FIRST SEEN at 121k is disarmed by
    baseline seeding and would never fire at all, which is a different (also
    correct) behaviour covered by test_baseline_seeding_keeps_a_restart_silent.
    """
    from clawatch_bridge import pressure
    pressure.observe(th(100_000))
    for _ in range(10):
        pressure.observe(th(121_000))
    assert len(rows(env, "nudge.fired")) == 1


def test_it_rearms_only_below_the_band_then_fires_again(env):
    from clawatch_bridge import pressure
    pressure.observe(th(100_000))     # seed low, armed
    pressure.observe(th(121_000))     # fire SOFT
    assert len(rows(env, "nudge.fired")) == 1
    pressure.observe(th(110_000))     # above 0.8 * 120k = 96k -> still disarmed
    assert not rows(env, "nudge.rearmed")
    pressure.observe(th(121_000))
    assert len(rows(env, "nudge.fired")) == 1
    pressure.observe(th(90_000))      # below the re-arm band
    assert len(rows(env, "nudge.rearmed")) == 1
    pressure.observe(th(121_000))
    assert len(rows(env, "nudge.fired")) == 2


def test_baseline_seeding_keeps_a_restart_silent(env):
    """A bridge restart is not an event. Without this, every restart fires a burst
    for threads that had been sitting high for hours."""
    from clawatch_bridge import pressure
    pressure.observe(th(200_000))     # first sight, already breached
    assert rows(env, "nudge.fired") == []
    s = rows(env, "ctx.sample")
    assert len(s) == 1 and s[0]["baseline"] is True


def test_seed_baseline_off_trips_on_the_first_poll(env, monkeypatch):
    """This is what makes the low-threshold scratch test work: without it the
    instance would seed itself into silence and prove nothing."""
    from clawatch_bridge import pressure
    from clawatch_bridge.config import settings
    monkeypatch.setattr(settings, "ctx_seed_baseline", False, raising=False)
    pressure.observe(th(200_000))
    fired = rows(env, "nudge.fired")
    assert len(fired) == 1 and fired[0]["level"] == "HARD"


def test_hard_crossing_fires_once_not_soft_then_hard(env):
    from clawatch_bridge import pressure
    pressure.observe(th(10_000))
    pressure.observe(th(200_000))
    fired = rows(env, "nudge.fired")
    assert [f["level"] for f in fired] == ["HARD"]
    pressure.observe(th(130_000))     # back into SOFT band, still disarmed
    assert len(rows(env, "nudge.fired")) == 1


def test_sample_is_throttled_by_delta(env):
    from clawatch_bridge import pressure
    pressure.observe(th(100_000))
    for d in (100_500, 101_000, 102_000):   # all under the 5000 delta
        pressure.observe(th(d))
    assert len(rows(env, "ctx.sample")) == 1
    pressure.observe(th(106_000))            # crosses the delta
    assert len(rows(env, "ctx.sample")) == 2


def test_a_thread_with_no_paneId_never_fires_or_logs(env):
    """An event we cannot attribute is worse than no event."""
    from clawatch_bridge import pressure
    t = th(200_000, pane=None)
    assert pressure.observe(t) == "HARD"
    assert rows(env) == []


def test_unparsed_monitors_the_monitor_and_reparse_closes_it(env):
    from clawatch_bridge import pressure
    pressure.observe(th(100_000))
    pressure.observe(th(None))
    u = rows(env, "ctx.unparsed")
    assert len(u) == 1 and u[0]["consecutive"] == 1
    pressure.observe(th(100_000))
    r = rows(env, "ctx.reparsed")
    assert len(r) == 1 and r[0]["consecutive"] == 1


def test_a_bash_pane_with_no_bar_is_not_logged_as_drift(env):
    """A shell legitimately has no status bar; logging it would bury the real
    signal in noise."""
    from clawatch_bridge import pressure
    pressure.observe(th(None, command="bash"))
    assert rows(env, "ctx.unparsed") == []
