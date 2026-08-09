"""The wash guards — the checks standing between a bug and a live Claude session.

SCOPE NOTE, stated rather than implied: the end-to-end wash was exercised against
a scratch tmux session, which proved the pane-command allowlist and the
abort-without-reseeding path on real panes. It could NOT prove the re-seed path,
because a bash pane does not clear when sent "/clear" — so the sentinel always
survives and the wash (correctly) stops. The re-seed is therefore covered here,
at the unit level, rather than left claimed-but-untested. The one piece proven
on a real pane is the paste primitive itself: three lines loaded and pasted with
`paste-buffer -p` rendered as three lines, submitted nothing, and left no buffer.

The command-path guard tests were REMOVED with the command path (2026-08-08).
There is one re-seed lane now — paste the tail — because a second lane that
fires only on repos carrying a marker file is a lane that gets tested rarely and
trusted anyway.
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
    monkeypatch.setattr(settings, "reseed_tail_enabled", True, raising=False)
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


# ── the tail-paste fallback ─────────────────────────────────────────────────
# Used where the slash-command does not apply. Everything the command path
# guards, this path must guard too — the Enter at the end is just as live.

def _paste_env(monkeypatch, rendered, *, menu=None, same_pane=True):
    """Wire the paste path: `rendered` is what capture() returns AFTER the paste."""
    from clawatch_bridge import wash, tmux
    calls = {"keys": [], "pasted": []}
    seq = iter(["", *([rendered] * 8)])  # first capture = empty input, then rendered

    monkeypatch.setattr(tmux, "paste", lambda i, t: calls["pasted"].append(t))
    monkeypatch.setattr(tmux, "send_key", lambda i, a: calls["keys"].append(a))
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: [next(seq, rendered)])
    monkeypatch.setattr(tmux, "parse_prompt", lambda lines: menu)
    monkeypatch.setattr(tmux, "_clean_tail", lambda x: x)
    monkeypatch.setattr(wash, "_still_same_pane", lambda w: same_pane)
    monkeypatch.setattr(wash.time, "sleep", lambda s: None)
    return calls


def test_the_clean_paste_path_submits(env, monkeypatch):
    from clawatch_bridge import wash
    calls = _paste_env(monkeypatch, "❯ [Pasted text #1 +40 lines]")
    guard, submitted = wash._paste_and_submit(1, "some tail", {"index": 1, "paneId": "%1"})
    assert guard == "clean" and submitted is True
    assert calls["keys"] == ["enter"]
    assert calls["pasted"] == ["some tail"]


def test_an_empty_tail_never_presses_enter(env, monkeypatch):
    """Enter on an empty input would submit a blank prompt into a session that
    was just cleared — the wash would have made things strictly worse."""
    from clawatch_bridge import wash
    calls = _paste_env(monkeypatch, "❯")
    guard, submitted = wash._paste_and_submit(1, "", {"index": 1, "paneId": "%1"})
    assert guard == "empty_tail" and submitted is False
    assert calls["keys"] == [] and calls["pasted"] == []


def test_a_paste_that_did_not_land_aborts(env, monkeypatch):
    """Input unchanged after the paste = it did not land. Same reasoning as the
    empty tail: never Enter on an input we cannot see content in."""
    from clawatch_bridge import wash
    calls = _paste_env(monkeypatch, "")   # still empty after pasting
    guard, submitted = wash._paste_and_submit(1, "some tail", {"index": 1, "paneId": "%1"})
    assert guard == "paste_not_rendered" and submitted is False
    assert "enter" not in calls["keys"]


def test_a_menu_on_screen_aborts_the_paste_too(env, monkeypatch):
    """Guard 4a is not command-specific: a paste can land while a permission
    menu is already up, and the Enter would answer the menu."""
    from clawatch_bridge import wash
    calls = _paste_env(monkeypatch, "❯ 1. Yes", menu={"options": [{"label": "Yes"}]})
    guard, submitted = wash._paste_and_submit(1, "some tail", {"index": 1, "paneId": "%1"})
    assert guard == "menu_present_aborted" and submitted is False
    assert "enter" not in calls["keys"] and "clear" in calls["keys"]


def test_a_pane_that_moved_aborts_before_pasting(env, monkeypatch):
    from clawatch_bridge import wash
    calls = _paste_env(monkeypatch, "❯ x", same_pane=False)
    guard, submitted = wash._paste_and_submit(1, "some tail", {"index": 1, "paneId": "%1"})
    assert guard == "completion_drift_aborted" and submitted is False
    assert calls["pasted"] == [], "nothing may be pasted into a pane that moved"


def test_the_tail_is_bounded_on_both_axes(env, monkeypatch):
    """An unbounded paste would refill the context the wash just emptied."""
    from clawatch_bridge import wash, tmux
    from clawatch_bridge.config import settings
    monkeypatch.setattr(tmux, "_clean_tail", lambda x: x)
    monkeypatch.setattr(settings, "reseed_tail_lines", 10, raising=False)
    monkeypatch.setattr(settings, "reseed_tail_max_chars", 400, raising=False)

    text = wash._reseed_tail_text([f"line {i} " + "x" * 60 for i in range(500)])
    assert len(text) <= 400
    assert text.startswith(wash.RESEED_TAIL_PREAMBLE)
    # Trimmed from the FRONT — the most recent lines are the ones worth keeping.
    assert "line 499" in text and "line 400" not in text


def test_the_tail_says_what_it_is(env, monkeypatch):
    """The model must not read a transcript of finished work as work in flight."""
    from clawatch_bridge import wash, tmux
    monkeypatch.setattr(tmux, "_clean_tail", lambda x: x)
    text = wash._reseed_tail_text(["did the thing"])
    assert "was cleared" in text and "not a restored conversation" in text


def test_a_blank_tail_produces_no_payload(env, monkeypatch):
    from clawatch_bridge import wash, tmux
    monkeypatch.setattr(tmux, "_clean_tail", lambda x: x)
    assert wash._reseed_tail_text(["", "   ", ""]) == ""


def test_reseed_tail_is_a_valid_event_value(env):
    """event_strict rejects an unknown enum value, so "tail" must be declared —
    otherwise every fallback re-seed would blow up at the emit, not at review."""
    from clawatch_bridge import events
    assert "tail" in events.SCHEMA["wash.reseeded"]["reseed"].allowed
    assert "tail" in events.SCHEMA["wash.completed"]["reseed"].allowed


# ── the clear-verification sentinel ─────────────────────────────────────────
# The bug that made the wash look broken on 2026-08-08: the sentinel was taken
# from a RAW capture, so it could be a box-drawing border from Claude Code's
# input frame -- which is redrawn right after a successful /clear. It survived
# every attempt, and the wash aborted on panes that had cleared fine.

def test_a_pane_that_really_cleared_is_not_reported_as_uncleared(env, monkeypatch):
    """The 2026-08-08 failure, end to end.

    Models a pane that clears properly: the real output goes away, and Claude
    Code immediately redraws its input frame. If the sentinel and the
    verification both come from a RAW capture, the sentinel IS that frame, it is
    still on screen afterwards, and the wash reports sentinel_survived on a pane
    that cleared perfectly. Asserting on the OUTCOME rather than on capture
    arguments is deliberate -- an argument assertion passes as soon as any one
    call is cleaned, which is how the first version of this test passed against
    the very bug it was written for.
    """
    from clawatch_bridge import wash, tmux
    frame = "╭" + "─" * 78 + "╮"
    state = {"cleared": False}

    def fake_send(index, text, submit):
        if text == "/clear":
            state["cleared"] = True

    def fake_capture(index, lines, scrollback, clean=True):
        body = [] if state["cleared"] else ["the actual output line that matters"]
        return body if clean else [frame, *body]   # the frame is ALWAYS drawn

    monkeypatch.setattr(tmux, "capture", fake_capture)
    monkeypatch.setattr(tmux, "send", fake_send)
    monkeypatch.setattr(tmux, "send_key", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "paste", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "parse_prompt", lambda lines: None)
    monkeypatch.setattr(wash, "_still_same_pane", lambda w: True)
    monkeypatch.setattr(wash, "_ctx_now", lambda i: 1000)
    monkeypatch.setattr(wash.time, "sleep", lambda s: None)
    wash._WASHES["w1"] = {
        "washId": "w1", "index": 1, "paneId": "%1", "threadKey": "t",
        "stage": "QUEUED", "outcome": None, "startedMs": 0, "trigger": "manual",
        "ctxBefore": 1, "ctxResolution": 1000, "reseed": None, "error": None,
    }
    wash._run_wash("w1")

    assert not rows(env, "wash.clear_failed"), \
        "a pane whose content really went away must not read as uncleared"
    assert wash.status("w1")["outcome"] != "failed"


def test_a_border_line_is_not_a_usable_sentinel_once_cleaned(env):
    """_clean_tail is what makes the sentinel trustworthy: strip the frame and
    the longest remaining line is real output, which /clear actually removes."""
    from clawatch_bridge import wash, tmux
    frame = "╭" + "─" * 78 + "╮"
    raw = [frame, "short", "the actual output line that matters"]
    assert wash._sentinel(raw) == frame, "raw capture picks the border (the bug)"
    assert wash._sentinel(tmux._clean_tail(raw)) != frame, "cleaned does not"


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


# --- Guard 1b: the manual path had no NEEDS_INPUT check at all ---------------
#
# maybe_autowash has refused non-IDLE panes since it was written and says why in
# its own docstring: washing a pane at NEEDS_INPUT answers a question with
# /clear. That check lived in maybe_autowash. BOTH triggers come through
# request_wash -- so the guard covered the path that ships DISABLED, and not the
# one an operator taps from the FAB. The only NEEDS_INPUT test in this file
# before now exercised maybe_autowash, which is why nothing failed.

def _prompted_pane(**over):
    t = {"index": 2, "paneId": "%46", "command": "claude", "repo": "dellatech",
         "status": "NEEDS_INPUT", "hasPrompt": True, "ctxTokens": 90_000}
    t.update(over)
    return t


@pytest.mark.parametrize("trigger", ["manual", "auto"])
def test_wash_refuses_a_pane_that_is_mid_question(env, monkeypatch, trigger):
    from clawatch_bridge import wash
    monkeypatch.setattr(wash, "_thread_by_index", lambda i: _prompted_pane())
    wash_id, reason = wash.request_wash(2, trigger=trigger)
    assert (wash_id, reason) == (None, "pane_needs_input")


def test_refusal_holds_on_status_alone_and_on_hasprompt_alone(env, monkeypatch):
    """Two independent signals set it (title glyph, and the tail-scan override in
    list_threads). Either one on its own has to be enough, or the guard has a
    hole exactly where detection is least certain."""
    from clawatch_bridge import wash
    for over in ({"hasPrompt": False}, {"status": "WORKING", "hasPrompt": True}):
        monkeypatch.setattr(wash, "_thread_by_index", lambda i, o=over: _prompted_pane(**o))
        assert wash.request_wash(2, trigger="manual")[1] == "pane_needs_input"


def test_working_pane_without_a_prompt_is_still_washable(env, monkeypatch):
    """WORKING is NOT blocked: the wash opens with Escape, which is the ordinary
    way to interrupt a running response. Answering an unseen question is the
    different, unrecoverable case -- so the guard must not quietly widen."""
    from clawatch_bridge import wash
    monkeypatch.setattr(wash, "_thread_by_index",
                        lambda i: _prompted_pane(status="WORKING", hasPrompt=False))
    monkeypatch.setattr(wash, "_run_wash", lambda wid: None)
    wash_id, reason = wash.request_wash(2, trigger="manual")
    assert reason is None and wash_id


def test_the_refusal_is_a_countable_event(env, monkeypatch):
    """Every other rejection here emits wash.guard_blocked so a wash that never
    ran is a fact rather than a gap in the series. This one has to as well."""
    from clawatch_bridge import wash
    monkeypatch.setattr(wash, "_thread_by_index", lambda i: _prompted_pane())
    wash.request_wash(2, trigger="manual")
    rows = [json.loads(l) for l in env.read_text().splitlines() if l.strip()]
    blocked = [r for r in rows if r.get("event") == "wash.guard_blocked"]
    assert blocked and blocked[-1]["reason"] == "pane_needs_input"
    assert blocked[-1]["trigger"] == "manual"
