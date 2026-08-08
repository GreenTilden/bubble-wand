"""Control-key allowlist tests.

WHY THIS EXISTS: the client sends an ACTION NAME, never a tmux key string. send_key
resolves the name against a fixed map, so no caller-controlled string can reach
`tmux send-keys` as a key. These tests pin both halves of that contract — the map
resolves what it claims to, and an unknown name raises rather than falling through
to something.

The navigation keys (up/down/left/right) were added for the Mini App's soft-key
row. They are safe to expose beside keys that commit precisely because they do not
commit: each moves a selection or a cursor. The separation test below is the one
that matters — it fails the moment a navigation action starts resolving to Enter.
"""

import pytest

from clawatch_bridge import tmux


ALL_ACTIONS = ["escape", "interrupt", "clear", "enter", "tab", "up", "down", "left", "right"]


@pytest.fixture
def sent(monkeypatch):
    """Capture the argv send_key would hand to tmux, without running tmux."""
    calls = []
    monkeypatch.setattr(tmux, "_pane_target", lambda i: f"dev:1.{i}")
    monkeypatch.setattr(tmux, "_run", lambda argv: calls.append(argv))
    return calls


@pytest.mark.parametrize(
    "action,key",
    [
        ("escape", "Escape"),
        ("interrupt", "C-c"),
        ("clear", "C-u"),
        ("enter", "Enter"),
        ("tab", "Tab"),
        ("up", "Up"),
        ("down", "Down"),
        ("left", "Left"),
        ("right", "Right"),
    ],
)
def test_each_action_resolves_to_its_tmux_key(sent, action, key):
    tmux.send_key(3, action=action)
    assert sent == [["send-keys", "-t", "dev:1.3", key]]


def test_an_unknown_action_raises_and_sends_nothing(sent):
    with pytest.raises(ValueError, match="unknown key action"):
        tmux.send_key(3, action="Enter")  # a tmux key name is NOT an action name
    assert sent == []


def test_a_raw_key_string_cannot_be_smuggled_through(sent):
    for hostile in ["C-c Enter", "Enter", "; rm -rf /", "Up Enter", ""]:
        with pytest.raises(ValueError):
            tmux.send_key(3, action=hostile)
    assert sent == []


def test_navigation_keys_never_resolve_to_something_that_commits(sent):
    """The whole reason arrows are safe as plain taps: they move, they don't submit."""
    committing = {"Enter", "C-c", "C-u"}
    for action in ["up", "down", "left", "right", "tab"]:
        sent.clear()
        tmux.send_key(3, action=action)
        keys = set(sent[0][3:])
        assert not (keys & committing), f"{action} resolved to a committing key: {keys}"


def test_one_action_sends_exactly_one_key(sent):
    """A soft-key tap is one keypress. A map entry that grew a second key would
    make a tap do two things, which is not what the button says it does."""
    for action in ALL_ACTIONS:
        sent.clear()
        tmux.send_key(3, action=action)
        assert len(sent) == 1
        assert len(sent[0]) == 4, f"{action} sent {len(sent[0]) - 3} keys, expected 1"
