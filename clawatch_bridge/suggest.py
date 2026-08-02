"""LLM-backed momentum suggestions for the wrist co-pilot.

Isolated from FastAPI and tmux: reads a cleaned tail (and optional parsed prompt)
and returns 2-3 short, always-on-our-side candidate replies. Every failure mode --
missing key, missing package, timeout, rate limit, malformed output -- returns []
so the watch degrades gracefully to its static quick-reply row.
"""
from __future__ import annotations

import json
import logging
import re
import threading

from .config import settings

log = logging.getLogger("clawatch.suggest")

_client = None  # lazily built; None while suggest is disabled or pkg missing

# Cumulative Haiku usage for the co-pilot itself (in-memory; resets on restart).
_usage_lock = threading.Lock()
_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

SYSTEM_PROMPT = (
    "You are a wrist co-pilot. A developer is supervising a Claude Code coding "
    "agent from a smartwatch and needs to reply with one tap. Given the recent "
    "terminal output (and any on-screen menu), produce 2-3 candidate replies.\n"
    "\n"
    "Rules:\n"
    "- Each reply is a short, natural phrase — roughly 4 to 12 words. Be specific "
    "and reference what the agent is actually doing (e.g. \"yes, refactor it and "
    "rerun the tests\" rather than a bare \"ok\"). Unambiguous, but not a paragraph. "
    "No preamble.\n"
    "- Bias toward momentum: prefer approving and moving forward (\"yes, go ahead "
    "with that\", \"looks right, ship it\", \"continue and I will review after\") — "
    "the developer is on the agent's side and wants progress — while staying "
    "context-appropriate. If it is a genuine either/or, still lead with the option "
    "that keeps work moving.\n"
    "- If the agent hit an error or is blocked, offer a specific unblock (\"skip "
    "that file and keep going\", \"try the other approach instead\", \"install it "
    "and retry\") rather than a vague \"ok\".\n"
    "- If several prompts or questions are stacked on screen, answer ONLY the most "
    "recent (bottom-most) one. Never bundle answers to multiple questions into a "
    "single reply.\n"
    "- Output ONLY a JSON array of strings, e.g. [\"yes, go ahead with that\","
    "\"continue and I will review after\",\"explain the tradeoff first\"]. No keys, "
    "no markdown, no commentary."
)


SUMMARY_SYSTEM_PROMPT = (
    "You are a wrist co-pilot. A developer is glancing at a smartwatch to see what "
    "each of their running Claude Code agents is doing RIGHT NOW. Given the recent "
    "terminal output from one agent that is actively working (not waiting on the "
    "developer), write ONE short status line describing what it is currently doing.\n"
    "\n"
    "Rules:\n"
    "- 3 to 8 words, present tense, concrete. Name the actual activity: "
    "\"running the gradle build\", \"editing the tmux parser\", \"searching for "
    "the crash\", \"installing dependencies\".\n"
    "- No trailing punctuation, no quotes, no preamble, no leading dash. Just the "
    "phrase.\n"
    "- If you genuinely cannot tell, output: working\n"
    "- Output ONLY the phrase, nothing else."
)


def _get_client():
    global _client
    if _client is None and settings.suggest_enabled:
        import anthropic  # local import: bridge still boots if pkg missing

        _client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            max_retries=0,  # per-request timeout is the only budget; no x3 blowup
        )
    return _client


def reset_client() -> None:
    """Drop the cached client so the next call rebuilds it with current settings
    (used after /setup provisions a key at runtime)."""
    global _client
    _client = None


def generate_suggestions(cleaned: list[str], prompt: dict | None) -> list[str]:
    client = _get_client()
    if client is None:
        return []
    if not cleaned and not prompt:  # nothing on screen -> no call, no cost
        return []

    tail_text = "\n".join(cleaned[-settings.suggest_tail_lines:])[-4000:]
    if prompt:
        opts = "\n".join(f"  {o['key']}. {o['label']}" for o in prompt["options"])
        ctx = (
            "An interactive menu is on screen.\n"
            f"Question: {prompt['question']}\n"
            f"Options:\n{opts}\n\n"
            f"Recent terminal:\n{tail_text}"
        )
    else:
        ctx = f"Recent terminal from the coding agent:\n{tail_text}"

    try:
        msg = client.with_options(timeout=settings.suggest_timeout).messages.create(
            model=settings.suggest_model,
            max_tokens=settings.suggest_max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": ctx}],
        )
    except Exception as e:  # noqa: BLE001 -- any SDK/HTTP failure degrades to []
        log.warning("suggest: anthropic call failed: %s", e)
        return []

    try:
        _record_usage(msg.usage)
    except Exception:  # noqa: BLE001 -- usage accounting must never break suggest
        pass

    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return _parse(text)


def _parse(text: str) -> list[str]:
    out: list[str] = []
    try:  # preferred: a JSON array anywhere in the text
        s, e = text.find("["), text.rfind("]")
        if s != -1 and e > s:
            out = [str(x).strip() for x in json.loads(text[s : e + 1]) if str(x).strip()]
    except Exception:  # noqa: BLE001
        out = []
    if not out:  # fallback: line-split, strip bullets/quotes/numbering
        for ln in text.splitlines():
            t = re.sub(r'^[\s\-\*\d.)"]+', "", ln).strip().strip('"').strip()
            if t:
                out.append(t)
    return [s[:100] for s in out][:3]  # cap length + count (2-3)


def _record_usage(usage) -> None:
    with _usage_lock:
        _usage["calls"] += 1
        _usage["input_tokens"] += int(getattr(usage, "input_tokens", 0) or 0)
        _usage["output_tokens"] += int(getattr(usage, "output_tokens", 0) or 0)


def get_usage() -> dict:
    with _usage_lock:
        calls = _usage["calls"]
        it = _usage["input_tokens"]
        ot = _usage["output_tokens"]
    cost = it / 1e6 * settings.suggest_price_in + ot / 1e6 * settings.suggest_price_out
    return {
        "calls": calls,
        "input_tokens": it,
        "output_tokens": ot,
        "estimated_cost_usd": round(cost, 4),
    }


def generate_summary(cleaned: list[str]) -> str:
    """One short present-tense line describing what a WORKING thread is doing.
    Any failure (no key, timeout, rate limit) degrades to "" so the watch just
    shows the plain status."""
    client = _get_client()
    if client is None or not cleaned:
        return ""
    tail_text = "\n".join(cleaned[-settings.suggest_tail_lines:])[-4000:]
    try:
        msg = client.with_options(timeout=settings.suggest_timeout).messages.create(
            model=settings.suggest_model,
            max_tokens=settings.summary_max_tokens,
            system=SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Recent terminal from the coding agent:\n{tail_text}"}],
        )
    except Exception as e:  # noqa: BLE001 -- any SDK/HTTP failure degrades to ""
        log.warning("summary: anthropic call failed: %s", e)
        return ""
    try:
        _record_usage(msg.usage)
    except Exception:  # noqa: BLE001
        pass
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    # Collapse to a single tidy line, strip stray quotes/bullets, cap length.
    text = text.splitlines()[0].strip() if text else ""
    text = re.sub(r'^[\s\-\*\d.)]+', '', text).strip().strip('"').strip("'").strip()
    return text[:60]


PROMPT_SUMMARY_SYSTEM_PROMPT = (
    "You are a wrist co-pilot. One of a developer's Claude Code agents has PAUSED and "
    "is asking them to choose from a menu. Given the question, the options, and the "
    "recent terminal output, write ONE short line describing the DECISION the developer "
    "is being asked to make, so they can pick without reading the whole screen.\n"
    "\n"
    "Rules:\n"
    "- One line, present tense, concrete. Capture WHAT is being decided and the real "
    "trade-off, not the option labels verbatim. E.g. \"whether to migrate the schema in "
    "place or via a new table\", \"which model to route summary calls to\".\n"
    "- Max ~18 words. No preamble, no quotes, no leading dash, no trailing punctuation.\n"
    "- If you genuinely cannot tell, output: awaiting your choice\n"
    "- Output ONLY the line."
)


def generate_prompt_summary(cleaned: list[str], prompt: dict) -> str:
    """One short line describing the decision a PAUSED thread is asking for. Any
    failure (no key, timeout, rate limit) degrades to "" so the watch just shows the
    options with no summary."""
    client = _get_client()
    if client is None or not prompt:
        return ""
    tail_text = "\n".join(cleaned[-settings.suggest_tail_lines:])[-4000:]
    opts = "\n".join(f"  {o['key']}. {o['label']}" for o in prompt["options"])
    ctx = (
        f"Question on screen: {prompt['question']}\n"
        f"Options:\n{opts}\n\n"
        f"Recent terminal:\n{tail_text}"
    )
    try:
        msg = client.with_options(timeout=settings.suggest_timeout).messages.create(
            model=settings.suggest_model,
            max_tokens=settings.prompt_summary_max_tokens,
            system=PROMPT_SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": ctx}],
        )
    except Exception as e:  # noqa: BLE001 -- any SDK/HTTP failure degrades to ""
        log.warning("prompt-summary: anthropic call failed: %s", e)
        return ""
    try:
        _record_usage(msg.usage)
    except Exception:  # noqa: BLE001
        pass
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = " ".join(text.split())  # collapse any newlines/extra spaces to one line
    text = re.sub(r'^[\s\-\*\d.)]+', '', text).strip().strip('"').strip("'").strip()
    return text[:140]
