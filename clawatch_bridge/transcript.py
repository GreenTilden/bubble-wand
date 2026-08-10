"""Session history from Claude's own transcript, because tmux has none.

WHY THIS EXISTS: Claude Code runs on the terminal's ALTERNATE SCREEN, and tmux
keeps no scrollback for the alternate screen. Measured rather than assumed
(dellatech cycle-66 L22): `alternate_on=1` and `history_size=0` on every Claude
pane, against `history_size=1624` on the one bash pane in the same session, with
`history-limit` set to 50000 and entirely irrelevant. `capture-pane -S -` on a
Claude pane returns exactly `pane_height` rows. So `scrollback=true` has been a
silent no-op on every Claude pane since this bridge was written -- the same shape
as the L15 `history` no-op, a parameter that looks like it works and hands back a
screenshot.

The transcript is a BETTER source than the buffer we thought we had. It is the
text as Claude wrote it, before a terminal ever wrapped it -- so it is immune to
the whole per-window-width problem L21 was about, rather than merely deeper.

THE HARD PART is which transcript. A pane knows its cwd and nothing else; a cwd
maps to a project DIRECTORY holding many sessions; and none of the obvious keys
discriminate:
  - the pane title is Claude's own aiTitle, and three different transcripts in
    this project reported the SAME last title (verified, not supposed);
  - `/proc/<pid>/environ` carries no CLAUDE_* variable;
  - the process holds no open fd on the file -- it appends and closes;
  - process start time picks the session's FIRST transcript, and `/clear` starts
    a new file under the same process.
So the match is made on CONTENT: what is on the pane's screen right now must
appear at the end of the transcript that produced it. That is the one thing that
cannot coincide across two sessions in the same repo, which is the case this has
to survive (two costas panes were live while this was written).

Nothing here executes anything or writes anywhere. It reads files the operator
already owns, on the operator's own machine, and serves them to the operator's
own phone through the same token and the same pane denylist as every other route.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# Where Claude keeps transcripts. Overridable for tests; not a client-facing knob.
PROJECTS_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects"))

# Read at most this much of a transcript's tail per request. A long session is
# tens of MB and the phone is asking for 150 lines; reading the whole file to
# serve the end of it is the kind of cost that only shows up on the session that
# matters most. Grown, not fixed -- see _read_rows.
_TAIL_BYTES = 512 * 1024
_MAX_TAIL_BYTES = 16 * 1024 * 1024

# Scoring for the content match. Tokens, not slices -- see _fragments for why the
# slice version scored zero on the live pane. 24 of them is enough that a repo's
# shared vocabulary cannot carry a match on its own; 3 is the floor because one
# or two long tokens really can appear in two sessions working the same file.
_FRAGMENTS = 24
_MATCH_FLOOR = 3

_WS = re.compile(r"\s+")


def slug_for(cwd: str) -> str:
    """`/home/darney/projects/dellatech` -> `-home-darney-projects-dellatech`.

    Both `/` and `.` become `-`, which is why a worktree path lands as
    `...-daliquot--claude-worktrees-...` (the doubled dash is `/.`). Derived from
    the live directory listing rather than from documentation.
    """
    return cwd.replace("/", "-").replace(".", "-")


def project_dir(cwd: str) -> Path:
    return PROJECTS_DIR / slug_for(cwd)


def candidates(cwd: str) -> list[Path]:
    """Transcripts for this cwd, newest first."""
    d = project_dir(cwd)
    if not d.is_dir():
        return []
    files = [p for p in d.glob("*.jsonl") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _read_rows(path: Path, max_bytes: int = _TAIL_BYTES) -> list[dict]:
    """Parse the last `max_bytes` of a JSONL transcript.

    The first line of a byte-offset read is almost always a partial record, so it
    is dropped -- deliberately, and only when we actually seeked: dropping it on a
    whole-file read would silently eat the first message of every short session.
    """
    size = path.stat().st_size
    seeked = size > max_bytes
    with path.open("rb") as fh:
        if seeked:
            fh.seek(size - max_bytes)
        blob = fh.read()
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if seeked and lines:
        lines = lines[1:]
    rows = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except ValueError:
            continue  # a torn record at either end is not an error
    return rows


def _blocks(message) -> list[dict]:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _tool_line(block: dict) -> str | None:
    """One line for a tool call. The phone wants to know WHAT ran, not its output.

    Tool results are skipped entirely: they are the bulk of a transcript by volume
    and the assistant's next message almost always states the outcome. Including
    them would make 'older' mostly a file-dump reader.
    """
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    detail = ""
    if isinstance(inp, dict):
        for key in ("command", "file_path", "pattern", "path", "url", "prompt", "query"):
            val = inp.get(key)
            if isinstance(val, str) and val.strip():
                detail = _WS.sub(" ", val.strip())
                break
    if len(detail) > 110:
        detail = detail[:109] + "…"
    return f"⏵ {name}{': ' + detail if detail else ''}"


def render(rows: list[dict]) -> list[str]:
    """Transcript events -> display lines, oldest first.

    Returns LOGICAL lines: a paragraph stays one line however long. That is the
    point of reading from here rather than from a terminal -- the client re-wraps
    to its own width, so the same history is legible at 390px and at 200 columns,
    which is precisely what a captured pane can never be.
    """
    out: list[str] = []
    for r in rows:
        kind = r.get("type")
        if kind not in ("user", "assistant"):
            continue
        if r.get("isMeta") or r.get("isSidechain"):
            continue
        msg = r.get("message")
        for b in _blocks(msg):
            btype = b.get("type")
            if btype == "thinking":
                continue          # the operator's own reasoning, and long
            if btype == "tool_result":
                continue          # see _tool_line
            if btype == "tool_use":
                line = _tool_line(b)
                if line:
                    out.append(line)
                continue
            if btype == "text":
                text = (b.get("text") or "").strip()
                if not text:
                    continue
                prefix = "▸ " if kind == "user" else ""
                for para in text.split("\n"):
                    para = para.rstrip()
                    if not para.strip():
                        continue
                    out.append(f"{prefix}{para}" if prefix else para)
                    prefix = ""   # only the first line of a prompt is marked
    return out


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip().lower()


def _match_text(rows: list[dict]) -> str:
    """Everything a transcript says, for MATCHING only -- including the tool
    results `render` deliberately drops.

    Display and identification want opposite things. The phone does not want a
    file dump, but the pane's screen is largely tool OUTPUT, so a haystack built
    from the display rendering scored zero against the very session that produced
    it (measured, first version). Matching therefore reads everything and the
    renderer stays curated.
    """
    parts: list[str] = []
    for r in rows:
        if r.get("type") not in ("user", "assistant"):
            continue
        for b in _blocks(r.get("message")):
            btype = b.get("type")
            if btype == "text":
                parts.append(b.get("text") or "")
            elif btype == "tool_use":
                inp = b.get("input")
                parts.append(json.dumps(inp) if isinstance(inp, (dict, list)) else str(inp))
            elif btype == "tool_result":
                c = b.get("content")
                if isinstance(c, str):
                    parts.append(c)
                elif isinstance(c, list):
                    parts.extend(
                        blk.get("text") or "" for blk in c if isinstance(blk, dict)
                    )
    return _norm("\n".join(parts))


def _fragments(pane_text: str) -> list[str]:
    """Distinctive TOKENS from what is on the pane right now.

    Contiguous slices were the first design and they scored zero against the live
    pane, which is worth recording: a Claude screen is mostly tool-call furniture
    ("1 shell command… ⎿ $ cd ~/…", the status bar, box rules), so a 48-character
    window almost always straddles chrome and content and matches nothing. Long
    tokens survive that -- they are the paths, identifiers and flags that appear
    on the screen AND in the transcript, with the furniture between them differing.

    Longest-first, so the score is carried by the rare tokens rather than by
    `projects` and `python`, which every session in a repo shares.
    """
    toks = {
        t for t in re.findall(r"[A-Za-z0-9_./~=\-]{10,}", pane_text)
        if not t.strip("-./=~") == ""
    }
    return [t.lower() for t in sorted(toks, key=len, reverse=True)[:_FRAGMENTS]]


def pick(cwd: str, pane_text: str) -> tuple[Path | None, str]:
    """Which transcript belongs to this pane. Returns (path, confidence).

    confidence is "matched" when the pane's own screen was found in the file,
    "mtime" when it was not and the newest file was assumed, "only" when there was
    nothing to disambiguate. The caller SHOWS this -- a history that might belong
    to the pane next door has to say so rather than look authoritative.
    """
    cands = candidates(cwd)
    if not cands:
        return None, "none"
    if len(cands) == 1:
        return cands[0], "only"
    frags = _fragments(pane_text)
    if frags:
        scored: list[tuple[int, Path]] = []
        for path in cands[:8]:   # newest 8; older ones cannot hold the live screen
            try:
                hay = _match_text(_read_rows(path))
            except OSError:
                continue
            scored.append((sum(1 for f in frags if f in hay), path))
        scored.sort(key=lambda sp: sp[0], reverse=True)
        # Strictly better than the runner-up, not merely best. Two sessions in one
        # repo share their paths and filenames, so a tie means the tokens that
        # separated them are not on screen -- and a confident wrong answer here
        # serves the pane next door's history under this pane's name.
        if scored and scored[0][0] >= _MATCH_FLOOR:
            if len(scored) == 1 or scored[0][0] > scored[1][0]:
                return scored[0][1], "matched"
    return cands[0], "mtime"


def page(
    cwd: str,
    pane_text: str,
    lines: int,
    before: int,
) -> tuple[list[str], bool, dict]:
    """A `lines`-tall window of rendered history ending `before` lines above the end.

    Same paging contract as tmux.capture_page, so the client walks back through
    either source with one code path -- and `has_older` is decided the same way,
    by looking one line past the window rather than by inferring from a short page.
    """
    if before < 0:
        raise ValueError("before must be >= 0")
    path, confidence = pick(cwd, pane_text)
    meta = {"session": path.stem if path else None, "confidence": confidence}
    if path is None:
        return [], False, meta
    want = lines + before + 1
    budget = _TAIL_BYTES
    rendered: list[str] = []
    while True:
        rendered = render(_read_rows(path, budget))
        # Grow only while there is more FILE to read: a session shorter than the
        # window is the top of its history, not an under-read.
        if len(rendered) >= want or budget >= _MAX_TAIL_BYTES or budget >= path.stat().st_size:
            break
        budget = min(budget * 4, _MAX_TAIL_BYTES)
    if before:
        rendered = rendered[:-before] if before < len(rendered) else []
    has_older = len(rendered) > lines
    return rendered[-lines:], has_older, meta
