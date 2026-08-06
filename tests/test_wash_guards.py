"""The wash guards — the checks standing between a bug and a live Claude session.

SCOPE NOTE, stated rather than implied: the end-to-end wash was exercised against
a scratch tmux session, which proved the pane-command allowlist and the
abort-without-reseeding path on real panes. It could NOT prove the re-seed path,
because a bash pane does not clear when sent "/clear" — so the sentinel always
survives and the wash (correctly) stops. The re-seed branches are therefore
covered here, at the unit level, rather than left claimed-but-untested.
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
    monkeypatch.setattr(settings, "wash_enabled", True, raising=False)
    monkeypatch.setattr(settings, "wash_pane_commands", ("claude",), raising=False)
    monkeypatch.setattr(settings, "reseed_command", "/brief", raising=False)
    monkeypatch.setattr(settings, "reseed_probe", ".foreman/cycle.json", raising=False)
    monkeypatch.setattr(settings, "autowash_enabled", False, raising=False)
    from clawatch_bridge import events, tmux, wash
    monkeypatch.setattr(tmux, "server_pid", lambda: 4242)
    events.reset_for_tests()
    wash.reset_for_tests()
    yield path
    events.reset_for_tests()
    wash.reset_for_tests()


def rows(path, event=None):
    if not path.exists():
        return []
    out = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return [r for r in out if event is None or r["event"] == event]


# ── guard 4b: the completion check parse_prompt structurally cannot do ───────
# parse_prompt() returns None unless it finds a cursored NUMBERED option, and
# Claude Code's slash-command popup is not numbered. So "/brief" advancing toward
# "/batch-brief" is invisible to it. This is the check that catches it.

@pytest.mark.parametrize("line,cmd,ok", [
    ("❯ /brief", "/brief", True),
    ("│ ❯ /brief                       │", "/brief", True),
    ("> /brief", "/brief", True),
    ("❯ /batch-brief", "/brief", False),      # the exact drift being guarded
    ("❯ /pre-brief", "/brief", False),        # shares the prefix, does something else
    ("❯ /briefing", "/brief", False),
    ("❯ tell me about /brief", "/brief", True),   # trailing token IS the command
    ("❯ /brief extra", "/brief", False),
    ("", "/brief", False),
])
def test_typed_line_is_exact(env, line, cmd, ok):
    from clawatch_bridge import wash
    assert wash._typed_line_is_exact([line], cmd) is ok


def test_typed_line_uses_the_bottom_most_line(env):
    """Same reason parse_status scans bottom-up: the live input line is the last
    one, and earlier lines can contain anything."""
    from clawatch_bridge import wash
    lines = ["❯ /batch-brief", "some output", "❯ /brief"]
    assert wash._typed_line_is_exact(lines, "/brief") is True


@pytest.mark.parametrize("cmd", ["brief", "/brief now", "/" + "x" * 100, ""])
def test_a_non_slash_command_is_refused_without_typing_anything(env, cmd, monkeypatch):
    """A re-seed command is a bare slash-command. Anything else is a config error,
    and typing it blind into a live session is the worst possible response."""
    from clawatch_bridge import wash, tmux
    typed = []
    monkeypatch.setattr(tmux, "send", lambda *a, **k: typed.append(a))
    monkeypatch.setattr(tmux, "send_key", lambda *a, **k: typed.append(a))
    guard, submitted = wash._type_and_submit(1, cmd, {"index": 1, "paneId": "%1"})
    assert guard == "invalid_command" and submitted is False
    assert typed == [], "nothing may be typed when the command is invalid"


def test_a_menu_on_screen_aborts_and_withdraws(env, monkeypatch):
    """Guard 4a, the safety-critical one: never press Enter while a permission
    menu is up — the Enter would answer the menu, not submit the command."""
    from clawatch_bridge import wash, tmux
    keys = []
    monkeypatch.setattr(tmux, "send", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "send_key", lambda i, a: keys.append(a))
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: ["❯ 1. Yes", "  2. No"])
    monkeypatch.setattr(tmux, "parse_prompt", lambda lines: {"options": [{"label": "Yes"}]})
    monkeypatch.setattr(tmux, "_clean_tail", lambda x: x)
    monkeypatch.setattr(wash, "_still_same_pane", lambda w: True)
    guard, submitted = wash._type_and_submit(1, "/brief", {"index": 1, "paneId": "%1"})
    assert guard == "menu_present_aborted" and submitted is False
    assert "enter" not in keys, "Enter must never be pressed with a menu up"
    assert "clear" in keys, "what we typed must be withdrawn"


def test_completion_drift_aborts_and_withdraws(env, monkeypatch):
    from clawatch_bridge import wash, tmux
    keys = []
    monkeypatch.setattr(tmux, "send", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "send_key", lambda i, a: keys.append(a))
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: ["❯ /batch-brief"])
    monkeypatch.setattr(tmux, "parse_prompt", lambda lines: None)
    monkeypatch.setattr(tmux, "_clean_tail", lambda x: x)
    monkeypatch.setattr(wash, "_still_same_pane", lambda w: True)
    guard, submitted = wash._type_and_submit(1, "/brief", {"index": 1, "paneId": "%1"})
    assert guard == "completion_drift_aborted" and submitted is False
    assert "enter" not in keys


def test_the_clean_path_submits(env, monkeypatch):
    from clawatch_bridge import wash, tmux
    keys = []
    monkeypatch.setattr(tmux, "send", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "send_key", lambda i, a: keys.append(a))
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: ["❯ /brief"])
    monkeypatch.setattr(tmux, "parse_prompt", lambda lines: None)
    monkeypatch.setattr(tmux, "_clean_tail", lambda x: x)
    monkeypatch.setattr(wash, "_still_same_pane", lambda w: True)
    guard, submitted = wash._type_and_submit(1, "/brief", {"index": 1, "paneId": "%1"})
    assert guard == "clean" and submitted is True
    assert keys == ["enter"]


# ── the re-seed probe: the countable negative case ──────────────────────────

def test_probe_absent_in_a_repo_without_the_marker(env, monkeypatch):
    """duckminster has no .foreman/, which is why it is the natural negative case:
    a wash there records reseed:"none" with probe:"absent" — a FACT the collector
    can count, not a silent degradation."""
    from clawatch_bridge import wash, tmux
    monkeypatch.setattr(tmux, "_run", lambda *a, **k: "/home/darney/projects/duckminster\n")
    assert wash._probe_state(1) == "absent"


def test_probe_present_in_a_foreman_repo(env, monkeypatch):
    from clawatch_bridge import wash, tmux
    monkeypatch.setattr(tmux, "_run", lambda *a, **k: "/home/darney/projects/darntech\n")
    assert wash._probe_state(1) == "present"


def test_probe_unconfigured_when_no_probe_is_set(env, monkeypatch):
    from clawatch_bridge import wash
    from clawatch_bridge.config import settings
    monkeypatch.setattr(settings, "reseed_probe", "", raising=False)
    assert wash._probe_state(1) == "unconfigured"


# ── the sentinel never reaches disk ─────────────────────────────────────────

def test_no_wash_event_can_carry_the_sentinel(env):
    """The sentinel is computed from pane content. events.py must have no field it
    could be written into — this asserts that structurally, not by inspection."""
    from clawatch_bridge import events
    for name, spec in events.SCHEMA.items():
        if not name.startswith("wash."):
            continue
        for field, kind in spec.items():
            assert kind is not events.MODEL or field in ("command", "model"), \
                f"{name}.{field} could carry slugified free text"
            assert kind in (events.INT, events.BOOL, events.IDENT,
                            events.BASENAME, events.MODEL) or isinstance(kind, events._Enum), \
                f"{name}.{field} is not one of the bounded types"


def test_autowash_ships_disabled(env, monkeypatch):
    """One `if`, one env var — and it must be OFF by default."""
    from clawatch_bridge import wash
    called = []
    monkeypatch.setattr(wash, "request_wash", lambda *a, **k: called.append(a))
    wash.maybe_autowash([{"index": 1, "status": "IDLE", "ctxTokens": 999_999}])
    assert called == []


def test_autowash_when_enabled_only_touches_idle_hard_panes(env, monkeypatch):
    from clawatch_bridge import wash
    from clawatch_bridge.config import settings
    monkeypatch.setattr(settings, "autowash_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ctx_hard_tokens", 150_000, raising=False)
    monkeypatch.setattr(settings, "ctx_soft_tokens", 120_000, raising=False)
    called = []
    monkeypatch.setattr(wash, "request_wash", lambda idx, trigger=None: called.append(idx))
    wash.maybe_autowash([
        {"index": 1, "status": "WORKING", "ctxTokens": 999_999},    # would interrupt a response
        {"index": 2, "status": "NEEDS_INPUT", "ctxTokens": 999_999},  # would answer a question with /clear
        {"index": 3, "status": "IDLE", "ctxTokens": 10_000},          # not under pressure
        {"index": 4, "status": "IDLE", "ctxTokens": 999_999},         # the only valid target
    ])
    assert called == [4]
