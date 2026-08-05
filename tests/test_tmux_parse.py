"""Status-bar parsing regression tests.

WHY THIS EXISTS: parse_status() used to scan the captured pane TOP-DOWN and return the first
line that looked like a status bar. The panes this bridge watches are Claude Code agents that
routinely *discuss* context budgets, so a rendered conversation line like

    soft ~120k ctx · hard ~150k

parses perfectly and would win over the real bar sitting at the bottom of the pane. While the
value only tinted a colour meter that was cosmetic. Once it drives a nudge threshold and gets
appended to a permanent event log, a wrong sample is a wrong permanent record.

The decoy test below FAILS on the top-down implementation and passes on the bottom-up one.
"""

from clawatch_bridge.tmux import parse_status


# Real status lines captured from the live dev:1 panes.
BAR_107K = "Opus 5 (1M context) · ▓▓▓▓▓▓▓░░░ 107k ctx · Σ5.8M · $2.77"
BAR_232K = "Opus 5 (1M context) · ▓▓▓▓▓▓▓▓▓▓ 232k ctx HARD · Σ28.7M · $12.54"
BAR_98K = "Sonnet 5 · ▓▓▓▓▓░░░░░ 98k ctx · Σ1.2M · $0.41"


def test_parses_a_plain_bar():
    got = parse_status([BAR_107K])
    assert got is not None
    assert got["ctxTokens"] == 107_000
    assert got["ctxTier"] is None  # no tier word below the vendor's own threshold
    assert got["model"] == "Opus 5 (1M context)"


def test_parses_tier_when_present():
    got = parse_status([BAR_232K])
    assert got["ctxTokens"] == 232_000
    assert got["ctxTier"] == "HARD"


def test_decoy_conversation_line_above_the_bar_does_not_win():
    """THE regression. A conversation line that mentions context budgets sits ABOVE the real
    status bar; bottom-up scanning must return the bar, not the decoy."""
    pane = [
        "  I'll break the session at the usual thresholds.",
        "  soft ~120k ctx · hard ~150k — that's the fleet rule",
        "",
        BAR_232K,
    ]
    got = parse_status(pane)
    assert got["ctxTokens"] == 232_000, "picked up the decoy conversation line, not the bar"
    assert got["ctxTier"] == "HARD"


def test_bottom_most_bar_wins_when_pane_shows_two():
    """A redraw can leave a stale bar higher in the pane. The newest is the bottom-most."""
    got = parse_status([BAR_98K, "", BAR_107K])
    assert got["ctxTokens"] == 107_000


def test_ctx_resolution_reflects_the_rendering_step():
    """ctxTokens comes from a ROUNDED rendered string, so precision is the render step, not
    1 token. Without this a consumer can report a 400-token 'improvement' that is pure noise."""
    assert parse_status([BAR_107K])["ctxResolution"] == 1_000
    # a one-decimal megatoken render is precise to 0.1M
    assert parse_status(["Opus 5 · ▓ 1.2M ctx · $9.99"])["ctxResolution"] == 100_000


def test_fail_soft_on_no_bar():
    assert parse_status(["just some output", "no status here"]) is None
    assert parse_status([]) is None
