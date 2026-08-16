"""Transcript-backed history — the source that exists, after the one we assumed didn't.

tmux keeps no scrollback for a pane on the alternate screen, which is every Claude
pane (measured in L22: history_size=0, alternate_on=1, against 1624 on the one bash
pane). So "show me what came before" cannot be answered from the terminal at all.
Claude's own transcript answers it better than the buffer would have: the text as
written, never wrapped by a terminal, so the client re-flows it to its own width.

Two things carry real risk here and both are tested hardest:

  IDENTIFICATION. A cwd maps to a project DIRECTORY of many sessions (45 in the
  directory this was measured against). Serving the wrong one is a privacy failure, not
  a display glitch -- so `pick` reports its confidence and refuses "matched" on a
  tie, and the client shows that.

  RENDERING vs MATCHING want opposite things. The phone does not want a file dump,
  so tool RESULTS are dropped from the display; but a Claude screen is mostly tool
  output, so matching that ignores results scores zero against the very session
  that produced it -- observed, first version. Two haystacks, one curated.

cycle-66 L22.
"""
import json

import pytest

from clawatch_bridge import transcript as T


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _assistant(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _tool(name, **inp):
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def _result(text):
    return {"type": "user",
            "message": {"content": [{"type": "tool_result", "content": text}]}}


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "PROJECTS_DIR", tmp_path)
    return tmp_path


def _session(projects, cwd, name, rows):
    d = projects / T.slug_for(cwd)
    d.mkdir(parents=True, exist_ok=True)
    return _write(d / f"{name}.jsonl", rows)


# --- the path mapping -------------------------------------------------------


def test_slug_replaces_both_slashes_and_dots():
    """The doubled dash in a worktree slug is `/.` -- derived from a live listing,
    where worktree directories really do carry the doubled dash, not from documentation."""
    assert T.slug_for("/home/user/projects/demo-repo") == "-home-user-projects-demo-repo"
    assert T.slug_for("/home/d/p/x/.claude/worktrees/y") == "-home-d-p-x--claude-worktrees-y"


def test_no_transcript_directory_is_empty_not_an_error(projects):
    rows, has_older, meta = T.page("/nowhere/at/all", "", lines=10, before=0)
    assert rows == []
    assert has_older is False
    assert meta["confidence"] == "none"


# --- rendering --------------------------------------------------------------


def test_thinking_is_never_rendered(projects):
    rows = [{"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "SECRET REASONING"},
        {"type": "text", "text": "the answer"}]}}]
    assert T.render(rows) == ["the answer"]


def test_tool_results_are_dropped_but_the_call_survives(projects):
    """What ran is the useful line on a phone; 400 lines of its output is not."""
    out = T.render([_tool("Bash", command="ls -la /tmp"), _result("a\nb\nc")])
    assert out == ["⏵ Bash: ls -la /tmp"]


def test_user_prompts_are_marked(projects):
    assert T.render([_user("do the thing")]) == ["▸ do the thing"]


def test_only_the_first_line_of_a_prompt_is_marked(projects):
    out = T.render([_user("line one\nline two")])
    assert out == ["▸ line one", "line two"]


def test_a_paragraph_stays_one_logical_line(projects):
    """The whole reason this source beats a captured pane: nothing here is wrapped,
    so the client can wrap it to ITS width instead of inheriting a terminal's."""
    para = "x" * 900
    out = T.render([_assistant(para)])
    assert out == [para]


def test_long_tool_input_is_truncated_not_dumped(projects):
    out = T.render([_tool("Bash", command="echo " + "y" * 500)])
    assert len(out[0]) < 140
    assert "…" in out[0]


def test_truncation_keeps_both_ends_of_the_command(projects):
    """Head-only truncation made a run of ssh calls unreadable: they share their
    first 40 characters, so every line rendered the same and the part that said
    what the call DID was the part thrown away."""
    out = T.render([
        _tool("Bash", command="ssh -o ConnectTimeout=25 root@somehost " + "'x' " * 40 + "docker restart pg"),
        _tool("Bash", command="ssh -o ConnectTimeout=25 root@somehost " + "'x' " * 40 + "docker network prune"),
    ])
    assert out[0] != out[1], "two different commands must not render identically"
    assert out[0].endswith("docker restart pg")
    assert out[1].endswith("docker network prune")
    assert all(len(line) < 140 for line in out)


def test_a_path_keeps_its_basename(projects):
    """The head of a file_path is the repo prefix every other line on screen shares."""
    out = T.render([_tool("Read", file_path="/home/user/projects/" + "deep/" * 30 + "the_actual_file.py")])
    assert out[0].endswith("the_actual_file.py")


def test_short_details_are_left_exactly_alone(projects):
    """The elision must be invisible below the budget -- no stray ellipsis, no
    reflowed spacing on the lines that were already fine."""
    out = T.render([_tool("Bash", command="ls -la /tmp")])
    assert out == ["⏵ Bash: ls -la /tmp"]


# --- tools that used to render as a bare name -------------------------------
#
# 75 lines across the last 60 transcripts said only `⏵ TaskUpdate` / `⏵ Skill` /
# `⏵ AskUserQuestion`, because `_tool_line` knew seven input keys and none of the
# newer tools use them. A content-free line still costs a row of the window.


def test_a_task_update_says_what_it_updated(projects):
    out = T.render([_tool("TaskUpdate", taskId="abc123", status="completed",
                          description="Port the digest to the watch")])
    assert out == ["⏵ TaskUpdate: Port the digest to the watch"]


def test_a_task_update_with_no_description_falls_back_to_its_status(projects):
    """The common shape: a bare status flip. `completed` is thin, and still more
    than the bare name it replaced."""
    out = T.render([_tool("TaskUpdate", status="completed", taskId="abc123")])
    assert out == ["⏵ TaskUpdate: completed"]


def test_a_question_is_read_out_of_its_nested_list(projects):
    out = T.render([_tool("AskUserQuestion", questions=[
        {"question": "Which pane should the wash run on?", "header": "Pane",
         "options": [{"label": "dev:1.3"}, {"label": "dev:1.4"}]},
        {"question": "second question, not shown", "header": "Other"},
    ])])
    assert out == ["⏵ AskUserQuestion: Which pane should the wash run on?"]


def test_a_skill_is_named(projects):
    assert T.render([_tool("Skill", skill="brief", args="")]) == ["⏵ Skill: brief"]


def test_an_unknown_tool_shows_its_first_string_field(projects):
    """An MCP call, or a tool that ships after this list was written. The fallback
    is what keeps the list from having to be exhaustive to be useful."""
    out = T.render([_tool("mcp__claude_ai_Gmail__create_draft",
                          to="someone@example.com", subject="Bubbles launch")])
    assert out == ["⏵ mcp__claude_ai_Gmail__create_draft: Bubbles launch"]


def test_a_tool_with_nothing_sayable_is_still_just_its_name(projects):
    """No string anywhere: the name alone is the honest answer, not an invented one."""
    assert T.render([_tool("ListAgents")]) == ["⏵ ListAgents"]


# --- paging -----------------------------------------------------------------


def _many(projects, n):
    _session(projects, "/repo", "s1", [_assistant(f"line {i}") for i in range(n)])
    return "/repo"


def test_page_zero_is_the_newest_lines(projects):
    cwd = _many(projects, 100)
    rows, _, _ = T.page(cwd, "", lines=10, before=0)
    assert rows == [f"line {i}" for i in range(90, 100)]


def test_before_steps_back_without_gap_or_overlap(projects):
    cwd = _many(projects, 100)
    p0, _, _ = T.page(cwd, "", lines=10, before=0)
    p1, _, _ = T.page(cwd, "", lines=10, before=10)
    assert p1[-1] == "line 89"
    assert p0[0] == "line 90"
    assert set(p0).isdisjoint(p1)


def test_has_older_is_true_mid_history_and_false_at_the_start(projects):
    cwd = _many(projects, 30)
    _, mid, _ = T.page(cwd, "", lines=10, before=10)
    _, top, _ = T.page(cwd, "", lines=10, before=20)
    assert mid is True
    assert top is False


def test_paging_past_the_beginning_is_empty_not_an_error(projects):
    cwd = _many(projects, 20)
    rows, has_older, _ = T.page(cwd, "", lines=10, before=999)
    assert rows == []
    assert has_older is False


def test_negative_before_is_rejected(projects):
    cwd = _many(projects, 20)
    with pytest.raises(ValueError):
        T.page(cwd, "", lines=10, before=-1)


# --- identification, which is the part that can leak ------------------------


def test_a_single_session_needs_no_matching(projects):
    cwd = _many(projects, 5)
    _, _, meta = T.page(cwd, "", lines=5, before=0)
    assert meta["confidence"] == "only"


def test_the_pane_on_screen_picks_its_own_session(projects):
    """Two sessions, same repo -- the live case this exists for (two costas panes
    were running while it was written)."""
    cwd = "/repo"
    _session(projects, cwd, "aaa", [_assistant(
        "refactoring the widget_serialiser_v2 module in /srv/widgets/serialiser_registry.py")])
    _session(projects, cwd, "bbb", [_assistant(
        "investigating the kafka_consumer_lag_probe in /srv/probes/kafka_consumer_lag_probe.py "
        "against the retention_window_seconds setting")])
    # A real screen: chrome, a command, and the identifiers in between.
    pane = ("⎿ $ investigating the kafka_consumer_lag_probe now "
            "/srv/probes/kafka_consumer_lag_probe.py retention_window_seconds")
    path, conf = T.pick(cwd, pane)
    assert path.stem == "bbb"
    assert conf == "matched"


def test_matching_reads_tool_results_even_though_display_drops_them(projects):
    """The first version scored zero on the live pane because its haystack was the
    DISPLAY rendering. A Claude screen is mostly tool output, so the identifying
    tokens live in exactly the blocks the renderer throws away."""
    cwd = "/repo"
    _session(projects, cwd, "aaa", [_assistant("unrelated chatter here")])
    _session(projects, cwd, "bbb", [_result(
        "/opt/service-pool/quixotic_ledger_reconciler.py:41 warning threshold_exceeded_marker "
        "reconciliation_backlog=8821 ledger_checkpoint_stale")])
    pane = ("$ cat /opt/service-pool/quixotic_ledger_reconciler.py "
            "threshold_exceeded_marker reconciliation_backlog ledger_checkpoint_stale")
    path, conf = T.pick(cwd, pane)
    assert path.stem == "bbb"
    assert conf == "matched"


def test_a_tie_is_reported_as_a_guess_not_a_match(projects):
    """Two sessions editing the same file share every token on screen. Reporting
    'matched' there would put another pane's history under this pane's name with a
    confident label on it."""
    cwd = "/repo"
    shared = _assistant("editing /home/user/projects/repo/shared_module_name.py carefully")
    _session(projects, cwd, "aaa", [shared])
    _session(projects, cwd, "bbb", [shared])
    _, conf = T.pick(cwd, "working on /home/user/projects/repo/shared_module_name.py carefully")
    assert conf == "mtime"


def test_an_unrecognisable_screen_falls_back_to_recency(projects):
    cwd = "/repo"
    _session(projects, cwd, "aaa", [_assistant("alpha content")])
    _session(projects, cwd, "bbb", [_assistant("beta content")])
    _, conf = T.pick(cwd, "totally unrelated screen with no shared identifiers")
    assert conf == "mtime"


# --- reading the tail of a large file ---------------------------------------


def test_a_torn_first_record_is_dropped_only_when_we_seeked(projects, tmp_path):
    """Dropping it unconditionally would silently eat the first message of every
    short session -- a data loss that looks like a rendering choice."""
    d = tmp_path / "x"
    d.mkdir()
    p = _write(d / "s.jsonl", [_assistant("first"), _assistant("second")])
    assert T.render(T._read_rows(p)) == ["first", "second"]          # whole file
    assert T.render(T._read_rows(p, max_bytes=100))[-1] == "second"   # seeked
    assert "first" not in T.render(T._read_rows(p, max_bytes=100))


# --- the memoised pick, and why a TTL alone is not enough --------------------
#
# cycle-66 L23. Read-back was a TAP: one pick, ~90ms, invisible. The live
# view polls the same route every 4s for as long as the phone is open, and a pick
# re-reads up to 8 transcripts to score them. Cached -- but the invalidation is
# the part with teeth, because a stale pick serves the wrong session, and a
# transcript that has stopped growing renders as a pane that has gone quiet.


@pytest.fixture(autouse=True)
def _clear_pick_cache():
    T._pick_cache.clear()
    yield
    T._pick_cache.clear()


# Enough distinctive vocabulary to clear _MATCH_FLOOR (3 tokens of 10+ chars).
# Thinner fixtures than this fall through to "mtime" and every assertion below
# then tests the mtime guess instead of the matcher -- which is how the first cut
# of these tests "passed the wrong thing".
PANE_A = ("editing clawatch_bridge/transcript.py — pick_cached, _PICK_CACHE_MAX — "
          "running tests/test_transcript_history.py")
PANE_B = ("household/panel-miniapp/static/app.js — renderHistoryLabel, "
          "paintTranscript — running tests/test_readback_mode.py")


def _two_sessions(projects, cwd="/repo/x"):
    """Two sessions in one repo -- the case the content match exists for."""
    import os
    a = _session(projects, cwd, "aaaa", [
        _assistant("editing clawatch_bridge/transcript.py, adding pick_cached"),
        _tool("Bash", command="pytest tests/test_transcript_history.py -q"),
        _result("_PICK_CACHE_MAX is 64; 25 passed"),
    ])
    b = _session(projects, cwd, "bbbb", [
        _assistant("household/panel-miniapp/static/app.js — renderHistoryLabel"),
        _tool("Bash", command="npm test -- tests/test_readback_mode.py"),
        _result("paintTranscript ok"),
    ])
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))   # b is newest, so an mtime guess picks b
    return a, b


def test_pick_is_memoised_per_pane(projects, monkeypatch):
    """A second poll for the same pane must not re-score the candidate files."""
    a, _b = _two_sessions(projects)
    pane = PANE_A
    picks = []
    real = T.pick
    monkeypatch.setattr(T, "pick", lambda *args: picks.append(1) or real(*args))

    first = T.pick_cached("/repo/x", pane, "%7")
    second = T.pick_cached("/repo/x", pane, "%7")

    assert first == second
    assert first[0].name == a.name
    assert first[1] == "matched"
    assert len(picks) == 1, "the second poll re-read the transcripts"


def test_a_new_transcript_invalidates_the_cache(projects):
    """/clear starts a NEW file and stops appending to the old one.

    A TTL-only cache would keep painting the abandoned transcript for its whole
    window -- and a transcript that no longer grows looks exactly like a pane that
    has nothing to say, which is the failure shape this route was built to end.
    """
    _a, _b = _two_sessions(projects)
    pane = PANE_A
    first = T.pick_cached("/repo/x", pane, "%7")
    assert first[0].name == "aaaa.jsonl"

    # The operator runs /clear: a new session file, whose content the pane now shows.
    import os
    c = _session(projects, "/repo/x", "cccc", [
        _assistant("fresh session, still clawatch_bridge/transcript.py and pick_cached"),
        _tool("Bash", command="pytest tests/test_transcript_history.py -q"),
        _result("_PICK_CACHE_MAX unchanged"),
    ])
    os.utime(c, (3000, 3000))

    again = T.pick_cached("/repo/x", pane, "%7")
    assert again[0].name == "cccc.jsonl", "the cache outlived the session it named"


def test_cache_is_keyed_per_pane_not_per_repo(projects):
    """Two panes in one repo is the normal case, and the reason pick scores content.

    Keying on cwd alone would hand the second pane the first pane's answer -- the
    exact privacy failure, arrived at through the cache instead of through the
    matcher.
    """
    _two_sessions(projects)
    pane_a = PANE_A
    pane_b = PANE_B

    got_a = T.pick_cached("/repo/x", pane_a, "%7")
    got_b = T.pick_cached("/repo/x", pane_b, "%9")

    assert got_a[0].name == "aaaa.jsonl"
    assert got_b[0].name == "bbbb.jsonl"


def test_no_pane_key_falls_through_uncached(projects):
    """An unkeyable request is served correctly and uncached, never on a shared key."""
    _two_sessions(projects)
    pane = PANE_A
    got = T.pick_cached("/repo/x", pane, None)
    assert got[0].name == "aaaa.jsonl"
    assert T._pick_cache == {}


def test_page_passes_the_pane_key_through(projects):
    """The route's plumbing, pinned: page() must key the cache, not bypass it."""
    _two_sessions(projects)
    pane = PANE_A
    rows, _older, meta = T.page("/repo/x", pane, lines=10, before=0, pane_key="%7")
    assert meta["confidence"] == "matched"
    assert any("transcript.py" in r for r in rows)
    assert ("/repo/x", "%7") in T._pick_cache


# --- the property the whole fix rests on ------------------------------------


def test_match_survives_a_hard_wrapped_pane(projects):
    """The live view exists BECAUSE the pane can be 26 columns wide. If the matcher
    needed a wide pane, it would fail exactly when it is needed.

    It survives because the score is substring containment over long tokens: a
    path broken across two 26-column lines leaves a PREFIX that is still a
    substring of the whole path in the transcript. Measured on all four live panes
    at 26 columns before this was relied on; pinned here so a future matcher that
    switches to exact-token equality cannot pass silently.
    """
    import textwrap
    _a, _b = _two_sessions(projects)
    wide = PANE_A
    narrow = "\n".join(textwrap.wrap(wide, 26, break_long_words=True, break_on_hyphens=False))
    assert max(len(l) for l in narrow.splitlines()) <= 26

    got = T.pick_cached("/repo/x", narrow, "%7")
    assert got[1] == "matched"
    assert got[0].name == "aaaa.jsonl"
