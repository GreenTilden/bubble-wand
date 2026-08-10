"""Paged scrollback — walking back through the buffer instead of enlarging the tail.

`history=True` gives a DEEPER tail: always anchored to the bottom, capped at 500 by
the route. It answers "show me more of the end". It does not answer "show me what
came before that", and on a phone that is the question — the depth ladder tops out
at 500 and every rung re-downloads the whole tail on a 4s poll.

`before=N` ends the window N rows above the newest row, so page 2 costs exactly what
page 1 cost. The anchor is the newest ROW and deliberately not the visible pane: the
visible pane is the pane HEIGHT, which the panes-to-windows split (L21) changed for
every pane at once. Page boundaries measured against it would have moved under the
operator mid-read.

The subtle one is has_older. A short page and the top of the buffer are
indistinguishable from outside, so the client cannot infer it — the bridge captures
one row PAST the window, reports whether it existed, and discards it.

della cycle-66 L22 · paged scrollback.
"""
import pytest

from clawatch_bridge import tmux


def _rows(n, start=0):
    """n rows, oldest first — `row 0` is the oldest, matching capture order."""
    return [f"row {i}" for i in range(start, start + n)]


def _page(monkeypatch, buffer_rows, lines, before, ansi=False):
    """Drive capture_page against a fake tmux holding `buffer_rows`.

    _do_capture is faked at the level BELOW the paging arithmetic: it honours the
    row count it is asked for, tail-anchored, exactly as the real one does with
    clean=False. RAW rows -- capture_page does its own cleaning, so a buffer fed
    here can contain blanks and chrome and the shrink is real. That keeps these
    tests about the paging maths rather than about capture-pane's flags, which
    test_tail_history covers.
    """
    seen = {}

    def fake_do_capture(target, n, scrollback, clean, ansi_flag=False, history=False):
        seen.setdefault("asked_all", []).append(n)
        seen["asked"] = seen["asked_all"][0]
        seen["history"] = history
        seen["ansi"] = ansi_flag
        assert clean is False, (
            "capture_page must take RAW rows and clean them itself -- cleaning "
            "inside _do_capture happens after its slice, which is what made the "
            "first version page in a row space the client never sees")
        return buffer_rows[-n:] if n and n > 0 else list(buffer_rows)

    monkeypatch.setattr(tmux, "_pane_target", lambda i: f"dev:{i}.1")
    monkeypatch.setattr(tmux, "_do_capture", fake_do_capture)
    got, has_older = tmux.capture_page(1, lines=lines, before=before, ansi=ansi)
    return got, has_older, seen


def test_before_zero_returns_the_newest_rows(monkeypatch):
    got, _, _ = _page(monkeypatch, _rows(500), lines=150, before=0)
    assert got == _rows(150, start=350), "page 0 is the live tail"


def test_before_steps_back_by_exactly_that_many_rows(monkeypatch):
    """The property that makes paging composable: page N+1 begins where page N ended,
    with no gap and no repeat. A one-row overlap is the classic off-by-one here and
    reads as duplicated output rather than as a bug."""
    page0, _, _ = _page(monkeypatch, _rows(500), lines=150, before=0)
    page1, _, _ = _page(monkeypatch, _rows(500), lines=150, before=150)
    assert page1[-1] == "row 349"
    assert page0[0] == "row 350"
    assert set(page0).isdisjoint(page1), "pages must not overlap"


def test_pages_are_the_same_size_however_deep(monkeypatch):
    """The entire reason this exists rather than a bigger `lines`: cost is flat."""
    deep, _, _ = _page(monkeypatch, _rows(5000), lines=150, before=3000)
    assert len(deep) == 150


def test_has_older_true_when_the_buffer_continues_above(monkeypatch):
    _, has_older, _ = _page(monkeypatch, _rows(500), lines=150, before=150)
    assert has_older is True


def test_has_older_false_at_the_top_of_the_buffer(monkeypatch):
    """Exactly-consumed buffer: 200 rows, a 150-row window ending 50 back. Nothing
    above it, and the page is FULL — so a client counting returned rows would guess
    wrong. This is the case has_older exists for."""
    got, has_older, _ = _page(monkeypatch, _rows(200), lines=150, before=50)
    assert len(got) == 150
    assert has_older is False


def test_short_page_is_not_by_itself_the_end(monkeypatch):
    """The inverse trap: a page can be short because the buffer ran out mid-window,
    and that IS the top -- but the signal must come from has_older, not from len()."""
    got, has_older, _ = _page(monkeypatch, _rows(100), lines=150, before=20)
    assert len(got) < 150
    assert has_older is False


def test_paging_past_the_whole_buffer_is_empty_not_an_error(monkeypatch):
    """A client that keeps tapping 'older' at the top must get a quiet empty page.
    Raising here would turn the last tap of a normal read into an error toast."""
    got, has_older, _ = _page(monkeypatch, _rows(100), lines=150, before=500)
    assert got == []
    assert has_older is False


def test_it_asks_tmux_for_the_window_plus_the_sentinel(monkeypatch):
    """No extra tmux call for has_older: the probe row rides along in the same
    capture. lines + before + 1."""
    _, _, seen = _page(monkeypatch, _rows(5000), lines=150, before=300)
    assert seen["asked"] == 451


def test_paging_always_asks_for_history(monkeypatch):
    """Without history=True the capture is the visible pane, so every page above the
    first would silently return the same screen -- the failure this whole file is
    downstream of."""
    _, _, seen = _page(monkeypatch, _rows(500), lines=150, before=150)
    assert seen["history"] is True


def test_ansi_passes_through_to_the_capture(monkeypatch):
    """The Mini App reads in colour; a page that arrives stripped looks like a
    different pane than the one above it."""
    _, _, seen = _page(monkeypatch, _rows(500), lines=150, before=150, ansi=True)
    assert seen["ansi"] is True


def test_negative_before_is_rejected(monkeypatch):
    monkeypatch.setattr(tmux, "_pane_target", lambda i: f"dev:{i}.1")
    with pytest.raises(ValueError):
        tmux.capture_page(1, lines=150, before=-1)


# --- the shrink that cleaning causes ---------------------------------------
#
# Found on the LIVE pane a minute after the first version passed 11 green tests.
# Every one of those tests fed the fake a buffer of plain rows, so cleaning was a
# no-op and paging in raw-row space looked identical to paging in cleaned-row
# space. The real pane is not plain: _clean_tail drops chrome and collapses blank
# runs, so a 61-row raw capture arrived as 57 cleaned rows, skipping 40 left 17,
# and has_older read False on a buffer that visibly continued above.
#
# These tests feed a buffer that SHRINKS under cleaning, which is the only kind
# that can tell the two row spaces apart.


def _lumpy(n_content):
    """Content rows separated by runs of blanks, which _clean_tail collapses to
    one. Roughly the shape of real Claude output: prose in paragraphs."""
    rows = []
    for i in range(n_content):
        rows.append(f"row {i}")
        if i % 3 == 0:
            rows += ["", "", ""]   # a run of 3 -> collapses to 1
    return rows


def test_a_page_is_full_even_when_cleaning_ate_rows(monkeypatch):
    """The live failure, as a test. A window under-filled by cleaning shows the
    operator a short screen and, worse, moves the next page's boundary."""
    got, _, _ = _page(monkeypatch, _lumpy(400), lines=20, before=20)
    assert len(got) == 20


def test_has_older_survives_the_shrink(monkeypatch):
    """The half that actually misleads: a short page disables the 'older' control,
    so the operator is told they have reached the top of a buffer they have not."""
    _, has_older, _ = _page(monkeypatch, _lumpy(400), lines=20, before=20)
    assert has_older is True


def test_it_asks_deeper_rather_than_returning_short(monkeypatch):
    """Growth is the mechanism, so assert it happened -- a single request that got
    lucky on a plain buffer is what shipped the bug."""
    _, _, seen = _page(monkeypatch, _lumpy(400), lines=20, before=20)
    assert len(seen["asked_all"]) > 1
    assert seen["asked_all"][1] > seen["asked_all"][0]


def test_it_stops_asking_once_tmux_stops_giving(monkeypatch):
    """A short buffer must not spend the whole growth budget: tmux clamps -S at the
    start of the buffer, so a request returning no more raw rows than the last one
    is the top and the loop ends there."""
    _, has_older, seen = _page(monkeypatch, _lumpy(10), lines=150, before=0)
    assert has_older is False
    assert len(seen["asked_all"]) == 2, (
        "one request to find the buffer short, one to confirm it stopped growing")


def test_growth_is_bounded(monkeypatch):
    """A pane whose cleaning ratio is pathological must fail as a short page, not
    as a hung request -- this runs on an operator tap."""
    # Every row is chrome-adjacent blank filler, so cleaning always collapses far
    # below `want` no matter how deep we ask.
    buf = [""] * 50000
    _, _, seen = _page(monkeypatch, buf, lines=150, before=0)
    assert len(seen["asked_all"]) <= tmux._PAGE_GROWTH_TRIES
