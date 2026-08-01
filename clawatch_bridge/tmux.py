"""Injection-safe tmux operations — the highest-risk file in the bridge.

Security rules enforced here:
  * Never shell=True; never string-interpolate into a shell. Always argv lists.
  * The pane target is built server-side from an integer index the client sends,
    then verified against the live pane list. No user string reaches the -t arg.
  * Sending text uses `send-keys -l -- <text>` (literal), so key names like
    "Enter", "C-c", ";", "kill-server" in dictated text are inserted as plain
    characters, never interpreted. Submitting is a SEPARATE `send-keys Enter`.
"""
from __future__ import annotations

import re
import subprocess

from .config import settings

TMUX = "tmux"
_TIMEOUT = 5

# Braille block (U+2800–U+28FF) — Claude Code's working spinner.
_BRAILLE_LO, _BRAILLE_HI = 0x2800, 0x28FF
# Star-like glyphs Claude Code shows while awaiting user input.
_NEEDS_INPUT_GLYPHS = set("✳✻✽✶✷✵✴❋✱✲✧✦✺✹✸*")

# Box-drawing range (U+2500–U+257F) plus a few ASCII rule chars — used to spot
# pure border/divider lines that are just terminal chrome.
_BORDER_EXTRA = set("-—–_=│ ")
# A numbered menu option, optionally preceded by the selection cursor "❯"/">".
_OPTION_RE = re.compile(r"^\s*([❯>])?\s*(\d+)\.\s+(\S.*)$")
# Claude Code status bar, e.g. "Opus 4.8 · ▓▓▓ 311k ctx HARD · $22.12".
_STATUS_TOKENS_RE = re.compile(r"([\d.]+)\s*([kmKM]?)\s*ctx")
_STATUS_TIER_RE = re.compile(r"ctx\s+([A-Za-z]+)")
_STATUS_COST_RE = re.compile(r"\$\s*([\d.]+)")


class TmuxError(Exception):
    """A tmux command failed or returned unexpected output."""


def _run(args: list[str], timeout: int = _TIMEOUT) -> str:
    try:
        proc = subprocess.run(
            [TMUX, *args],
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:  # tmux not installed / not on PATH
        raise TmuxError("tmux not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise TmuxError(f"tmux timed out: {args}") from e
    if proc.returncode != 0:
        raise TmuxError(proc.stderr.strip() or f"tmux failed: {args}")
    return proc.stdout


def _derive_status(title: str) -> tuple[str, str, str]:
    """Return (glyph, status, label) from a pane title.

    label = title with the leading status glyph (and following space) stripped,
    for clean display on the watch.
    """
    t = title.strip()
    if not t:
        return "", "IDLE", ""
    glyph = t[0]
    cp = ord(glyph)
    if _BRAILLE_LO <= cp <= _BRAILLE_HI:
        status = "WORKING"
    elif glyph in _NEEDS_INPUT_GLYPHS:
        status = "NEEDS_INPUT"
    else:
        # No recognized status glyph — treat as idle, keep the whole title as label.
        return "", "IDLE", t
    label = t[1:].lstrip()
    return glyph, status, label


# ---- tail cleaning -------------------------------------------------------

def _is_border(line: str) -> bool:
    """True if the line is only box-drawing / rule characters (a divider)."""
    t = line.strip()
    if not t:
        return False
    for c in t:
        if c.isalnum():
            return False
        if not (0x2500 <= ord(c) <= 0x257F or c in _BORDER_EXTRA):
            return False
    return True


def _is_chrome(line: str) -> bool:
    """True if the line is Claude Code UI chrome we don't want on the watch:
    dividers, the model/context/cost status bar, the empty input caret, tips,
    the 'auto mode' hint, and the 'Esc to cancel' prompt footer.
    """
    t = line.strip()
    if not t:
        return False
    if _is_border(line):
        return True
    if "·" in t and "ctx" in t:          # e.g. "Opus 4.8 · ▓▓▓ 311k ctx HARD · $22.12"
        return True
    low = t.lower()
    if "auto mode on" in low:
        return True
    if t.startswith("⏵⏵"):
        return True
    if low.startswith("tip:"):
        return True
    if low.startswith("esc to cancel"):
        return True
    if t == "❯":                          # empty input box caret
        return True
    return False


def _clean_tail(lines: list[str]) -> list[str]:
    """Strip terminal chrome and collapse blank runs so the watch shows just
    the meaningful conversation text (and any active prompt options)."""
    out: list[str] = []
    for ln in lines:
        if _is_chrome(ln):
            continue
        if not ln.strip():
            if out and not out[-1].strip():
                continue  # collapse consecutive blanks
        out.append(ln.rstrip())
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def parse_prompt(cleaned: list[str]) -> dict | None:
    """Detect a Claude Code interactive menu in already-cleaned tail lines.

    Returns {"question": str, "options": [{"key","label","selected"}]} or None.
    Requires the selection cursor "❯" on a numbered option. Only the contiguous
    menu block anchored by that cursor is captured — numbered lists elsewhere in
    the tail (e.g. inside a plan, which often has several) are NOT swept in. Doing
    so previously produced bogus/duplicate options: duplicate option keys crash the
    watch's LazyColumn, and stray plan bullets would send the wrong digit on tap.
    """
    # Every numbered-option line, with its position in the tail.
    matches: list[tuple[int, dict]] = []
    for i, ln in enumerate(cleaned):
        m = _OPTION_RE.match(ln)
        if m:
            matches.append(
                (
                    i,
                    {
                        "key": m.group(2),
                        "label": m.group(3).strip(),
                        "selected": m.group(1) == "❯",
                    },
                )
            )
    # The live menu is anchored by the cursor; without one there is no prompt.
    sel = next((p for p, (_, o) in enumerate(matches) if o["selected"]), None)
    if sel is None:
        return None

    # Expand from the cursor across the contiguous run of option lines — adjacent
    # matches separated only by blank lines. Any non-blank line between two numbered
    # lines (plan prose, the question) breaks the run, so plan bullets stay out.
    def _only_blank_between(a: int, b: int) -> bool:
        return all(not cleaned[k].strip() for k in range(a + 1, b))

    lo = hi = sel
    while lo > 0 and _only_blank_between(matches[lo - 1][0], matches[lo][0]):
        lo -= 1
    while hi < len(matches) - 1 and _only_blank_between(matches[hi][0], matches[hi + 1][0]):
        hi += 1

    block = matches[lo : hi + 1]
    first_opt_idx = block[0][0]

    # Dedupe by key (belt-and-suspenders; a real menu already has unique digits).
    options: list[dict] = []
    seen: set[str] = set()
    for _, o in block:
        if o["key"] in seen:
            continue
        seen.add(o["key"])
        options.append(o)

    question = ""
    for j in range(first_opt_idx - 1, -1, -1):
        if cleaned[j].strip():
            question = cleaned[j].strip()
            break
    return {"question": question, "options": options}


def _valid_index(index: int) -> bool:
    return isinstance(index, int) and 1 <= index <= 99


def _current_indices() -> list[int]:
    out = _run(["list-panes", "-t", settings.tmux_window, "-F", "#{pane_index}"])
    return [int(x) for x in out.split()]


def _pane_target(index: int) -> str:
    """Build and validate a pane target. Raises ValueError on a bad/absent index."""
    if not _valid_index(index):
        raise ValueError(f"invalid pane index: {index!r}")
    if index not in _current_indices():
        raise ValueError(f"pane index {index} does not exist in {settings.tmux_window}")
    return f"{settings.tmux_window}.{index}"


def parse_status(lines: list[str]) -> dict | None:
    """Parse Claude Code's status bar (model · ▓ Nk ctx TIER · $X.XX) into
    structured fields for the watch's per-thread meter. Fail-soft: returns None
    if there is no status bar or it can't be parsed (format drift must never
    break capture).
    """
    for ln in lines:
        t = ln.strip()
        if "·" not in t or "ctx" not in t:
            continue
        m = _STATUS_TOKENS_RE.search(t)
        if not m:
            continue
        try:
            n = float(m.group(1))
        except ValueError:
            continue
        suffix = m.group(2).lower()
        if suffix == "k":
            tokens = int(n * 1_000)
        elif suffix == "m":
            tokens = int(n * 1_000_000)
        else:
            tokens = int(n)
        model = t.split("·", 1)[0].strip() or None
        tier_m = _STATUS_TIER_RE.search(t)
        tier = tier_m.group(1) if tier_m else None
        cost_m = _STATUS_COST_RE.search(t)
        cost = float(cost_m.group(1)) if cost_m else None
        return {"model": model, "ctxTokens": tokens, "ctxTier": tier, "costUsd": cost}
    return None


def list_threads() -> list[dict]:
    out = _run(
        [
            "list-panes",
            "-t",
            settings.tmux_window,
            "-F",
            "#{pane_index}\t#{pane_current_command}\t#{pane_title}",
        ]
    )
    threads: list[dict] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        idx = int(parts[0])
        command = parts[1] if len(parts) > 1 else ""
        title = parts[2] if len(parts) > 2 else ""
        glyph, status, label = _derive_status(title)
        # Tail-based prompt detection beats the title glyph: if a pane is parked at
        # an interactive menu, surface it as NEEDS_INPUT so it's never hidden
        # (whatever the spinner glyph reads) AND flag it as tappable so the watch can
        # distinguish "answer by tapping" from "needs dictated input".
        has_prompt = False
        meter: dict | None = None
        try:
            raw = _do_capture(f"{settings.tmux_window}.{idx}", 25, False, False)
            meter = parse_status(raw)
            if parse_prompt(_clean_tail(raw)):
                has_prompt = True
                status = "NEEDS_INPUT"
        except TmuxError:
            pass
        threads.append(
            {
                "index": idx,
                "pane": f"{settings.tmux_window}.{idx}",
                "command": command,
                "status": status,
                "glyph": glyph,
                "title": title,
                "label": label,
                "hasPrompt": has_prompt,
                "model": (meter or {}).get("model"),
                "ctxTokens": (meter or {}).get("ctxTokens"),
                "ctxTier": (meter or {}).get("ctxTier"),
                "costUsd": (meter or {}).get("costUsd"),
            }
        )
    return threads


def _do_capture(target: str, lines: int, scrollback: bool, clean: bool) -> list[str]:
    """Capture a pane by its already-built target (no re-validation)."""
    args = ["capture-pane", "-p", "-t", target]
    if scrollback:
        args += ["-S", "-"]  # from the start of the scrollback buffer
    out = _run(args)
    captured = out.splitlines()
    if not scrollback and lines and lines > 0:
        captured = captured[-lines:]
    if clean:
        captured = _clean_tail(captured)
    return captured


def capture(index: int, lines: int, scrollback: bool, clean: bool = True) -> list[str]:
    target = _pane_target(index)
    return _do_capture(target, lines, scrollback, clean)


# Fixed allowlist of control keys the watch may send. The client sends only the
# action name; the actual tmux key string is chosen here, so no user-controlled
# string ever reaches send-keys as a key name.
_KEY_MAP: dict[str, list[str]] = {
    "escape": ["Escape"],     # dismiss a menu / cancel
    "interrupt": ["C-c"],     # stop a running command
    "clear": ["C-u"],         # clear the current input line
    "enter": ["Enter"],       # bare submit
}


def send_key(index: int, action: str) -> None:
    """Send a single allowlisted control key to a pane."""
    keys = _KEY_MAP.get(action)
    if keys is None:
        raise ValueError(f"unknown key action: {action!r}")
    target = _pane_target(index)
    _run(["send-keys", "-t", target, *keys])


def send(index: int, text: str, submit: bool) -> None:
    """Send literal text into a pane, then optionally press Enter to submit.

    Newlines in dictated text are collapsed to spaces so they cannot submit the
    prompt prematurely — submission is controlled solely by `submit`.
    """
    target = _pane_target(index)
    clean = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    if clean:
        # -l : literal (no key-name interpretation). -- : end of options.
        _run(["send-keys", "-t", target, "-l", "--", clean])
    if submit:
        _run(["send-keys", "-t", target, "Enter"])
