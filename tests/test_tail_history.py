"""Bounded scrollback — what makes `lines` mean something above the pane height.

Bare `capture-pane -p` returns the VISIBLE pane and nothing more, so the tail slice
inside _do_capture could only ever SHRINK the result: asking a 45-row pane for 150
lines returned 45, with no error and no way for the caller to tell "that's all there
is" from "we never asked for more". That silence is the bug these tests exist for --
a naive `lines=150` looks like it works and quietly returns a screenshot.

`history=True` adds `-S -{lines}`, bounded by the caller's own request. Deliberately
NOT the same thing as `scrollback=True`, which is `-S -` -- the entire buffer, however
many thousand lines that happens to be.

della cycle-66 L15 · catch-me-up digest.
"""
import inspect

import pytest

from clawatch_bridge import tmux


def _captured_args(monkeypatch, lines=150, **kw):
    seen = {}

    def fake_run(args, **_):
        seen["args"] = args
        return "line one\n"

    monkeypatch.setattr(tmux, "_run", fake_run)
    tmux._do_capture("dev:1.1", lines, kw.pop("scrollback", False), True, **kw)
    return seen["args"]


def test_history_false_asks_for_the_visible_pane_only(monkeypatch):
    """The watch's exact call, and the default. A stray -S here would change what
    every existing client sees on every poll."""
    assert "-S" not in _captured_args(monkeypatch, lines=40)


def test_history_true_bounds_the_start_by_the_requested_lines(monkeypatch):
    args = _captured_args(monkeypatch, lines=150, history=True)
    assert "-S" in args
    assert args[args.index("-S") + 1] == "-150", (
        "the start offset must track the caller's own `lines`, not a fixed constant")


def test_history_is_not_the_whole_buffer(monkeypatch):
    """The distinction that keeps a digest from shipping a 10k-line buffer to Anthropic."""
    bounded = _captured_args(monkeypatch, lines=150, history=True)
    everything = _captured_args(monkeypatch, lines=150, scrollback=True)
    assert bounded[bounded.index("-S") + 1] == "-150"
    assert everything[everything.index("-S") + 1] == "-"


def test_scrollback_still_wins_over_history(monkeypatch):
    """Both set: the explicit whole-buffer request is the wider one and must not be
    silently narrowed to the bounded window."""
    args = _captured_args(monkeypatch, lines=150, scrollback=True, history=True)
    assert args[args.index("-S") + 1] == "-"


def test_history_defaults_off_at_the_signature(monkeypatch):
    """A new caller that forgets the argument gets today's behaviour, not the deeper
    (and more expensive) read. Same doctrine as `ansi`."""
    assert inspect.signature(tmux.capture).parameters["history"].default is False
    assert inspect.signature(tmux._do_capture).parameters["history"].default is False


@pytest.mark.parametrize("lines", [0, -5])
def test_nonpositive_lines_asks_for_no_history(monkeypatch, lines):
    """`lines=0` already means "don't slice"; it must not become `-S -0`, which asks
    tmux for a window the caller never requested."""
    assert "-S" not in _captured_args(monkeypatch, lines=lines, history=True)
