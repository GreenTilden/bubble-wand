"""The scope may be a whole session, and an index is an ORDINAL over it.

WHY THIS EXISTS: every pane in a tmux window shares one width, and the window is
sized by the clients viewing it. Five Claude panes tiled in one window are
therefore five threads whose print width is set by whoever attached last — a
53-column phone divides to ~10 columns each. That width is not recoverable
later: Claude hard-wraps its own output with real newlines at the width it had
when it printed, so no resize, no `capture-pane -J`, and no zoom-on-read reflows
it. Verified on a live pane (della cycle-66): every prose line ended at ≤51
chars with a real newline and -J changed nothing but trailing spaces.

One pane per window decouples them — tmux sizes each window independently from
the clients actually viewing it, proven live on fenton where dev:1 was 200x66 and
dev:2 was 189x61 in the same session at the same moment. Which is only useful if
the bridge can see more than one window, hence this.

THE TRAP THIS FILE GUARDS: `pane_index` restarts at 1 in every window. Five
one-pane windows are five different panes all called 1. Enumerating on tmux's
numbering would have produced a thread list that looked completely right while
every index addressed the wrong pane — the failure would have been invisible in
the list and only visible in where the keystrokes landed.
"""
import pytest
from fastapi.testclient import TestClient

from clawatch_bridge import main, tmux
from clawatch_bridge.config import Settings, settings


def _row(session, window, pane, pane_id, command="node", path="", title=""):
    """One tmux list-panes line in tmux._PANE_FORMAT's field order."""
    return "\t".join([session, str(window), str(pane), pane_id, command, path, title])


# Five windows, one pane each — the layout this change exists to serve. Note
# every pane_index is 1: that is the whole trap.
FIVE_WINDOWS = "\n".join(
    _row("dev", w, 1, f"%4{w}", title=f"claude — w{w}") for w in range(1, 6)
)

# The layout as it stands today: one window, five panes.
ONE_WINDOW = "\n".join(
    _row("dev", 1, p, f"%4{p}", title=f"claude — p{p}") for p in range(1, 6)
)


@pytest.fixture
def panes(monkeypatch):
    """Serve a fixed list-panes body; assert nothing else shells out to tmux."""
    def _use(body, scope):
        monkeypatch.setattr(settings, "tmux_scope", scope)

        def fake_run(args, **kw):
            assert args and args[0] == "list-panes", f"unexpected tmux call: {args}"
            return body

        monkeypatch.setattr(tmux, "_run", fake_run)

    return _use


# --- scope form -------------------------------------------------------------


def test_a_colon_means_one_window(monkeypatch):
    monkeypatch.setattr(settings, "tmux_scope", "dev:1")
    assert tmux._scope_args() == ["-t", "dev:1"]


def test_a_bare_name_means_the_whole_session(monkeypatch):
    """-s is what widens list-panes past the current window. Without it a
    session target silently returns only the ACTIVE window's panes, which reads
    as 'the feature works, there just aren't many panes'."""
    monkeypatch.setattr(settings, "tmux_scope", "dev")
    assert tmux._scope_args() == ["-s", "-t", "dev"]


def test_the_legacy_env_var_still_selects_the_scope(monkeypatch):
    """A deployed unit carries CLAWATCH_TMUX_WINDOW and must not change
    behaviour on restart. The rename is additive, not a migration."""
    monkeypatch.delenv("CLAWATCH_TMUX_SCOPE", raising=False)
    monkeypatch.setenv("CLAWATCH_TMUX_WINDOW", "dev:1")
    assert Settings().tmux_scope == "dev:1"

    monkeypatch.setenv("CLAWATCH_TMUX_SCOPE", "dev")
    assert Settings().tmux_scope == "dev", "explicit scope must win over the legacy name"


# --- the ordinal ------------------------------------------------------------


def test_ordinals_do_not_collide_across_windows(panes):
    """The trap, asserted directly: five panes that are all pane_index 1 must
    enumerate as five distinct indices addressing five distinct panes."""
    panes(FIVE_WINDOWS, "dev")
    rows = tmux._enumerate()
    assert [r["index"] for r in rows] == [1, 2, 3, 4, 5]
    assert [r["pane_id"] for r in rows] == ["%41", "%42", "%43", "%44", "%45"]
    assert {r["pane_index"] for r in rows} == {1}, "fixture no longer exercises the collision"


def test_each_ordinal_targets_its_own_window(panes):
    panes(FIVE_WINDOWS, "dev")
    assert [tmux._pane_target(i) for i in range(1, 6)] == [
        "dev:1.1", "dev:2.1", "dev:3.1", "dev:4.1", "dev:5.1"
    ]


def test_the_target_never_carries_a_pane_id(panes):
    """L17's property, re-asserted at the wider scope: targets stay positional
    and server-built. Widening the scope must not become a retarget-by-id."""
    panes(FIVE_WINDOWS, "dev")
    for i in range(1, 6):
        assert "%" not in tmux._pane_target(i)


def test_pane_id_map_is_keyed_by_ordinal_not_pane_index(panes):
    """Keying on pane_index would collapse all five windows onto key 1 — and the
    identity guard would then pass for the wrong pane, which is worse than no
    guard because it reports success."""
    panes(FIVE_WINDOWS, "dev")
    assert tmux._pane_id_map() == {1: "%41", 2: "%42", 3: "%43", 4: "%44", 5: "%45"}


def test_ordering_is_imposed_not_inherited_from_tmux(panes):
    """A client's ordinal must not depend on list-panes' output order."""
    shuffled = "\n".join([
        _row("dev", 3, 1, "%43"), _row("dev", 1, 2, "%42"),
        _row("dev", 5, 1, "%45"), _row("dev", 1, 1, "%41"),
    ])
    panes(shuffled, "dev")
    rows = tmux._enumerate()
    assert [(r["window_index"], r["pane_index"]) for r in rows] == [
        (1, 1), (1, 2), (3, 1), (5, 1)
    ]
    assert [r["pane_id"] for r in rows] == ["%41", "%42", "%43", "%45"]


# --- today's layout is untouched --------------------------------------------


def test_one_window_scope_is_byte_identical_to_before(panes):
    """The deployed default. Ordinal and pane_index coincide here, which is why
    the distinction stayed invisible for as long as it did."""
    panes(ONE_WINDOW, "dev:1")
    rows = tmux._enumerate()
    assert [r["index"] for r in rows] == [1, 2, 3, 4, 5]
    assert [r["index"] == r["pane_index"] for r in rows] == [True] * 5
    assert [tmux._pane_target(i) for i in range(1, 6)] == [f"dev:1.{i}" for i in range(1, 6)]


# --- the renumber hazard, generalised ---------------------------------------


def test_closing_a_window_shifts_every_later_ordinal(panes):
    """The known pane-close hazard now has a second source: under a session scope
    the ordinal spans windows, so closing WINDOW 2 renumbers 3-5 as well. Same
    guard, wider blast radius — asserted so the widening is a documented
    consequence rather than a surprise found by typing into the wrong pane."""
    panes("\n".join(_row("dev", w, 1, f"%4{w}") for w in (1, 3, 4, 5)), "dev")
    assert tmux._pane_id_map() == {1: "%41", 2: "%43", 3: "%44", 4: "%45"}
    # A client still holding index 3 for %44's predecessor is caught, not served.
    with pytest.raises(tmux.PaneIdentityError):
        tmux.assert_pane_identity(3, "%43")
    tmux.assert_pane_identity(2, "%43")  # re-pointed correctly, passes


def test_a_missing_ordinal_names_the_scope(panes):
    panes(FIVE_WINDOWS, "dev")
    with pytest.raises(ValueError) as e:
        tmux._pane_target(9)
    assert str(e.value) == tmux._missing_pane_msg(9)
    assert "dev" in str(e.value)


# --- over HTTP --------------------------------------------------------------


def test_tail_reports_the_resolved_address_not_the_scope(monkeypatch):
    """The `pane` field is what a client shows to confirm WHICH pane is on
    screen. Formatted from config it would read "dev.3" — an address that does
    not exist — for the pane actually living at dev:3.1."""
    monkeypatch.setattr(settings, "tmux_scope", "dev")
    monkeypatch.setattr(tmux, "_run", lambda args, **kw: FIVE_WINDOWS)
    monkeypatch.setattr(tmux, "_do_capture", lambda *a, **k: ["a line"])
    c = TestClient(main.app, raise_server_exceptions=False)
    c.headers.update({"Authorization": f"Bearer {settings.token}"})

    r = c.get("/api/threads/3/tail?lines=10")
    assert r.status_code == 200, r.text
    assert r.json()["pane"] == "dev:3.1"


def test_threads_list_spans_windows(monkeypatch):
    monkeypatch.setattr(settings, "tmux_scope", "dev")
    monkeypatch.setattr(tmux, "_run", lambda args, **kw: FIVE_WINDOWS)
    monkeypatch.setattr(tmux, "_do_capture", lambda *a, **k: ["a line"])
    c = TestClient(main.app, raise_server_exceptions=False)
    c.headers.update({"Authorization": f"Bearer {settings.token}"})

    threads = c.get("/api/threads").json()["threads"]
    assert [t["index"] for t in threads] == [1, 2, 3, 4, 5]
    assert [t["pane"] for t in threads] == [f"dev:{w}.1" for w in range(1, 6)]
    assert [t["paneId"] for t in threads] == [f"%4{w}" for w in range(1, 6)]
