"""LLM-backed momentum suggestions for the wrist co-pilot.

Isolated from FastAPI and tmux: reads a cleaned tail (and optional parsed prompt)
and returns 2-3 ultra-short, always-on-our-side candidate replies. Every failure
mode -- missing key, missing package, timeout, rate limit, malformed output --
returns [] so the watch degrades gracefully to its static quick-reply row.
"""
from __future__ import annotations

import json
import logging
import re

from .config import settings

log = logging.getLogger("clawatch.suggest")

_client = None  # lazily built; None while suggest is disabled or pkg missing

SYSTEM_PROMPT = (
    "You are a wrist co-pilot. A developer is supervising a Claude Code coding "
    "agent from a smartwatch and needs to reply with one tap. Given the recent "
    "terminal output (and any on-screen menu), produce 2-3 ULTRA-SHORT candidate "
    "replies.\n"
    "\n"
    "Rules:\n"
    "- Each reply is at most ~6 words. No preamble.\n"
    "- Bias toward momentum: prefer approve / continue / proceed / \"yes go "
    "ahead\" / \"ship it\" -- the developer is on the agent's side and wants "
    "forward progress -- while staying context-appropriate. If it is a genuine "
    "either/or, still lead with the option that keeps work moving.\n"
    "- If the agent hit an error or is blocked, offer a terse unblock (\"try "
    "again\", \"skip that\", \"use X instead\") rather than a vague \"ok\".\n"
    "- If several prompts or questions are stacked on screen, answer ONLY the "
    "most recent (bottom-most) one. Never bundle answers to multiple questions "
    "into a single reply.\n"
    "- Output ONLY a JSON array of strings, e.g. [\"yes go ahead\",\"continue\","
    "\"explain first\"]. No keys, no markdown, no commentary."
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
    return [s[:60] for s in out][:3]  # cap length + count (2-3)
