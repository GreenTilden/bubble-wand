"""Colour passthrough — `capture-pane -e` for clients that can render it.

WHY THIS EXISTS: tmux flattens colour unless you ask for `-e`, and the moment
you do, every line-inspecting function in tmux.py starts seeing escape bytes
in front of the text it matches on. The status bar stops looking like the
status bar, a divider stops looking like a divider, and a menu option stops
matching the option regex — so the phone would quietly get the chrome back
AND lose its menus, with colour as the only visible change. These pin the
split: parsers read stripped text, the display payload keeps its colour.

The watch is the other half of the contract. It asks for no colour and must
receive exactly what it received before, byte for byte.
"""

from clawatch_bridge import tmux
from clawatch_bridge.tmux import _clean_tail, parse_prompt, parse_status, strip_ansi

# Real shapes: SGR runs around a status bar, an OSC title, a coloured menu.
RED = "\x1b[31m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"
TRUECOLOR = "\x1b[38;2;215;119;87m"
C256 = "\x1b[38;5;208m"
OSC_TITLE = "\x1b]0;claude — demo-repo\x07"

BAR = "Opus 5 (1M context) · ▓▓▓▓▓▓▓░░░ 107k ctx · Σ5.8M · $2.77"


def test_strip_ansi_removes_every_sequence_shape():
    assert strip_ansi(f"{RED}hello{RESET}") == "hello"
    assert strip_ansi(f"{TRUECOLOR}hi{RESET}") == "hi"
    assert strip_ansi(f"{C256}hi{RESET}") == "hi"
    assert strip_ansi(f"{OSC_TITLE}hi") == "hi"
    assert strip_ansi("\x1b[2Jcleared") == "cleared"
    assert strip_ansi("plain") == "plain"  # no-op on plain text


def test_coloured_status_bar_is_still_recognised_as_chrome():
    """The status bar is the line most likely to be coloured -- it is a meter.
    Note it survives even a naive implementation, because its chrome rule is a
    substring test ("·" and "ctx"); the PREFIX-matched chrome below is where a
    raw probe actually breaks, which is why both are pinned."""
    out = _clean_tail([f"{DIM}{BAR}{RESET}", "real output"])
    assert out == ["real output"]


def test_prefix_matched_chrome_is_still_recognised_when_coloured():
    """_is_chrome tests startswith() for the auto-mode hint, tips and the
    cancel footer. A leading colour run puts an escape byte in front of the
    prefix and every one of those rules silently stops matching -- so turning
    colour on would hand the phone back the chrome the watch spent effort
    removing."""
    out = _clean_tail([
        f"{DIM}⏵⏵ auto mode on{RESET}",
        f"{DIM}Tip: press esc twice to edit{RESET}",
        f"{DIM}Esc to cancel{RESET}",
        "real output",
    ])
    assert out == ["real output"]


def test_coloured_divider_is_still_recognised_as_a_border():
    out = _clean_tail([f"{DIM}{'─' * 40}{RESET}", "real output"])
    assert out == ["real output"]


def test_colour_survives_cleaning_on_the_lines_that_are_kept():
    """Cleaning decides on stripped text but must return the ORIGINAL line --
    stripping here would make the whole feature a no-op with no error."""
    line = f"{TRUECOLOR}● Running the suite{RESET}"
    assert _clean_tail([line]) == [line]


def test_blank_run_collapse_sees_through_colour():
    """A 'blank' line from a coloured pane is often not empty -- it carries a
    reset sequence. Testing `.strip()` on the raw line would treat it as
    content and defeat the collapse."""
    out = _clean_tail(["a", RESET, RESET, "b"])
    assert out == ["a", RESET, "b"]


def test_leading_and_trailing_coloured_blanks_are_trimmed():
    assert _clean_tail([RESET, "a", f"{RED}{RESET}"]) == ["a"]


def test_menu_parses_once_stripped_and_labels_carry_no_escapes():
    """What the route does: parse on stripped lines. The labels it returns are
    rendered as TEXT by clients with no ANSI decoder, so an escape reaching
    PromptInfo is a rendering bug on every one of them."""
    coloured = [
        f"{BOLD}Do you want to make this edit to tmux.py?{RESET}",
        f"{RED}❯ 1. Yes{RESET}",
        f"{C256}  2. No, tell Claude what to do differently (esc){RESET}",
    ]
    parsed = parse_prompt([strip_ansi(ln) for ln in coloured])
    assert parsed is not None
    assert [o["key"] for o in parsed["options"]] == ["1", "2"]
    assert all("\x1b" not in o["label"] for o in parsed["options"])
    assert "\x1b" not in parsed["question"]


def test_status_parser_reads_a_coloured_bar_once_stripped():
    got = parse_status([strip_ansi(f"{DIM}{BAR}{RESET}")])
    assert got is not None and got["ctxTokens"] == 107_000


# --- the -e flag itself -----------------------------------------------------


def _captured_args(monkeypatch, **kw):
    seen = {}

    def fake_run(args, **_):
        seen["args"] = args
        return "line one\n"

    monkeypatch.setattr(tmux, "_run", fake_run)
    tmux._do_capture("dev:1.1", 40, False, True, **kw)
    return seen["args"]


def test_ansi_false_does_not_pass_dash_e(monkeypatch):
    """The watch's exact call. `-e` appearing here would hand escape bytes to a
    client with no renderer -- visible as garbage, not as colour."""
    assert "-e" not in _captured_args(monkeypatch)


def test_ansi_true_passes_dash_e(monkeypatch):
    assert "-e" in _captured_args(monkeypatch, ansi=True)


def test_capture_defaults_to_no_colour(monkeypatch):
    """Signature-level: a new caller that forgets the argument gets the watch's
    behaviour, not the phone's."""
    import inspect

    assert inspect.signature(tmux.capture).parameters["ansi"].default is False
    assert inspect.signature(tmux._do_capture).parameters["ansi"].default is False
