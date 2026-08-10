"""Transcript-backed history — the source that exists, after the one we assumed didn't.

tmux keeps no scrollback for a pane on the alternate screen, which is every Claude
pane (measured in L22: history_size=0, alternate_on=1, against 1624 on the one bash
pane). So "show me what came before" cannot be answered from the terminal at all.
Claude's own transcript answers it better than the buffer would have: the text as
written, never wrapped by a terminal, so the client re-flows it to its own width.

Two things carry real risk here and both are tested hardest:

  IDENTIFICATION. A cwd maps to a project DIRECTORY of many sessions (45 for
  dellatech when this was written). Serving the wrong one is a privacy failure, not
  a display glitch -- so `pick` reports its confidence and refuses "matched" on a
  tie, and the client shows that.

  RENDERING vs MATCHING want opposite things. The phone does not want a file dump,
  so tool RESULTS are dropped from the display; but a Claude screen is mostly tool
  output, so matching that ignores results scores zero against the very session
  that produced it -- observed, first version. Two haystacks, one curated.

della cycle-66 L22.
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
    """The doubled dash in a worktree slug is `/.` -- derived from the live listing,
    where `-home-darney-projects-daliquot--claude-worktrees-...` is a real directory."""
    assert T.slug_for("/home/darney/projects/dellatech") == "-home-darney-projects-dellatech"
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
    assert out[0].endswith("…")


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
    shared = _assistant("editing /home/darney/projects/repo/shared_module_name.py carefully")
    _session(projects, cwd, "aaa", [shared])
    _session(projects, cwd, "bbb", [shared])
    _, conf = T.pick(cwd, "working on /home/darney/projects/repo/shared_module_name.py carefully")
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
