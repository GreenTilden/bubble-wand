"""The privacy invariant of events.py, asserted rather than trusted.

These tests exist because the invariant ("metrics and pane identity only, never
pane content") is the kind of rule that decays silently: nothing breaks when it is
violated, the log just quietly starts carrying transcript text. So each test pins
one structural property that makes a violation impossible rather than merely
discouraged.
"""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture()
def evlog(tmp_path, monkeypatch):
    path = tmp_path / "wash-events.jsonl"
    monkeypatch.setenv("CLAWATCH_EVENT_LOG", str(path))
    monkeypatch.setenv("CLAWATCH_EVENT_STRICT", "1")
    from clawatch_bridge.config import settings
    monkeypatch.setattr(settings, "event_log", str(path), raising=False)
    monkeypatch.setattr(settings, "event_strict", True, raising=False)
    from clawatch_bridge import events
    events.reset_for_tests()
    yield path
    events.reset_for_tests()


def _rows(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_there_is_no_string_value_type(evlog):
    """The load-bearing property: no TEXT/STR constructor exists.

    A developer cannot log free text by adding a kwarg — they must first add a new
    type to events.py, which is a reviewable diff in the file whose whole docstring
    is the invariant.
    """
    from clawatch_bridge import events
    kinds = {events.INT, events.FLOAT, events.BOOL,
             events.IDENT, events.BASENAME, events.MODEL}
    assert "str" not in kinds and "text" not in kinds
    assert not any(k in ("str", "text", "string") for k in kinds)


def test_unknown_field_is_rejected_in_strict_mode(evlog):
    from clawatch_bridge import events
    with pytest.raises(ValueError):
        events.emit("wash.cleared", "fenton:1:1", attempts=1, elapsedMs=5,
                    transcript="the user said something private")


def test_unknown_field_is_dropped_when_not_strict(evlog, monkeypatch):
    from clawatch_bridge import events
    from clawatch_bridge.config import settings
    monkeypatch.setattr(settings, "event_strict", False, raising=False)
    events.emit("wash.cleared", "fenton:1:1", attempts=1, elapsedMs=5,
                transcript="the user said something private")
    row = _rows(evlog)[0]
    assert "transcript" not in row
    assert "private" not in json.dumps(row)


def test_reason_is_a_closed_enum_so_exception_text_cannot_land(evlog):
    """TmuxError carries proc.stderr — arbitrary subprocess output. If `reason`
    were free text, one careless reason=str(e) would put terminal content on disk."""
    from clawatch_bridge import events
    with pytest.raises(ValueError):
        events.emit("wash.failed", "fenton:1:1",
                    reason="tmux: /home/darney/secret-thing failed", stage="CLEAR",
                    durationMs=1)
    events.emit("wash.failed", "fenton:1:1", reason="tmux_error", stage="CLEAR",
                durationMs=1)
    assert _rows(evlog)[0]["reason"] == "tmux_error"


def test_model_is_slugified_not_passed_through(evlog):
    """parse_status derives `model` from an unbounded slice of a rendered line.
    Slugification bounds the blast radius if the bar parser ever mis-picks."""
    from clawatch_bridge import events
    events.emit("nudge.fired", "fenton:1:1", paneIndex=1, level="SOFT",
                ctxTokens=130000, ctxResolution=1000, status="IDLE",
                model="Opus 5 (1M context) — private client name here")
    m = _rows(evlog)[0]["model"]
    assert m == "opus-5-1m-context-private-client-name-here"[:40]
    assert len(m) <= 40
    assert all(c.isalnum() or c == "-" for c in m)
    # The point of the cap: an unbounded slice of a mis-parsed line cannot land
    # here in full.
    assert "here" not in m


def test_basename_rejects_a_path(evlog):
    from clawatch_bridge import events
    with pytest.raises(ValueError):
        events.emit("nudge.fired", "fenton:1:1", paneIndex=1, level="SOFT",
                    ctxTokens=1, ctxResolution=1000, status="IDLE",
                    repo="/home/darney/projects/secret-project")


def test_envelope_is_server_generated(evlog):
    from clawatch_bridge import events
    with pytest.raises(ValueError):
        events.emit("wash.cleared", "fenton:1:1", attempts=1, elapsedMs=1,
                    ts="1999-01-01T00:00:00Z")
    events.emit("wash.cleared", "fenton:1:1", attempts=1, elapsedMs=1)
    row = _rows(evlog)[0]
    assert row["v"] == events.SCHEMA_VERSION
    assert row["ts"].startswith("20")


def test_no_schema_carries_a_cost_or_spend_field(evlog):
    """Their absence is what keeps this log out of the Darren-only spend class."""
    from clawatch_bridge import events
    for name, spec in events.SCHEMA.items():
        for field in spec:
            assert "cost" not in field.lower(), f"{name}.{field}"
            assert "spend" not in field.lower(), f"{name}.{field}"
            assert "usd" not in field.lower(), f"{name}.{field}"


def test_nudge_delivered_is_refused(evlog):
    """The watch PULLS; the bridge cannot observe delivery. An invented
    nudge.delivered would fabricate the denominator of nudgeToBathConversion."""
    from clawatch_bridge import events
    assert "nudge.delivered" not in events.SCHEMA


def test_log_file_is_0600(evlog):
    from clawatch_bridge import events
    events.emit("wash.cleared", "fenton:1:1", attempts=1, elapsedMs=1)
    assert oct(os.stat(evlog).st_mode & 0o777) == "0o600"


def test_oversize_line_is_dropped_with_only_a_byte_count(evlog, monkeypatch):
    from clawatch_bridge import events
    from clawatch_bridge.config import settings
    monkeypatch.setattr(settings, "event_strict", False, raising=False)
    monkeypatch.setattr(events, "MAX_LINE_BYTES", 80, raising=False)
    events.emit("nudge.fired", "fenton:1:1", paneIndex=1, level="SOFT",
                ctxTokens=130000, ctxResolution=1000, status="IDLE",
                model="x" * 40, repo="some-repo")
    rows = _rows(evlog)
    assert rows and rows[-1]["event"] == "evt.oversize"
    assert set(rows[-1]) <= {"v", "ts", "event", "bytes"}


def test_no_events_route_exists():
    """The collector reads this file from local disk. A route would put every
    thread's context curve on an internet-reachable surface to save a `cat`."""
    from clawatch_bridge import main
    paths = [getattr(r, "path", "") for r in main.app.routes]
    assert not any("event" in p for p in paths), \
        "the event log must not be internet-reachable"
