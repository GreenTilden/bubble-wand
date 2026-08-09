"""Layer B — pane exclusion at the bridge, so every client inherits it.

WHY THIS EXISTS: the Mini App has filtered panes since it shipped, which
protected the phone and nothing else. The watch talks to this bridge directly,
so it saw every pane in the window regardless. A filter living in one of two
clients filters that client, not the surface.

The default is EMPTY and that is deliberate — this repo is public, so the
mechanism ships and the patterns stay in the deployed unit. Which means the
inertness test below is not a formality: an unset env var here fails OPEN and
silently, so "the code is present" is not evidence the filter is running. The
live probe against the deployed service is what proves it.
"""
import pytest
from fastapi.testclient import TestClient

from clawatch_bridge import main, pane_filter, tmux
from clawatch_bridge.config import settings

# index -> (command, title, cwd)
PANES = {
    1: ("node", "claude — building", "/home/darney/projects/dellatech"),
    2: ("node", "claude — reconciling", "/home/darney/projects/secret-project"),
    3: ("node", "SECRET design review", "/home/darney/projects/scratch"),
}


def _list_panes_output(fmt: str) -> str:
    """Render the two -F formats tmux.py asks for, from one fixture."""
    rows = []
    for idx, (command, title, cwd) in sorted(PANES.items()):
        if "pane_id" in fmt and "pane_current_command" not in fmt:
            rows.append(f"{idx}\t%4{idx}")
        elif "pane_current_command" in fmt and "pane_id" in fmt:
            rows.append(f"{idx}\t{command}\t{title}\t%4{idx}\t{cwd}")
        elif "pane_current_command" in fmt:
            rows.append(f"{idx}\t{command}\t{title}\t{cwd}")
        else:
            rows.append(str(idx))
    return "\n".join(rows)


@pytest.fixture
def patterns(monkeypatch):
    def _set(*pats):
        monkeypatch.setattr(settings, "excluded_pane_patterns", tuple(pats))
    return _set


@pytest.fixture
def client(monkeypatch):
    def fake_run(args, **kw):
        if args and args[0] == "list-panes":
            return _list_panes_output(args[args.index("-F") + 1] if "-F" in args else "")
        return ""
    monkeypatch.setattr(tmux, "_run", fake_run)
    monkeypatch.setattr(tmux, "_do_capture", lambda *a, **k: ["a line"])
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: ["a line"])
    monkeypatch.setattr(tmux, "send", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "send_key", lambda *a, **k: None)
    c = TestClient(main.app, raise_server_exceptions=False)
    c.headers.update({"Authorization": f"Bearer {settings.token}"})
    return c


# --- the matcher ------------------------------------------------------------

def test_matches_across_every_exposed_text_field():
    for field in ("title", "label", "repo", "command"):
        assert pane_filter.is_excluded({field: "secret-project"}, ("secret",)), field


def test_match_is_case_insensitive_both_ways():
    assert pane_filter.is_excluded({"title": "SECRET design review"}, ("secret",))
    assert pane_filter.is_excluded({"title": "secret design review"}, ("SECRET",))


def test_no_patterns_means_no_exclusion():
    """The shipped default. Nothing matches, nothing is hidden."""
    assert not pane_filter.is_excluded({"title": "secret"}, ())


def test_unrelated_pane_is_untouched():
    assert not pane_filter.is_excluded({"title": "claude — building", "repo": "dellatech"},
                                       ("secret", "private"))


# --- the list ---------------------------------------------------------------

def test_excluded_panes_are_absent_from_the_list(client, patterns):
    patterns("secret")
    got = client.get("/api/threads").json()["threads"]
    assert [t["index"] for t in got] == [1]


def test_empty_default_exposes_everything(client, patterns):
    """Inertness is the guarantee that makes an empty default safe to ship."""
    patterns()
    got = client.get("/api/threads").json()["threads"]
    assert [t["index"] for t in got] == [1, 2, 3]


def test_excluded_pane_contents_are_never_captured(client, patterns, monkeypatch):
    """The point of the filter is not to LOOK at the pane. Filtering the finished
    list would still have read its visible contents in to parse the status line."""
    seen = []
    monkeypatch.setattr(tmux, "_do_capture",
                        lambda target, *a, **k: seen.append(target) or ["a line"])
    patterns("secret")
    client.get("/api/threads")
    assert not any(t.endswith(".2") or t.endswith(".3") for t in seen), seen


# --- the addressed routes ---------------------------------------------------

# Derived from the app rather than restated, so a new index-addressed route is
# covered the moment it is added — a hand-kept list is exactly how the route that
# needs the guard most ends up being the one missing from it.
INDEX_ROUTES = sorted(
    (m.lower(), getattr(r, "path").replace("{index}", "{i}").replace("{wash_id}", "abc123"))
    for r in main.app.routes
    if "/api/threads/{index}" in getattr(r, "path", "")
    for m in getattr(r, "methods", set()) or set()
    if m.lower() in ("get", "post")
)


@pytest.mark.parametrize("method,path", INDEX_ROUTES)
def test_excluded_index_404s_on_every_addressed_route(client, patterns, method, path):
    """Dropping a pane from the LIST hides it and nothing more — every route here
    is addressed by index, so a client that knows the number reaches it anyway."""
    patterns("secret")
    kw = {"json": {"text": "hi"}} if method == "post" else {}
    r = getattr(client, method)(path.format(i=2), **kw)
    assert r.status_code == 404, (path, r.status_code, r.text)


def test_excluded_and_nonexistent_are_indistinguishable(client, patterns):
    """A distinct status OR message for 'excluded' would confirm the pane exists,
    which is the fact being withheld. Both paths raise through
    tmux._missing_pane_msg, so this asserts they still share it rather than
    comparing two format strings by eye."""
    patterns("secret")
    excluded = client.get("/api/threads/2/tail")
    assert excluded.status_code == 404
    assert excluded.json()["detail"] == tmux._missing_pane_msg(2)


def test_the_ordinary_missing_pane_path_uses_the_same_message(client, patterns):
    """The other half of the pair — if _pane_target's wording drifts, exclusion
    becomes distinguishable again and this fails."""
    patterns()
    with pytest.raises(ValueError) as e:
        tmux._pane_target(9)
    assert str(e.value) == tmux._missing_pane_msg(9)


def test_allowed_index_still_works(client, patterns):
    patterns("secret")
    assert client.get("/api/threads/1/tail").status_code == 200


# --- the coverage guard -----------------------------------------------------

def test_every_index_addressed_route_checks_exclusion():
    """Unlike the identity guard, this one has NO exemptions: any route taking an
    index can confirm an excluded pane exists, including one that only polls."""
    missing = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        if "/api/threads/{index}" not in path:
            continue
        deps = [d.call for d in getattr(route.dependant, "dependencies", [])]
        if main.require_pane_allowed not in deps:
            missing.append(path)
    assert not missing, (
        f"{missing} take a pane index but never check exclusion — an excluded "
        f"pane stays reachable by anyone who knows its number")
