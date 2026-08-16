"""An index is not an identity — enforced at the routes, not just inside wash.

WHY THIS EXISTS: tmux pane indices are POSITIONAL and are renumbered when a pane
is killed. Proven empirically rather than inferred (cycle-66 L17): four
panes %45 %46 %47 %48 at indices 1-4, kill index 2, and the pane that was index 3
(%47) BECOMES index 2. Every route here addresses by index, so after a close or a
split on the desktop, a phone still holding index 3 silently reads — and, worse,
TYPES INTO — a different Claude session than the one named on its screen.

wash.py already knew this ("Guard 2. An index is not an identity — re-verify
before every keystroke") and guarded only itself. send/key/submit-menu are the
same keystroke risk with none of the protection.

THE SHAPE THAT MATTERS: `paneId` is an ASSERTION THE SERVER CHECKS, never a
target it uses. Nothing builds a tmux target from a client-supplied string, so
"this bridge only ever touches settings.tmux_window" stays true by construction —
a pane id from another session is simply absent from the map and mismatches like
any other. A retarget-by-id design would have been friendlier and would have
handed a client the ability to name a pane outside the window.
"""
import inspect
import re

import pytest
from fastapi.testclient import TestClient

from clawatch_bridge import main, tmux
from clawatch_bridge.config import settings

PANES = {1: "%45", 2: "%46", 3: "%47"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(tmux, "_pane_id_map", lambda: dict(PANES))
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: ["a line"])
    monkeypatch.setattr(tmux, "capture_page", lambda *a, **k: (["a line"], False))
    monkeypatch.setattr(tmux, "send", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "send_key", lambda *a, **k: None)
    # pane_address reaches LIVE tmux, and this fixture did not fake it -- so these
    # tests silently depended on the developer's own tmux session having an index 3
    # under the configured scope. It did, until the panes-to-windows split (L21)
    # left dev:1 holding one pane: two tests about the identity GATE began failing
    # with 404 for a reason that has nothing to do with identity, in a file whose
    # fixture fakes everything else the route touches. The tail route grew this call
    # in the same commit that widened the scope, which is why "140 green" was true
    # when L21 measured it and false an hour later, with no code change in between.
    monkeypatch.setattr(tmux, "pane_address", lambda index: f"dev:1.{index}")
    c = TestClient(main.app, raise_server_exceptions=False)
    c.headers.update({"Authorization": f"Bearer {settings.token}"})
    return c


# --- the assertion itself ---------------------------------------------------


def test_no_pane_id_is_a_no_op(monkeypatch):
    """The watch sends none and must be unaffected. Also: no list-panes call at
    all, so the existing client pays nothing for a check it does not use."""
    called = []
    monkeypatch.setattr(tmux, "_pane_id_map", lambda: called.append(1) or {})
    tmux.assert_pane_identity(3, None)
    assert called == [], "identity check ran a tmux command for a caller that sent no id"


def test_matching_id_passes(monkeypatch):
    monkeypatch.setattr(tmux, "_pane_id_map", lambda: dict(PANES))
    tmux.assert_pane_identity(3, "%47")


def test_renumbered_pane_is_caught(monkeypatch):
    """The real scenario: index 2 was killed, so %47 slid from index 3 to 2. A
    client still asking for index 3 must not be served %48's session."""
    monkeypatch.setattr(tmux, "_pane_id_map", lambda: {1: "%45", 2: "%47", 3: "%48"})
    with pytest.raises(tmux.PaneIdentityError):
        tmux.assert_pane_identity(3, "%47")


def test_identity_error_is_not_a_valueerror():
    """Routes map ValueError to 404 ("gone"). "Moved" is a different fact with a
    different client repair, so it must not be swallowed by those handlers."""
    assert not issubclass(tmux.PaneIdentityError, ValueError)


def test_a_pane_id_is_never_used_as_a_target(monkeypatch):
    """The security property, asserted rather than trusted to review: whatever a
    client sends, the tmux target is built from the server's own enumeration.

    Still positional after the scope widened to whole sessions -- the window now
    comes from tmux's answer for that pane rather than from the scope string,
    which is a different SOURCE for the same shape, not a retarget-by-id."""
    monkeypatch.setattr(tmux, "_enumerate", lambda: [
        {"index": i, "session_name": "dev", "window_index": 1, "pane_index": i,
         "pane_id": pid, "command": "node", "path": "", "title": ""}
        for i, pid in PANES.items()
    ])
    target = tmux._pane_target(3)
    assert target == "dev:1.3"
    assert "%" not in target


# --- over HTTP --------------------------------------------------------------


def test_stale_pane_id_409s_on_read(client):
    r = client.get("/api/threads/3/tail?lines=40&paneId=%25999")
    assert r.status_code == 409, r.text
    assert "layout changed" in r.json()["detail"]


def test_stale_pane_id_409s_on_write(client):
    """The one that matters. A keystroke going to the wrong session is the whole
    reason this exists; a wrong tail is only embarrassing."""
    r = client.post("/api/threads/3/send", json={"text": "rm -rf something", "submit": True},
                    params={"paneId": "%999"})
    assert r.status_code == 409, r.text


def test_correct_pane_id_is_served(client):
    assert client.get("/api/threads/3/tail?lines=40&paneId=%2547").status_code == 200


def test_omitted_pane_id_still_works(client):
    """The watch's exact request shape."""
    assert client.get("/api/threads/3/tail?lines=40").status_code == 200


# --- the coverage guard -----------------------------------------------------

# Addressed by wash_id, which IS an identity, and guarded per-keystroke inside
# wash.py. A 409 here would break status polling for a wash that is proceeding
# correctly.
IDENTITY_EXEMPT = {"/api/threads/{index}/wash/{wash_id}"}


def test_every_index_addressed_route_checks_identity():
    """The general guard. Adding a route that takes {index} and forgetting the
    dependency is exactly how this class of defect returns, and it returns
    SILENTLY -- the route works perfectly until the day the layout shifts."""
    missing = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        if "/api/threads/{index}" not in path or path in IDENTITY_EXEMPT:
            continue
        deps = [d.call for d in getattr(route.dependant, "dependencies", [])]
        if main.require_pane_identity not in deps:
            missing.append(path)
    assert not missing, (
        f"{missing} take a pane index but never verify it still points at the "
        f"caller's pane — a renumber will silently retarget them")


def test_tmux_failure_during_the_check_is_a_502(monkeypatch):
    """Running the check as a route DEPENDENCY puts it outside the per-route
    `except TmuxError -> 502` handlers, so the same underlying failure came back
    500 until this was pinned. The client maps 502 to "could not reach the
    bridge"; a 500 reads as a bug in the bridge itself."""
    def boom():
        raise tmux.TmuxError("tmux is gone")
    monkeypatch.setattr(tmux, "_pane_id_map", boom)
    c = TestClient(main.app, raise_server_exceptions=False)
    c.headers.update({"Authorization": f"Bearer {settings.token}"})
    assert c.get("/api/threads/3/tail?lines=40&paneId=%2547").status_code == 502
