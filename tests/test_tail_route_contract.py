"""The tail ROUTE's contract — not `tmux.capture`'s.

WHY THIS EXISTS: `history` shipped in L15 with seven green tests, and on the live
service it did nothing at all. Every one of those tests called `_do_capture` or
inspected `capture`'s signature; not one went through HTTP. `get_tail` never
declared the parameter, FastAPI drops unknown query params in silence, and so a
client asking `?lines=100&history=true` got the visible pane back — 200 OK, no
warning, the exact silent-shrink failure the feature was built to end.

This is the same boundary L14.1 caught on `UsageResponse` (pydantic dropping
undeclared keys). Same lesson, opposite direction: there, the response model ate
outbound fields; here, the route signature ate inbound ones. Function-level tests
cannot see either.

So these tests drive the app. `test_every_capture_knob_is_reachable_over_http` is
the general guard — it derives its expectations from `tmux.capture`'s own
signature, so the NEXT knob added to capture and forgotten on the route fails
here instead of shipping as a no-op.

della cycle-66 L16 · found while deploying L15.
"""
import inspect

import pytest
from fastapi.testclient import TestClient

from clawatch_bridge import main, tmux
from clawatch_bridge.config import settings

# Knobs that are deliberately server-side only, with the reason they are not
# client-controllable. Anything NOT listed here must be reachable over HTTP.
NOT_CLIENT_FACING = {
    "index",  # path parameter, not a query knob
    "clean",  # chrome-stripping is the bridge's job; a client must not disable it
}


@pytest.fixture
def client(monkeypatch):
    seen = {}

    def fake_capture(index, lines, scrollback, clean=True, ansi=False, history=False):
        seen.update(index=index, lines=lines, scrollback=scrollback,
                    clean=clean, ansi=ansi, history=history)
        return ["captured line"]

    monkeypatch.setattr(tmux, "capture", fake_capture)
    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {settings.token}"})
    return c, seen


def test_history_true_reaches_tmux(client):
    """The defect, stated directly: the route must forward what the caller asked."""
    c, seen = client
    assert c.get("/api/threads/1/tail?lines=100&history=true").status_code == 200
    assert seen["history"] is True, (
        "the route accepted history=true and called tmux without it — FastAPI "
        "drops undeclared query params silently, so the client sees 200 and a "
        "screen-sized tail")
    assert seen["lines"] == 100


def test_history_defaults_off_over_http(client):
    """The watch's poll sends no `history`; it must keep getting today's screen."""
    c, seen = client
    assert c.get("/api/threads/1/tail?lines=40").status_code == 200
    assert seen["history"] is False


def test_every_capture_knob_is_reachable_over_http():
    """The GENERAL guard. Derived from capture()'s signature rather than a hand
    list, so a knob added there and forgotten on the route fails here — which is
    exactly how `history` reached production as a no-op."""
    knobs = {
        name for name in inspect.signature(tmux.capture).parameters
        if name not in NOT_CLIENT_FACING
    }
    exposed = set(inspect.signature(main.get_tail).parameters)
    missing = knobs - exposed
    assert not missing, (
        f"tmux.capture takes {sorted(missing)} but /api/threads/{{index}}/tail "
        f"does not accept them — a client asking for them gets 200 and silence")


# --- paging (L22) ---------------------------------------------------------
#
# Same boundary, checked before shipping this time rather than after: the paging
# maths is unit-tested in test_tail_paging.py, and NONE of that would catch the
# route forgetting to declare `before`. The failure would look identical to the
# L15 one -- 200 OK, and every "older" tap returning the live tail.


@pytest.fixture
def paging_client(monkeypatch):
    """Fakes BOTH capture paths so a test can assert which one the route chose."""
    seen = {}

    def fake_capture(index, lines, scrollback, clean=True, ansi=False, history=False):
        seen.update(path="capture", index=index, lines=lines, history=history)
        return ["live tail line"]

    def fake_capture_page(index, lines, before, ansi=False):
        seen.update(path="capture_page", index=index, lines=lines,
                    before=before, ansi=ansi)
        return (["older line"], True)

    monkeypatch.setattr(tmux, "capture", fake_capture)
    monkeypatch.setattr(tmux, "capture_page", fake_capture_page)
    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {settings.token}"})
    return c, seen


def test_before_reaches_tmux_as_a_page_request(paging_client):
    c, seen = paging_client
    r = c.get("/api/threads/1/tail?lines=150&before=300&ansi=true")
    assert r.status_code == 200
    assert seen["path"] == "capture_page"
    assert seen["before"] == 300
    assert seen["lines"] == 150


def test_has_older_and_before_are_in_the_response_body(paging_client):
    """pydantic drops undeclared response keys silently (L14.1). A client whose
    'older' button never disables is the visible symptom of that, and it looks
    like a UI bug rather than a schema one."""
    c, _ = paging_client
    body = c.get("/api/threads/1/tail?lines=150&before=300").json()
    assert body["hasOlder"] is True
    assert body["before"] == 300


def test_before_zero_takes_the_untouched_live_path(paging_client):
    """The watch and the 4s poll both send no `before`. They must not be routed
    through the paging branch at all -- a paging bug can then only break paging."""
    c, seen = paging_client
    body = c.get("/api/threads/1/tail?lines=40").json()
    assert seen["path"] == "capture"
    assert body["before"] == 0
    assert body["hasOlder"] is False


def test_negative_before_is_a_422_not_a_silent_zero(paging_client):
    c, _ = paging_client
    assert c.get("/api/threads/1/tail?lines=150&before=-1").status_code == 422


# --- the history route (L22) ----------------------------------------------
#
# Same reason as everything above it: the paging maths and the transcript reader
# are unit-tested, and none of that would notice the route dropping `before` or
# the response model eating `confidence`. Confidence is the one that matters --
# a client that cannot see it presents a guessed session as this pane's history.


@pytest.fixture
def history_client(monkeypatch):
    seen = {}

    def fake_page(cwd, pane_text, lines, before, pane_key=None):
        seen.update(cwd=cwd, lines=lines, before=before, pane_text=pane_text,
                    pane_key=pane_key)
        return (["older line"], True, {"session": "abc123", "confidence": "matched"})

    monkeypatch.setattr(tmux, "pane_cwd", lambda index: "/home/x/repo")
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: ["screen line"])
    monkeypatch.setattr(tmux, "pane_address", lambda index: f"dev:1.{index}")
    monkeypatch.setattr(tmux, "pane_id", lambda index: "%42")
    monkeypatch.setattr(main.transcript, "page", fake_page)
    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {settings.token}"})
    return c, seen


def test_history_forwards_before_and_returns_the_page(history_client):
    c, seen = history_client
    body = c.get("/api/threads/2/history?lines=50&before=100").json()
    assert seen["before"] == 100
    assert seen["lines"] == 50
    assert body["lines"] == ["older line"]
    assert body["hasOlder"] is True


def test_history_keys_the_transcript_cache_on_the_pane_id(history_client):
    """The live view polls this route every 4s, so the pick is memoised -- and the
    key has to be the pane, not the repo. Two panes in one repo is the normal case;
    a cache keyed on cwd alone would hand the second pane the first one's session,
    reaching the exact privacy failure the matcher exists to prevent, by a route
    that bypasses the matcher entirely."""
    c, seen = history_client
    c.get("/api/threads/2/history")
    assert seen["pane_key"] == "%42"


def test_history_reports_which_session_and_how_sure(history_client):
    c, _ = history_client
    body = c.get("/api/threads/2/history").json()
    assert body["session"] == "abc123"
    assert body["confidence"] == "matched"


def test_history_reads_the_cwd_server_side(history_client):
    """No client-supplied path reaches the filesystem, and the screen used to
    disambiguate is captured here rather than sent -- otherwise a caller could
    fish for a transcript by guessing at its contents."""
    c, seen = history_client
    c.get("/api/threads/2/history")
    assert seen["cwd"] == "/home/x/repo"
    assert seen["pane_text"] == "screen line"


def test_history_on_a_missing_pane_is_404(monkeypatch):
    def boom(index):
        raise ValueError("pane index 9 does not exist in dev:1")
    monkeypatch.setattr(tmux, "pane_cwd", boom)
    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {settings.token}"})
    assert c.get("/api/threads/9/history").status_code == 404
