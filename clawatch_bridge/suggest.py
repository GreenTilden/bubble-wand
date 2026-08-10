"""LLM-backed momentum suggestions for the wrist co-pilot.

Isolated from FastAPI and tmux: reads a cleaned tail (and optional parsed prompt)
and returns 2-3 short, always-on-our-side candidate replies. Every failure mode --
missing key, missing package, timeout, rate limit, malformed output -- returns []
so the watch degrades gracefully to its static quick-reply row.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone

from .config import settings

log = logging.getLogger("clawatch.suggest")

_client = None  # lazily built; None while suggest is disabled or pkg missing

# Cumulative Haiku usage for the co-pilot itself. DURABLE across restarts: an in-memory
# counter silently means "since whatever restart last happened", so a quiet day and a
# service bounce read identically. Persisted to settings.usage_state (totals only — no
# pane content, no prompts) and reloaded at import. Every path here is best-effort: this
# module's contract is that accounting NEVER breaks a suggestion.
_usage_lock = threading.Lock()
# by_model carries the per-model split that makes the dollar figure honest once more
# than one tier is in play; the flat totals stay for the API shape and for state files
# written before the split existed.
_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "since": None, "by_model": {}}


def _load_usage() -> None:
    """Best-effort restore of the cumulative counters. Corrupt or absent → start clean."""
    try:
        with open(settings.usage_state, encoding="utf-8") as fh:
            saved = json.load(fh)
        with _usage_lock:
            for k in ("calls", "input_tokens", "output_tokens"):
                _usage[k] = int(saved.get(k) or 0)
            _usage["since"] = saved.get("since") or None
            # A state file written before per-model accounting has no by_model. Those
            # tokens were all Haiku (nothing else existed yet), but rather than assert
            # that, get_usage() prices any unattributed remainder at the co-pilot rate
            # -- which is the same answer, without inventing a per-model history.
            by_model = saved.get("by_model")
            if isinstance(by_model, dict):
                _usage["by_model"] = {
                    str(m): {k: int(v.get(k) or 0) for k in ("calls", "input_tokens", "output_tokens")}
                    for m, v in by_model.items()
                    if isinstance(v, dict)
                }
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001 -- a bad state file must not stop the bridge
        log.warning("usage state unreadable (%s: %s) — counting from zero",
                    type(e).__name__, e)


def _save_usage_locked() -> None:
    """Atomic write, called with _usage_lock held. Never raises."""
    try:
        os.makedirs(os.path.dirname(settings.usage_state), exist_ok=True)
        tmp = f"{settings.usage_state}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_usage, fh)
        os.replace(tmp, settings.usage_state)   # atomic — no torn read by /api/usage
    except Exception as e:  # noqa: BLE001
        log.warning("usage state not persisted (%s: %s)", type(e).__name__, e)


_load_usage()

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


_warm_in_flight = threading.Lock()  # at most one background warm at a time


def _warm_local_model() -> None:
    """Fire-and-forget model load, spawned when a local call times out.

    Ollama cancels a cold load when the requester disconnects, so the timed-out
    suggest call warms nothing by itself — the live probe that found this paid
    the cloud fallback three taps in a row. This thread holds its connection
    for as long as the load takes; the tap that spawned it was already served
    by the fallback, and the next one is local."""
    if not _warm_in_flight.acquire(blocking=False):
        return  # a warm is already running; a second would just queue behind it

    def _run():
        import urllib.request
        try:
            req = urllib.request.Request(
                f"http://{settings.ollama_host}/api/generate",
                data=json.dumps({"model": settings.ollama_model, "prompt": "",
                                 "keep_alive": settings.ollama_keep_alive}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=300).read()
            log.info("suggest: local model warmed")
        except Exception as e:  # noqa: BLE001 -- best-effort; next tap falls back again
            log.warning("suggest: local warm failed: %s", e)
        finally:
            _warm_in_flight.release()

    threading.Thread(target=_run, daemon=True, name="ollama-warm").start()


def _ollama_suggestions(ctx: str) -> list[str]:
    """The local backend. Same contract as every path in this module: any
    failure — host down, model evicted and cold-loading past the timeout,
    unparseable output — returns [] and the caller decides what happens next.
    No usage recording: the meter counts cloud spend, and a local call is the
    absence of exactly that."""
    import urllib.error
    import urllib.request

    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "think": False,
        "keep_alive": settings.ollama_keep_alive,
        "options": {"temperature": 0, "num_predict": settings.suggest_max_tokens},
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": ctx}],
    }
    for attempt in (0, 1):
        try:
            req = urllib.request.Request(
                f"http://{settings.ollama_host}/api/chat",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=settings.suggest_timeout) as r:
                body = json.loads(r.read().decode())
            return _parse(((body.get("message") or {}).get("content") or "").strip())
        except urllib.error.HTTPError as e:  # noqa: PERF203
            # Model families disagree on the think field's PRESENCE (llama-class
            # 400s when it is set, qwen-class needs it false, gpt-oss rejects
            # disabling) — retry once without it. Thinking tokens then race the
            # same timeout they would cost production.
            if attempt == 0 and "think" in payload:
                log.info("suggest: local backend rejected think field (%s) — retrying without", e.code)
                payload.pop("think")
                continue
            log.warning("suggest: local backend HTTP %s", e.code)
            return []
        except Exception as e:  # noqa: BLE001 -- timeout/conn-refused/bad JSON all degrade
            if isinstance(e, TimeoutError) or "timed out" in str(e):
                _warm_local_model()  # a cold load missed the window; warm for the next tap
            log.warning("suggest: local backend failed: %s", e)
            return []
    return []


def generate_suggestions(cleaned: list[str], prompt: dict | None) -> list[str]:
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

    if settings.suggest_backend == "ollama":
        out = _ollama_suggestions(ctx)
        if out:
            return out
        log.info("suggest: local backend produced nothing — falling back to cloud")

    client = _get_client()
    if client is None:
        return []

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


def _record_usage(usage, model: str | None = None) -> None:
    """`model` defaults to the co-pilot model so the three existing call sites are
    unchanged; the digest passes its own so its tokens are priced at its own rate."""
    model = model or settings.suggest_model
    it = int(getattr(usage, "input_tokens", 0) or 0)
    ot = int(getattr(usage, "output_tokens", 0) or 0)
    with _usage_lock:
        _usage["calls"] += 1
        _usage["input_tokens"] += it
        _usage["output_tokens"] += ot
        per = _usage["by_model"].setdefault(
            model, {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        )
        per["calls"] += 1
        per["input_tokens"] += it
        per["output_tokens"] += ot
        if not _usage["since"]:
            # Stamped on the FIRST ever call, then never again — this is what makes the
            # totals mean something. A cumulative number with no start date is unreadable.
            _usage["since"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _save_usage_locked()


def get_usage() -> dict:
    with _usage_lock:
        calls = _usage["calls"]
        it = _usage["input_tokens"]
        ot = _usage["output_tokens"]
        since = _usage["since"]
        by_model = {m: dict(v) for m, v in _usage["by_model"].items()}

    fallback = (settings.suggest_price_in, settings.suggest_price_out)
    cost = 0.0
    for model, per in by_model.items():
        p_in, p_out = settings.model_prices.get(model, fallback)
        per_cost = per["input_tokens"] / 1e6 * p_in + per["output_tokens"] / 1e6 * p_out
        cost += per_cost
        # Priced HERE, where the price table lives, and shipped with the split.
        # The alternative is a client that re-implements pricing against its own
        # copy of the rates -- two answers to "what did this cost", diverging the
        # first time a rate changes. `priced` says whether this used the model's
        # own rate or the co-pilot fallback, so an unpriced model is visible as
        # an estimate rather than passing as a quote.
        per["estimated_cost_usd"] = round(per_cost, 4)
        per["priced"] = model in settings.model_prices
    # Anything the per-model split does not account for -- tokens from a state file
    # written before the split -- is priced at the co-pilot rate rather than dropped.
    # Dropping it would make the total FALL after an upgrade, which reads as a bug in
    # the meter and hides real spend.
    rest_in = max(0, it - sum(v["input_tokens"] for v in by_model.values()))
    rest_out = max(0, ot - sum(v["output_tokens"] for v in by_model.values()))
    rest_cost = rest_in / 1e6 * fallback[0] + rest_out / 1e6 * fallback[1]
    cost += rest_cost
    rest_calls = max(0, calls - sum(v["calls"] for v in by_model.values()))
    return {
        "calls": calls,
        "input_tokens": it,
        "output_tokens": ot,
        # Tokens are MEASURED (straight off the API response). Dollars are NOT: this is
        # tokens x a configured list price, which is the right shape for an in-app budget
        # guard and exactly the fabrication a fleet-wide cost collector must refuse. The
        # basis travels with the number so the two can never be quietly added together.
        "estimated_cost_usd": round(cost, 4),
        "cost_basis": "list-price estimate, not a metered bill",
        "since": since,
        # Per-model split. Surfaced because "$0.40 of Haiku" and "$0.40 of Sonnet"
        # are different facts about how the app is being used, and the flat total
        # cannot tell them apart. UsageResponse must declare this or pydantic drops
        # it silently -- the exact defect L14.1's live probe caught.
        "by_model": by_model,
        # The remainder the split cannot attribute (tokens recorded before
        # per-model metering existed). Reported rather than folded into the total
        # in silence: a breakdown whose parts do not sum to the headline is what
        # makes a meter untrustworthy, and the repair is to name the gap.
        "unattributed": {
            "calls": rest_calls,
            "input_tokens": rest_in,
            "output_tokens": rest_out,
            "estimated_cost_usd": round(rest_cost, 4),
        },
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


DIGEST_SYSTEM_PROMPT = (
    "A developer has come back to a Claude Code agent they left running and cannot "
    "remember where they left it. The agent is now PAUSED and waiting -- it is NOT "
    "asking a multiple-choice question. Given a long stretch of recent terminal "
    "output, catch the developer up and hand them their next move.\n"
    "\n"
    "Return JSON with exactly three keys:\n"
    '  "recap":   2-4 strings. What this agent actually DID, oldest first. Past '
    "tense, concrete, one step each: \"ran the 73-test suite, 2 failed in "
    'test_api\", \"patched the pane gate and re-ran -- green\". Name real files, '
    "commands, numbers and errors from the output. Skip banners, prompts and "
    "chrome.\n"
    '  "state":   ONE string. Where it stands right now and what it is waiting '
    'for. Max ~20 words.\n'
    '  "options": 2-4 strings. Things the developer could SAY to the agent to '
    "move it forward, written as the message itself -- they are typed into the "
    "send box verbatim. Roughly 4-14 words each. Make them genuinely different "
    "moves, not three phrasings of \"continue\": at least one that advances the "
    "work, and, where the output justifies it, one that inspects/verifies "
    "instead. Ground every one in what is actually on screen.\n"
    "\n"
    "Rules:\n"
    "- If the output is too thin to tell (a bare shell, a fresh pane), return "
    'empty lists and a "state" of: nothing to catch up on\n'
    "- Never invent a file, test count, error or command that is not in the "
    "output. An honest \"cannot tell from the output\" beats a plausible "
    "fabrication.\n"
    "- Output ONLY the JSON object. No markdown fence, no commentary."
)


# Structured outputs: the shape is enforced by the API rather than asked for in prose,
# so a digest cannot come back as a chatty paragraph or a fenced block. _parse_digest
# below is kept anyway -- a refusal or a max_tokens truncation still yields text that
# does not conform, and the contract here is that a bad response degrades to empty
# rather than raising.
DIGEST_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "recap": {"type": "array", "items": {"type": "string"}},
            "state": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["recap", "state", "options"],
        "additionalProperties": False,
    },
}


def generate_digest(cleaned: list[str]) -> dict:
    """Catch-me-up digest for a PAUSED pane: what happened, where it stands, what
    you could say next.

    A different artifact from generate_summary, not a longer one. The summary is
    3-8 words about a pane that is mid-work and answers "is it still going"; this
    reads a much deeper tail and answers "what was I doing and what are my moves".
    That is why it gets its own model tier (settings.digest_model) and its own,
    larger, tail budget -- and why it is on-demand ONLY. Nothing auto-fires it.

    Degrades to empty lists on every failure, same contract as its siblings: the
    Mini App renders "nothing to catch up on" rather than an error banner, and a
    502 from the route still means "we never reached the bridge".
    """
    client = _get_client()
    empty = {"recap": [], "state": "", "options": []}
    if client is None or not cleaned:
        return empty
    tail_text = "\n".join(cleaned[-settings.digest_tail_lines:])[-16000:]
    try:
        msg = client.with_options(timeout=settings.digest_timeout).messages.create(
            model=settings.digest_model,
            max_tokens=settings.digest_max_tokens,
            system=DIGEST_SYSTEM_PROMPT,
            # Adaptive thinking is what Sonnet 5 does when `thinking` is omitted, so
            # this is the default stated out loud rather than a change -- worth
            # stating because max_tokens above is sized for thinking + text together.
            # effort=low keeps a catch-up read from turning into a research project.
            thinking={"type": "adaptive"},
            output_config={"effort": "low", "format": DIGEST_FORMAT},
            messages=[{"role": "user", "content": f"Recent terminal from the coding agent:\n{tail_text}"}],
        )
    except Exception as e:  # noqa: BLE001 -- any SDK/HTTP failure degrades to empty
        log.warning("digest: anthropic call failed: %s", e)
        return empty
    try:
        # Priced at the DIGEST model's rates, not the co-pilot's. Passing the model
        # through is the whole reason _record_usage takes it: this call is ~4x the
        # price of a suggestion, and folding it into one flat rate would make the
        # dollar figure the app displays quietly wrong in the direction of cheap.
        _record_usage(msg.usage, settings.digest_model)
    except Exception:  # noqa: BLE001
        pass
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return _parse_digest(text)


def _parse_digest(text: str) -> dict:
    """Tolerant of a fenced or chatty response; refuses to guess at a broken one."""
    out = {"recap": [], "state": "", "options": []}
    try:
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e <= s:
            return out
        data = json.loads(text[s : e + 1])
    except Exception:  # noqa: BLE001 -- malformed JSON degrades to empty, never raises
        log.warning("digest: unparseable model output")
        return out
    if not isinstance(data, dict):
        return out

    def _lines(key: str, cap: int, limit: int) -> list[str]:
        v = data.get(key)
        if not isinstance(v, list):
            return []
        return [" ".join(str(x).split())[:cap] for x in v if str(x).strip()][:limit]

    out["recap"] = _lines("recap", 160, 4)
    out["options"] = _lines("options", 120, 4)
    state = data.get("state")
    out["state"] = " ".join(str(state).split())[:160] if isinstance(state, str) else ""
    return out


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
