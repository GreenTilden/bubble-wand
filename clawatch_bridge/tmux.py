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

import os
import re
import subprocess
import time

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
# A numbered menu option: optional selection cursor (❯ and its look-alikes), then
# "N." or "N)", then the label. Menus render "❯ 1. Yes …". No leading box border is
# tolerated on purpose — Claude Code's plan box does NOT wrap its own numbered steps
# in "│ … │", and allowing a border here made those plan steps parse as menu options.
_OPTION_RE = re.compile(r"^\s*([❯>▸▶›])?\s*(\d+)[.)]\s+(\S.*)$")
# Multi-select checkbox at the head of an option label, e.g. "[ ] Pepperoni" /
# "[x] Mushrooms". Its presence marks the whole menu as multi-select (digits
# TOGGLE options; submission happens on the separate ✔ Submit tab).
_CHECKBOX_RE = re.compile(r"^\[( |x|X|✓|✔)\]\s*")
# Instructional sub-lines under a menu option (a hint, not part of the label).
_MENU_HINT_PREFIXES = ("shift+tab", "ctrl+", "esc ", "esc·", "press ", "tab to", "· ")
# Claude Code status bar, e.g. "Opus 4.8 · ▓▓▓ 311k ctx HARD · Σ4.2M · $22.12".
_STATUS_TOKENS_RE = re.compile(r"([\d.]+)\s*([kmKM]?)\s*ctx")
_STATUS_TIER_RE = re.compile(r"ctx\s+([A-Za-z]+)")
_STATUS_COST_RE = re.compile(r"\$\s*([\d.]+)")
# Cumulative session token spend, printed by the status line as "Σ4.2M" / "Σ812k".
_STATUS_SPEND_RE = re.compile(r"Σ\s*([\d.]+)\s*([kmKM]?)")


def _scale_tokens(n: float, suffix: str) -> int:
    suffix = suffix.lower()
    if suffix == "k":
        return int(n * 1_000)
    if suffix == "m":
        return int(n * 1_000_000)
    return int(n)


class TmuxError(Exception):
    """A tmux command failed or returned unexpected output."""


def _run(args: list[str], timeout: int = _TIMEOUT, input: str | None = None) -> str:
    try:
        proc = subprocess.run(
            [TMUX, *args],
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input,
        )
    except FileNotFoundError as e:  # tmux not installed / not on PATH
        raise TmuxError("tmux not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise TmuxError(f"tmux timed out: {args}") from e
    if proc.returncode != 0:
        raise TmuxError(proc.stderr.strip() or f"tmux failed: {args}")
    return proc.stdout


_SERVER_PID: int | None = None


def server_pid() -> int | None:
    """The tmux server's pid, cached for the process lifetime.

    Half of the durable thread identity: `threadKey = host:tmuxServerPid:paneId`.
    pane ids (%N) are unique and never reused *while the server lives*, but a
    restarted tmux server starts numbering again — so without the pid, events from
    two different server generations would collide on one key and a wash from
    yesterday would appear to belong to today's pane.

    Cached because it cannot change without this process's world changing anyway:
    if the tmux server restarts, every pane target this bridge holds is already
    invalid. Fail-soft: None on any error, and callers degrade the key rather than
    refuse to record.
    """
    global _SERVER_PID
    if _SERVER_PID is None:
        try:
            out = _run(["display-message", "-p", "#{pid}"]).strip()
            _SERVER_PID = int(out) if out.isdigit() else None
        except (TmuxError, ValueError):
            _SERVER_PID = None
    return _SERVER_PID


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

# Every escape sequence `capture-pane -e` can emit: SGR colour/attribute runs
# (the only ones we keep), plus OSC strings and any other CSI, which we drop.
# Everything in this file that INSPECTS a line -- chrome detection, border
# detection, the menu parser, the status-bar parser -- must run on the stripped
# form, or a colour run inside a line silently defeats the match. Colour is a
# display concern and only the display layer is allowed to see it.
_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC ... BEL/ST (titles, hyperlinks)
    r"|\x1b\[[0-9;:?]*[ -/]*[@-~]"         # any CSI, incl. SGR
    r"|\x1b[@-Z\\-_]"                      # lone two-character escapes
)


def strip_ansi(text: str) -> str:
    """Plain text for anything that PARSES. A no-op on already-plain captures,
    which is why the ansi and non-ansi paths can stay one code path."""
    return _ANSI_RE.sub("", text)


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
    if t in ("↓", "↑", "▲", "▼"):         # scroll-more indicators inside a box
        return True
    return False


def _clean_tail(lines: list[str]) -> list[str]:
    """Strip terminal chrome and collapse blank runs so the watch shows just
    the meaningful conversation text (and any active prompt options)."""
    out: list[str] = []
    for ln in lines:
        # Decide on the stripped form, keep the original. With `-e` captures a
        # line can start with a colour run, which would make `startswith("⏵⏵")`
        # and the border scan both miss -- so the status bar and dividers would
        # reappear on the phone the moment colour was switched on.
        probe = strip_ansi(ln)
        if _is_chrome(probe):
            continue
        if not probe.strip():
            if out and not strip_ansi(out[-1]).strip():
                continue  # collapse consecutive blanks
        out.append(ln.rstrip())
    while out and not strip_ansi(out[0]).strip():
        out.pop(0)
    while out and not strip_ansi(out[-1]).strip():
        out.pop()
    return out


def _clean_question(text: str) -> str:
    """Trim a joined question down to the final interrogative sentence, so a header
    reads 'Would you like to proceed?' rather than dragging in trailing plan prose
    that ended up adjacent after borders were stripped."""
    q = text.strip()
    if "?" not in q:
        return q
    end = q.rfind("?")
    start = 0
    for sep in (". ", "! ", "? "):
        p = q.rfind(sep, 0, end)
        if p != -1:
            start = max(start, p + len(sep))
    return q[start : end + 1].strip()


def parse_prompt(cleaned: list[str]) -> dict | None:
    """Detect a Claude Code interactive menu in already-cleaned tail lines.

    Returns {"question": str, "options": [{"key","label","selected"}]} or None.

    The menu is anchored by the selection cursor "❯"; without one there is no live
    prompt. The block is the TIGHT run of option lines around the cursor — options
    render on consecutive lines with NO blank line between them, so a blank line
    bounds the menu. That single rule does the heavy lifting:
      * plan steps (their own numbered list) sit above the question and are always
        separated from the menu by a blank line, so they never leak in; and
      * a wrapped option label (a non-blank continuation line, e.g. option 3
        spilling onto "the web") stays INSIDE the run and is joined onto its label,
        instead of truncating the menu and dropping every option below it.
    """
    # Every numbered-option line, with its position in the cleaned tail.
    matches: list[tuple[int, dict]] = []
    for i, ln in enumerate(cleaned):
        m = _OPTION_RE.match(ln)
        if m:
            matches.append(
                (i, {"key": m.group(2), "label": m.group(3).strip(), "selected": bool(m.group(1))})
            )
    sel = next((p for p, (_, o) in enumerate(matches) if o["selected"]), None)
    if sel is None:
        return None

    def _no_blank_between(a: int, b: int) -> bool:
        """True if no blank line lies strictly between line indices a and b."""
        return all(cleaned[k].strip() for k in range(a + 1, b))

    # Expand across consecutive options with no blank line between them.
    lo = hi = sel
    while lo > 0 and _no_blank_between(matches[lo - 1][0], matches[lo][0]):
        lo -= 1
    while hi < len(matches) - 1 and _no_blank_between(matches[hi][0], matches[hi + 1][0]):
        hi += 1
    block = matches[lo : hi + 1]

    # The block ends at the first blank line at/after its last option.
    block_end = block[-1][0] + 1
    while block_end < len(cleaned) and cleaned[block_end].strip():
        block_end += 1

    # Assemble options, folding wrapped continuation lines into each label. A
    # continuation is a non-blank, non-option, non-hint line before the next option.
    options: list[dict] = []
    seen: set[str] = set()
    for bi, (line_idx, o) in enumerate(block):
        nxt = block[bi + 1][0] if bi + 1 < len(block) else block_end
        extra: list[str] = []
        for k in range(line_idx + 1, nxt):
            s = cleaned[k].strip()
            if not s or _OPTION_RE.match(cleaned[k]):
                continue
            if any(s.lower().startswith(p) for p in _MENU_HINT_PREFIXES):
                continue
            if s.lower() in ("submit", "✔ submit"):
                # The un-numbered Submit row of a multi-select menu — a control,
                # not part of the option label above it.
                continue
            extra.append(s)
        label = (o["label"] + (" " + " ".join(extra) if extra else "")).strip()
        if o["key"] in seen:
            continue
        seen.add(o["key"])
        cb = _CHECKBOX_RE.match(label)
        checked = None
        if cb:
            checked = cb.group(1) not in (" ",)
            label = _CHECKBOX_RE.sub("", label).strip()
        options.append(
            {"key": o["key"], "label": label, "selected": o["selected"], "checked": checked}
        )

    # Question: skip the blank gap above the first option, then join the contiguous
    # non-blank lines (Claude wraps it), stopping at a blank or another option.
    first_line = block[0][0]
    j = first_line - 1
    while j >= 0 and not cleaned[j].strip():
        j -= 1
    q_lines: list[str] = []
    while j >= 0 and cleaned[j].strip() and not _OPTION_RE.match(cleaned[j]):
        q_lines.append(cleaned[j].strip())
        j -= 1
    question = _clean_question(" ".join(reversed(q_lines)))
    multi = any(o["checked"] is not None for o in options)
    return {"question": question, "options": options, "multiSelect": multi}


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

    Scans BOTTOM-UP. The real status bar is always the bottom-most matching line, and these
    panes are agents that routinely *discuss* context budgets — a rendered conversation line
    like "soft ~120k ctx · hard ~150k" parses perfectly and, scanning top-down, would win
    over the actual bar. That was tolerable when this only tinted a meter; it is not once the
    value drives a nudge threshold and gets appended to a permanent event log.
    """
    for ln in reversed(lines):
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
        tokens = _scale_tokens(n, m.group(2))
        # ctxTokens is derived from a RENDERED, ROUNDED string ("107k" -> 107000), so its
        # true precision is the rendering step, not 1 token. Recorded so a consumer never
        # reports a 400-token "improvement" that is pure rounding noise.
        _suffix = (m.group(2) or "").lower()
        _scale = 1_000_000 if _suffix == "m" else 1_000 if _suffix == "k" else 1
        _decimals = len(m.group(1).split(".")[1]) if "." in m.group(1) else 0
        resolution = max(1, _scale // (10 ** _decimals))
        model = t.split("·", 1)[0].strip() or None
        tier_m = _STATUS_TIER_RE.search(t)
        tier = tier_m.group(1) if tier_m else None
        cost_m = _STATUS_COST_RE.search(t)
        cost = float(cost_m.group(1)) if cost_m else None
        spend_m = _STATUS_SPEND_RE.search(t)
        spend = _scale_tokens(float(spend_m.group(1)), spend_m.group(2)) if spend_m else None
        return {
            "model": model,
            "ctxTokens": tokens,
            "ctxResolution": resolution,
            "ctxTier": tier,
            "costUsd": cost,
            "spendTokens": spend,
        }
    return None


def list_threads() -> list[dict]:
    out = _run(
        [
            "list-panes",
            "-t",
            settings.tmux_window,
            "-F",
            "#{pane_index}\t#{pane_current_command}\t#{pane_title}\t#{pane_id}\t#{pane_current_path}",
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
        # pane_id (%N) is stable for the pane's whole life and never reused while the tmux
        # server lives — which is exactly the lifetime a wash spans. pane_index is NOT an
        # identity: it is recomputed every poll and panes have already been destroyed and
        # recreated, so indices and ids diverge. Anything that outlives one poll keys on
        # pane_id. cwd is carried only as a repo BASENAME downstream, never a full path.
        pane_id = parts[3] if len(parts) > 3 else ""
        cwd = parts[4] if len(parts) > 4 else ""
        glyph, status, label = _derive_status(title)
        # Tail-based prompt detection beats the title glyph: if a pane is parked at
        # an interactive menu, surface it as NEEDS_INPUT so it's never hidden
        # (whatever the spinner glyph reads) AND flag it as tappable so the watch can
        # distinguish "answer by tapping" from "needs dictated input".
        has_prompt = False
        meter: dict | None = None
        try:
            # Capture the WHOLE visible pane, then slice for the prompt check. The old
            # 25-line window could push the status bar out of frame entirely — a permission
            # dialog, a long plan box, or a task list is enough — and then ctxTokens silently
            # went None. As a meter tint that was cosmetic; as a nudge threshold it means the
            # check stops running with nothing anywhere reporting that it stopped.
            # _do_capture pops trailing blanks BEFORE slicing, so raw[-25:] is byte-identical
            # to what the prompt path received before. No extra tmux call.
            raw = _do_capture(f"{settings.tmux_window}.{idx}", 0, False, False)
            meter = parse_status(raw)
            if parse_prompt(_clean_tail(raw[-25:])):
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
                # Durable identity. paneId survives the whole life of the pane; repo is the
                # cwd BASENAME only — never the full path, and never pane contents.
                "paneId": pane_id or None,
                "repo": os.path.basename(cwd.rstrip("/")) if cwd else None,
                "model": (meter or {}).get("model"),
                "ctxTokens": (meter or {}).get("ctxTokens"),
                "ctxResolution": (meter or {}).get("ctxResolution"),
                "ctxTier": (meter or {}).get("ctxTier"),
                "costUsd": (meter or {}).get("costUsd"),
                "spendTokens": (meter or {}).get("spendTokens"),
            }
        )
    return threads


def _do_capture(
    target: str,
    lines: int,
    scrollback: bool,
    clean: bool,
    ansi: bool = False,
    history: bool = False,
) -> list[str]:
    """Capture a pane by its already-built target (no re-validation).

    `ansi` adds `-e`, which makes tmux emit the pane's colour/attribute escape
    sequences instead of flattening them. Off by default and deliberately so:
    the watch strips colour for legibility on a 1.4" screen, and every parser
    in this module reads plain text. Only a client with room for it (the
    Telegram Mini App) asks for colour, and only for DISPLAY.

    `history` adds `-S -{lines}`, and it is what makes `lines` MEAN something
    above the pane height. Bare `capture-pane -p` returns the VISIBLE pane and
    nothing else, so the `captured[-lines:]` slice below could only ever shrink
    the result -- asking a 45-row pane for 100 lines returned 45, silently, and
    a caller reading the response had no way to tell the difference between
    "that's all there is" and "we never asked for more". Off by default: the
    watch asks for 40 and must keep getting exactly the screen it gets today.

    Deliberately NOT the same thing as `scrollback`, which is `-S -` -- the
    WHOLE buffer, however many thousand lines that is. This one is bounded by
    the caller's own `lines`.
    """
    args = ["capture-pane", "-p", "-t", target]
    if ansi:
        args.append("-e")
    if scrollback:
        args += ["-S", "-"]  # from the start of the scrollback buffer
    elif history and lines and lines > 0:
        # Start `lines` rows ABOVE the visible region. tmux clamps to the start
        # of the buffer on its own, so a young pane is not an error case.
        args += ["-S", f"-{int(lines)}"]
    out = _run(args)
    captured = out.splitlines()
    # A tall, mostly-empty pane (e.g. a fresh session) pads the capture with
    # blank rows below the content; drop them BEFORE the tail slice, or a menu
    # sitting high in the pane falls entirely outside the window and the
    # prompt goes undetected.
    while captured and not captured[-1].strip():
        captured.pop()
    if not scrollback and lines and lines > 0:
        captured = captured[-lines:]
    if clean:
        captured = _clean_tail(captured)
    return captured


def capture(
    index: int,
    lines: int,
    scrollback: bool,
    clean: bool = True,
    ansi: bool = False,
    history: bool = False,
) -> list[str]:
    target = _pane_target(index)
    return _do_capture(target, lines, scrollback, clean, ansi, history)


# Fixed allowlist of control keys the watch may send. The client sends only the
# action name; the actual tmux key string is chosen here, so no user-controlled
# string ever reaches send-keys as a key name.
_KEY_MAP: dict[str, list[str]] = {
    "escape": ["Escape"],     # dismiss a menu / cancel
    "interrupt": ["C-c"],     # stop a running command
    "clear": ["C-u"],         # clear the current input line
    "enter": ["Enter"],       # bare submit
    "tab": ["Tab"],           # next question tab / toward ✔ Submit
    # Navigation. None of these commit anything: they move a selection within a
    # menu, or scrub the input line. That is what makes them safe to expose as
    # plain taps next to keys that DO commit — the phone client can drive a
    # multi-select prompt to the right option without ever guessing at Enter.
    "up": ["Up"],             # previous option / previous history entry
    "down": ["Down"],         # next option
    "left": ["Left"],         # cursor left within the input line
    "right": ["Right"],       # cursor right within the input line
}


def send_key(index: int, action: str) -> None:
    """Send a single allowlisted control key to a pane."""
    keys = _KEY_MAP.get(action)
    if keys is None:
        raise ValueError(f"unknown key action: {action!r}")
    target = _pane_target(index)
    _run(["send-keys", "-t", target, *keys])


def _is_submit_confirm(parsed: dict) -> bool:
    """True if the parsed menu is the ✔ Submit review tab ('Ready to submit your
    answers?' with '1. Submit answers / 2. Cancel')."""
    return any(o["label"].lower().startswith("submit answers") for o in parsed["options"])


def submit_menu(index: int) -> dict:
    """Drive a multi-select question toward submission: Tab moves off the question
    (to the next question, or to the ✔ Submit review tab). On the review tab,
    select '1. Submit answers'. If another question renders instead, stop there —
    the watch re-scrapes and keeps answering.
    """
    target = _pane_target(index)
    # Guard: if no menu is on screen (already answered, race with another
    # answerer), press NOTHING — blind keys would land in the chat input.
    parsed = parse_prompt(_do_capture(target, 40, False, True))
    if not parsed:
        return {"submitted": False, "advanced": False}
    if not _is_submit_confirm(parsed):
        _run(["send-keys", "-t", target, "Tab"])
        time.sleep(0.4)
        parsed = parse_prompt(_do_capture(target, 40, False, True))
        if parsed and not _is_submit_confirm(parsed):
            return {"submitted": False, "advanced": True}
    if parsed:
        # On the review tab: the digit selects 'Submit answers' wherever the
        # cursor sits; Enter would depend on cursor position.
        _run(["send-keys", "-t", target, "-l", "--", "1"])
    else:
        # Menu vanished after Tab (unexpected fast path) — a bare Enter confirms.
        _run(["send-keys", "-t", target, "Enter"])
    return {"submitted": True, "advanced": False}


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


def paste(index: int, text: str) -> None:
    """Paste multi-line text into a pane's input, WITHOUT submitting it.

    Not a variant of send(). send() collapses newlines to spaces on purpose --
    it carries dictated text, where one stray newline would submit the prompt
    early -- and that collapsing is exactly why the wash's re-paste was dropped
    as "lands as ONE space-joined line". This is the primitive that was missing:
    `paste-buffer -p` wraps the text in bracketed-paste markers, which is how a
    terminal app is told "this is one paste", so the Claude TUI takes the
    newlines as content instead of N submissions.

    The text goes in via load-buffer's stdin, never as an argv element, so pane
    content is not exposed in the process table.

    The buffer is DELETED in a finally. It holds captured pane text, and a tmux
    buffer is readable by anything that can reach the tmux server and survives
    until overwritten -- leaving it there would put pane content somewhere it
    outlives the wash, which is the one thing this file's privacy rule forbids.
    """
    target = _pane_target(index)
    if not text:
        return
    buf = f"clawatch-wash-{index}"
    _run(["load-buffer", "-b", buf, "-"], input=text)
    try:
        _run(["paste-buffer", "-p", "-b", buf, "-t", target])
    finally:
        try:
            _run(["delete-buffer", "-b", buf])
        except TmuxError:
            pass  # already gone; nothing to clean up
