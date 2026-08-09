"""Pane exclusion at the bridge -- the layer every client inherits.

The Mini App has carried its own copy of this logic since it shipped, which
protected the phone and nothing else: the watch talks to this bridge directly,
so it saw every pane in the window regardless. A filter that lives in one of two
clients is not a filter on the surface, it is a filter on one client.

DEFAULT EMPTY, ON PURPOSE. This repo is public. Shipping the actual patterns as
a literal default would publish the very tokens the exclusion exists to keep off
a screen -- the mechanism is the reusable part, the pattern list is local
configuration. Set CLAWATCH_EXCLUDED_PANE_PATTERNS (comma-separated) in the
deployed unit. Empty means inert: no pattern, no exclusion, byte-identical
behaviour to before this module existed.

Because the default is inert, the script is not the guard -- the WIRING is. An
unset env var here fails open and silently. Verify against the running service,
not against this file.

This is defense-in-depth, not a structural guarantee: a pane whose title, cwd,
and command never literally contain a listed string slips through. The durable
control remains keeping that work in a tmux session outside the configured
window entirely.
"""
from __future__ import annotations

# The text fields a thread actually exposes (clawatch_bridge/models.py::Thread).
# repo is a cwd BASENAME, never a full path -- matching what the thread carries,
# not what tmux knows, so the filter can never be more revealing than the payload.
_CHECKED_FIELDS = ("title", "label", "repo", "command")


def is_excluded(thread: dict, patterns: tuple[str, ...]) -> bool:
    """Case-insensitive substring match over the thread's text fields."""
    if not patterns:
        return False
    text = " ".join(str(thread.get(f) or "") for f in _CHECKED_FIELDS).lower()
    return any(p.lower() in text for p in patterns if p)
