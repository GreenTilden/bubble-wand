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
