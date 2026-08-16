"""Session history from Claude's own transcript, because tmux has none.

WHY THIS EXISTS: Claude Code runs on the terminal's ALTERNATE SCREEN, and tmux
keeps no scrollback for the alternate screen. Measured rather than assumed
(cycle-66 L22): `alternate_on=1` and `history_size=0` on every Claude
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
import time
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
    """`/home/user/projects/demo-repo` -> `-home-user-projects-demo-repo`.

    Both `/` and `.` become `-`, which is why a worktree path lands as
    `...-demo-repo--claude-worktrees-...` (the doubled dash is `/.`). Derived from
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


# Input keys worth showing, most specific first. `description` outranks `prompt`
# because the tools carrying both (Agent, TaskCreate) put a human sentence in one
# and a page of instructions in the other; the rest were read off the tools that
# were rendering as a bare name -- Skill, ScheduleWakeup, PushNotification,
# SendMessage, and the MCP calls, none of which use the original seven keys.
_DETAIL_KEYS = (
    "command", "file_path", "pattern", "path", "url",
    "description", "prompt", "query",
    "skill", "name", "reason", "message", "summary", "subject", "recipient",
)

# 110 was the old head-only budget and is kept, so a screen holds the same number
# of lines; the split is where the change is.
_DETAIL_MAX = 110
_DETAIL_HEAD = 70
_DETAIL_TAIL = 34
_ELLIPSIS = " … "


def _elide(detail: str) -> str:
    """Truncate from the MIDDLE, keeping both ends.

    Head-only truncation threw away the one part that told these lines apart. A
    run of `ssh -o ConnectTimeout=25 root@somehost '…'` calls is identical for its
    first 40 characters, so a screen of them read as one repeated line; a
    file_path lost its basename to the repo prefix it shares with everything else
    on screen. Both ends together are what make a line identifiable.
    """
    if len(detail) <= _DETAIL_MAX:
        return detail
    return f"{detail[:_DETAIL_HEAD]}{_ELLIPSIS}{detail[-_DETAIL_TAIL:]}"


def _questions_detail(inp: dict) -> str:
    """AskUserQuestion keeps its text one level down, in a list of dicts.

    Only the first question: the line is a "what happened here" marker, and the
    options belong on the pane itself, where the operator can actually answer.
    """
    qs = inp.get("questions")
    if not isinstance(qs, list):
        return ""
    for q in qs:
        if isinstance(q, dict):
            for key in ("question", "header"):
                val = q.get(key)
                if isinstance(val, str) and val.strip():
                    return _WS.sub(" ", val.strip())
    return ""


def _detail_for(inp: dict) -> str:
    for key in _DETAIL_KEYS:
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return _elide(_WS.sub(" ", val.strip()))
    nested = _questions_detail(inp)
    if nested:
        return _elide(nested)
    # Anything else -- an MCP call, a tool that shipped after this list was
    # written -- gets its first string field rather than nothing. A bare
    # `⏵ TaskUpdate` costs a row of a 150-line window and says nothing; the
    # same row reading `⏵ TaskUpdate: completed` is worth its space. Insertion
    # order, which is the order the model wrote the arguments in.
    for val in inp.values():
        if isinstance(val, str) and val.strip():
            return _elide(_WS.sub(" ", val.strip()))
    return ""


def _tool_line(block: dict) -> str | None:
    """One line for a tool call. The phone wants to know WHAT ran, not its output.

    Tool results are skipped entirely: they are the bulk of a transcript by volume
    and the assistant's next message almost always states the outcome. Including
    them would make 'older' mostly a file-dump reader.
    """
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    detail = _detail_for(inp) if isinstance(inp, dict) else ""
    return f"⏵ {name}{': ' + detail if detail else ''}"


def render_tagged(rows: list[dict]) -> list[tuple[str, str]]:
    """Transcript events -> (author, line) pairs, oldest first.

    `author` is "user", "claude" or "tool", and it is returned rather than left
    for the client to infer BECAUSE THE MARKERS CANNOT CARRY IT: only the first
    line of a prompt is marked, so an unmarked line is either the second
    paragraph of what the operator typed or the first line of Claude's reply, and
    nothing in the text tells those apart. A client that guesses gets it wrong in
    the most common state there is -- a one-line prompt answered by a paragraph.
    """
    out: list[tuple[str, str]] = []
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
                    out.append(("tool", line))
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
                    out.append((
                        "user" if kind == "user" else "claude",
                        f"{prefix}{para}" if prefix else para,
                    ))
                    prefix = ""   # only the first line of a prompt is marked
    return out


def render(rows: list[dict]) -> list[str]:
    """Transcript events -> display lines, oldest first.

    Returns LOGICAL lines: a paragraph stays one line however long. That is the
    point of reading from here rather than from a terminal -- the client re-wraps
    to its own width, so the same history is legible at 390px and at 200 columns,
    which is precisely what a captured pane can never be.
    """
    return [line for _, line in render_tagged(rows)]


def last_turn(tagged: list[tuple[str, str]]) -> int:
    """How many of the LAST lines are Claude's closing message to the operator.

    0 whenever the session ends on a prompt or on a tool call -- which is what
    makes this a usable "it is your move" signal and not merely "the newest
    text": a pane mid-work ends on `⏵ Bash: ...`, and a pane whose operator has
    just typed ends on their own prompt. Only a finished answer counts.

    Deliberately a COUNT FROM THE END rather than a line index. The client
    accumulates history by prepending older pages, so every absolute index it was
    given shifts the moment it reads further back; a count of trailing lines is
    the one form that survives that.

    Tool lines stop the count instead of being absorbed into the message they
    belong to. An assistant turn that ends in a tool call is not waiting on
    anybody, so counting through it would put the emphasis on a pane that is
    working -- exactly the false "answer me" this exists to avoid.
    """
    n = 0
    for author, _ in reversed(tagged):
        if author != "claude":
            break
        n += 1
    return n


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


# Memoised pick. Read-back was a tap: one pick, 8 files, ~90ms, nobody noticed.
# The live view polls this route every 4s for as long as the phone is open, and
# `pick` reads up to 8 transcripts of half a megabyte each to score them -- ~4MB
# of re-read per tick, to re-derive an answer that changes only when the operator
# runs /clear. So it is cached, but NOT on a timer alone: a stale pick is a wrong
# session, which is the one failure this module exists to prevent.
_PICK_TTL_S = 30.0
_PICK_CACHE_MAX = 64
_pick_cache: dict[tuple[str, str], dict] = {}


def pick_cached(cwd: str, pane_text: str, pane_key: str | None) -> tuple[Path | None, str]:
    """`pick`, memoised per pane, with an invalidation that survives /clear.

    Two conditions drop the entry, and the second is the load-bearing one:
      - age, so a mis-pick cannot outlive half a minute of being wrong; and
      - a change in the SET of candidate filenames, which is exactly what /clear
        does -- it starts a new transcript under the same project directory while
        the old one stops growing. A TTL alone would leave the live view painting
        a transcript that stopped being appended to, and a frozen pane looks like
        a quiet pane rather than like a bug (the L22 failure shape, again).

    Keyed on pane_key (the tmux `%N` pane id), never on cwd alone: two panes in
    one repo is the normal case here and the whole reason `pick` scores content.
    Falls straight through to `pick` when the caller has no pane id -- an
    unkeyable request is served correctly and uncached rather than sharing a
    neighbour's entry.
    """
    if not pane_key:
        return pick(cwd, pane_text)
    key = (cwd, pane_key)
    now = time.monotonic()
    names = frozenset(p.name for p in candidates(cwd))
    hit = _pick_cache.get(key)
    if (
        hit is not None
        and now - hit["at"] < _PICK_TTL_S
        and hit["names"] == names
        and (hit["path"] is None or hit["path"].exists())
    ):
        return hit["path"], hit["confidence"]
    path, confidence = pick(cwd, pane_text)
    # Panes die and are recreated, so keys accumulate over a long-lived server.
    # A flat clear at the cap, rather than an LRU: the population is a handful of
    # panes, so the cap is only ever reached by churn, and re-picking a live pane
    # costs one request.
    if len(_pick_cache) >= _PICK_CACHE_MAX:
        _pick_cache.clear()
    _pick_cache[key] = {"path": path, "confidence": confidence, "at": now, "names": names}
    return path, confidence


def page(
    cwd: str,
    pane_text: str,
    lines: int,
    before: int,
    pane_key: str | None = None,
) -> tuple[list[str], bool, dict]:
    """A `lines`-tall window of rendered history ending `before` lines above the end.

    Same paging contract as tmux.capture_page, so the client walks back through
    either source with one code path -- and `has_older` is decided the same way,
    by looking one line past the window rather than by inferring from a short page.
    """
    if before < 0:
        raise ValueError("before must be >= 0")
    path, confidence = pick_cached(cwd, pane_text, pane_key)
    meta = {"session": path.stem if path else None, "confidence": confidence, "last_turn": 0}
    if path is None:
        return [], False, meta
    want = lines + before + 1
    budget = _TAIL_BYTES
    tagged: list[tuple[str, str]] = []
    rendered: list[str] = []
    while True:
        tagged = render_tagged(_read_rows(path, budget))
        rendered = [line for _, line in tagged]
        # Grow only while there is more FILE to read: a session shorter than the
        # window is the top of its history, not an under-read.
        if len(rendered) >= want or budget >= _MAX_TAIL_BYTES or budget >= path.stat().st_size:
            break
        budget = min(budget * 4, _MAX_TAIL_BYTES)
    # Reported for the SESSION, not for the window: it counts back from the live
    # end, which is where the client's accumulated buffer ends however far back
    # this particular page reaches.
    meta["last_turn"] = last_turn(tagged)
    if before:
        rendered = rendered[:-before] if before < len(rendered) else []
    has_older = len(rendered) > lines
    return rendered[-lines:], has_older, meta
