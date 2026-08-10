"""FastAPI app exposing the tmux `dev` group to the wrist co-pilot.

Endpoints (all under /api require `Authorization: Bearer <CLAWATCH_TOKEN>`):
  GET  /healthz                         -> {"ok": true}                (no auth)
  GET  /api/threads                     -> {"threads": [...]}
  GET  /api/threads/{index}/tail        -> {"index","pane","lines","capturedAt","prompt"}
  POST /api/threads/{index}/send        -> {"ok": true}
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import tmux, suggest, setup as setup_mod, pressure, wash as wash_mod, transcript
from .config import settings
from .auth import require_token
from .models import (
    DigestResponse,
    KeyRequest,
    PromptInfo,
    SendRequest,
    SendResponse,
    WashStartResponse,
    WashStatusResponse,
    SetupRequest,
    SetupResponse,
    SubmitMenuResponse,
    SuggestResponse,
    SummaryResponse,
    UsageResponse,
    TailResponse,
    HistoryResponse,
    Thread,
    ThreadsResponse,
)

log = logging.getLogger("clawatch")

app = FastAPI(title="clawatch-bridge", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    logging.basicConfig(level=logging.INFO)
    # The scope's SHAPE is spelled out, not left to be inferred from the string.
    # "dev" and "dev:1" differ by one character and by every pane in the session,
    # and the failure mode of reading it wrong is a thread list that looks
    # plausible while addressing the wrong panes.
    log.info("clawatch-bridge listening on %s:%s (tmux scope %s — %s)",
             settings.host, settings.port, settings.tmux_scope,
             "one window" if ":" in settings.tmux_scope else "whole session, every window")
    # COUNT, never the patterns themselves — a log line is a surface too, and the
    # patterns name what is being kept off a screen. Logged unconditionally
    # because "0 exclusion patterns" is the reading that matters: the filter fails
    # OPEN when unset, so silence would look identical to it working.
    log.info("pane exclusion: %d pattern(s) active", len(settings.excluded_pane_patterns))
    if settings.token_generated:
        log.warning("No CLAWATCH_TOKEN set — generated one for this run:")
        log.warning("    CLAWATCH_TOKEN=%s", settings.token)
        log.warning("Pass it to the watch app, or set CLAWATCH_TOKEN in the environment to pin it.")
    if not settings.configured:
        log.warning("UNCONFIGURED (no ANTHROPIC_API_KEY) — finish onboarding from a device on this LAN:")
        log.warning("    http://<this-box-LAN-IP>:%s/setup?t=%s", settings.port, settings.setup_token)


@app.exception_handler(tmux.PaneIdentityError)
async def _pane_identity_handler(request: Request, exc: tmux.PaneIdentityError):
    """409, not 404. The pane exists; it is not the one the caller meant.

    A client that gets 404 should drop the pane from its list. A client that
    gets this should REFRESH and re-point -- different fact, different repair,
    so they cannot share a status code.
    """
    log.warning("pane identity mismatch: %s", exc)
    return JSONResponse(status_code=409, content={"detail": str(exc)})


async def require_pane_allowed(index: int) -> None:
    """404 an excluded pane, so the list and the addressed routes agree.

    Same 502-not-500 reasoning as require_pane_identity below: as a dependency
    this runs outside the routes' own `except TmuxError` handlers, so the
    mapping has to be restated here to keep the contract identical wherever the
    failure happens.
    """
    try:
        tmux.assert_pane_allowed(index)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))


async def require_pane_identity(
    index: int,
    # Sent by clients that track pane identity (the Mini App). Absent from the
    # watch's requests, where it is a no-op -- see tmux.assert_pane_identity.
    paneId: str | None = Query(default=None),
) -> None:
    try:
        tmux.assert_pane_identity(index, paneId)
    except tmux.TmuxError as e:
        # Every route BODY maps a tmux failure to 502. Running this check as a
        # dependency put it outside those handlers, so the same failure came back
        # 500 -- and the client's own error mapping keys on 502 to say "could not
        # reach the bridge's tmux" rather than a generic server error. Keep the
        # contract identical wherever the failure happens.
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/api/threads", response_model=ThreadsResponse, dependencies=[Depends(require_token)])
async def get_threads() -> ThreadsResponse:
    try:
        raw = tmux.list_threads()
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))
    # Pressure is computed HERE, on the poll, rather than by the client: three
    # client loops across two watches hit this endpoint, and a client-side
    # decision would fire (and log) the same crossing up to three times.
    for t in raw:
        t["ctxPressure"] = pressure.observe(t)
    wash_mod.maybe_autowash(raw)   # ships disabled; see wash.maybe_autowash
    return ThreadsResponse(threads=[Thread(**t) for t in raw])


@app.get(
    "/api/threads/{index}/tail",
    response_model=TailResponse,
    dependencies=[Depends(require_token), Depends(require_pane_allowed), Depends(require_pane_identity)],
)
async def get_tail(
    index: int,
    lines: int = Query(default=settings.default_tail_lines, ge=1, le=500),
    scrollback: bool = Query(default=False),
    # Colour passthrough, opt-in per client. The watch does NOT ask for it --
    # it strips colour deliberately, because on a 1.4" screen syntax colour is
    # noise. A phone has the room, and tmux colour is a genuine "where am I"
    # signal there. Default false so an existing client cannot be handed
    # escape sequences it has no renderer for.
    ansi: bool = Query(default=False),
    # Bounded scrollback. WITHOUT this the `lines` above is a ceiling and never a
    # floor -- bare capture-pane returns the visible pane, so asking a 45-row pane
    # for 100 lines returns 45 with no error. tmux.capture has taken `history`
    # since L15; this route did not expose it, so a client asking for it got the
    # screen and no way to tell. Off by default: the watch's poll is unchanged.
    history: bool = Query(default=False),
    # Paging. `before` rows above the newest row is where the returned window
    # ENDS, so before=0 is the live tail and before=150 is the screen before it.
    # Capped well above any plausible read-back so a client bug cannot ask tmux
    # for a million-row capture, and NOT bounded by `lines`' 500: the whole point
    # is that depth is reached by walking, not by one enormous request.
    before: int = Query(default=0, ge=0, le=20000),
) -> TailResponse:
    try:
        if before:
            # Deliberately its own branch rather than folding `before` into
            # capture(): before=0 must stay byte-identical to what every client
            # polls today, including the watch, which does not know this
            # parameter exists. A paging bug can then only ever break paging.
            captured, has_older = tmux.capture_page(
                index, lines=lines, before=before, ansi=ansi
            )
        else:
            captured = tmux.capture(
                index, lines=lines, scrollback=scrollback, ansi=ansi, history=history
            )
            # Only meaningful for a client that asked to be positioned at all.
            # A live-tail poll gets False and ignores it.
            has_older = False
        # Resolved, not formatted from config: under a session scope the window
        # differs per pane, so `f"{scope}.{index}"` would report an address that
        # does not exist -- and would do it in the field a client shows the
        # operator to confirm WHICH pane they are reading.
        address = tmux.pane_address(index)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))
    # The menu parser NEVER sees escapes: a colour run inside an option label
    # would break the option regex, and the labels it returns are rendered as
    # text by clients that cannot decode colour. No-op when ansi is false.
    parsed = tmux.parse_prompt([tmux.strip_ansi(ln) for ln in captured])
    return TailResponse(
        index=index,
        pane=address,
        lines=captured,
        capturedAt=datetime.now(timezone.utc).isoformat(),
        prompt=PromptInfo(**parsed) if parsed else None,
        before=before,
        hasOlder=has_older,
    )


@app.get(
    "/api/threads/{index}/history",
    response_model=HistoryResponse,
    dependencies=[Depends(require_token), Depends(require_pane_allowed), Depends(require_pane_identity)],
)
async def get_history(
    index: int,
    lines: int = Query(default=80, ge=1, le=400),
    before: int = Query(default=0, ge=0, le=100000),
) -> HistoryResponse:
    """Real scrollback, from the transcript rather than from tmux.

    The tail routes read a pane. This reads what Claude WROTE, which for a pane on
    the alternate screen is the only record that exists above the visible rows --
    see transcript.py for the measurement. It is also better than the buffer we
    thought we had: unwrapped text, so the client re-flows it to its own width.

    Behind the same three gates as every pane route, and the identity gate matters
    MORE here, not less: serving the wrong pane's history is a privacy failure
    rather than a display glitch. The cwd is read server-side from the live pane;
    no client-supplied path reaches the filesystem.
    """
    try:
        cwd = tmux.pane_cwd(index)
        # The pane's current screen is the discriminator between two sessions in
        # the same repo. Captured here, never sent by the client -- a client-
        # supplied "screen" would let a caller fish for a transcript by guessing
        # at its content.
        pane_text = "\n".join(tmux.capture(index, lines=60, scrollback=False))
        address = tmux.pane_address(index)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))
    try:
        rows, has_older, meta = transcript.page(
            cwd, pane_text, lines=lines, before=before
        )
    except OSError as e:
        # A transcript that cannot be read is a 502 rather than an empty 200:
        # "no history" and "history unreadable" are different facts and the
        # client's answer differs (say so, vs disable the control).
        raise HTTPException(status_code=502, detail=f"transcript unreadable: {e}")
    return HistoryResponse(
        index=index,
        pane=address,
        lines=rows,
        capturedAt=datetime.now(timezone.utc).isoformat(),
        before=before,
        hasOlder=has_older,
        session=meta.get("session"),
        confidence=meta.get("confidence", "none"),
    )


@app.post(
    "/api/threads/{index}/send",
    response_model=SendResponse,
    dependencies=[Depends(require_token), Depends(require_pane_allowed), Depends(require_pane_identity)],
)
async def post_send(index: int, body: SendRequest) -> SendResponse:
    try:
        tmux.send(index, text=body.text, submit=body.submit)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return SendResponse(ok=True)


@app.post(
    "/api/threads/{index}/key",
    response_model=SendResponse,
    dependencies=[Depends(require_token), Depends(require_pane_allowed), Depends(require_pane_identity)],
)
async def post_key(index: int, body: KeyRequest) -> SendResponse:
    try:
        tmux.send_key(index, action=body.action)
    except ValueError as e:
        # bad index (404) vs bad action (400)
        code = 400 if "action" in str(e) else 404
        raise HTTPException(status_code=code, detail=str(e))
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return SendResponse(ok=True)

@app.post(
    "/api/threads/{index}/wash",
    response_model=WashStartResponse,
    status_code=202,
    dependencies=[Depends(require_token), Depends(require_pane_allowed), Depends(require_pane_identity)],
)
async def post_wash(index: int) -> WashStartResponse:
    """Start a context wash. Returns 202 immediately with a washId; poll
    GET .../wash/{washId} for the stage.

    Async on purpose: a wash takes seconds (escape, clear, verify, re-seed), and
    holding the request open would run past the watch client's 15s callTimeout —
    the wash would succeed while the caller saw a network failure.

    Takes NO body. A wash has exactly one meaning; a body would invite a client to
    start specifying how to type into a live Claude session.
    """
    wash_id, blocked = wash_mod.request_wash(index, trigger="manual")
    if wash_id is None:
        code = 404 if blocked in ("pane_missing", "index_invalid") else 409
        raise HTTPException(status_code=code, detail=blocked or "blocked")
    return WashStartResponse(washId=wash_id)


@app.get(
    "/api/threads/{index}/wash/{wash_id}",
    response_model=WashStatusResponse,
    dependencies=[Depends(require_token), Depends(require_pane_allowed)],
)
async def get_wash(index: int, wash_id: str) -> WashStatusResponse:
    w = wash_mod.status(wash_id)
    if w is None or w.get("index") != index:
        raise HTTPException(status_code=404, detail="unknown wash")
    return WashStatusResponse(
        washId=wash_id, stage=w["stage"], outcome=w.get("outcome"),
        reseed=w.get("reseed"),
    )


@app.post(
    "/api/threads/{index}/submit-menu",
    response_model=SubmitMenuResponse,
    dependencies=[Depends(require_token), Depends(require_pane_allowed), Depends(require_pane_identity)],
)
async def post_submit_menu(index: int) -> SubmitMenuResponse:
    """Advance a multi-select menu toward ✔ Submit (Tab, then Enter only when no
    further question renders). The watch's 'Submit these' button lands here."""
    try:
        result = tmux.submit_menu(index)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return SubmitMenuResponse(ok=True, **result)


@app.post(
    "/api/threads/{index}/suggest",
    response_model=SuggestResponse,
    dependencies=[Depends(require_token), Depends(require_pane_allowed), Depends(require_pane_identity)],
)
async def post_suggest(index: int) -> SuggestResponse:
    try:
        captured = tmux.capture(index, lines=settings.suggest_tail_lines, scrollback=False)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))
    parsed = tmux.parse_prompt(captured)
    return SuggestResponse(suggestions=suggest.generate_suggestions(captured, parsed))


@app.post(
    "/api/threads/{index}/summary",
    response_model=SummaryResponse,
    dependencies=[Depends(require_token), Depends(require_pane_allowed), Depends(require_pane_identity)],
)
async def post_summary(index: int) -> SummaryResponse:
    try:
        captured = tmux.capture(index, lines=settings.suggest_tail_lines, scrollback=False)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return SummaryResponse(summary=suggest.generate_summary(captured))


@app.post(
    "/api/threads/{index}/prompt-summary",
    response_model=SummaryResponse,
    dependencies=[Depends(require_token), Depends(require_pane_allowed), Depends(require_pane_identity)],
)
async def post_prompt_summary(index: int) -> SummaryResponse:
    """One short line describing the DECISION a paused thread is asking for, so the
    watch can show it above the option chips. Menu-aware; degrades to \"\" if there
    is no live menu or Haiku is unavailable."""
    try:
        captured = tmux.capture(index, lines=settings.suggest_tail_lines, scrollback=False)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))
    parsed = tmux.parse_prompt(captured)
    if not parsed:
        return SummaryResponse(summary="")
    return SummaryResponse(summary=suggest.generate_prompt_summary(captured, parsed))


@app.post(
    "/api/threads/{index}/digest",
    response_model=DigestResponse,
    dependencies=[Depends(require_token), Depends(require_pane_allowed), Depends(require_pane_identity)],
)
async def post_digest(index: int) -> DigestResponse:
    """Catch-me-up digest for a pane you left running: what it did, where it stands,
    what you could say next.

    The only route here that reads with `history=True` -- it asks tmux for lines ABOVE
    the visible screen, because "what has this been doing" is a question the current
    screen cannot answer. Everything else deliberately reads the screen only.

    Deliberately NOT gated on pane status. Whether a pane counts as "paused and not
    mid-question" is a judgement the client already makes to decide whether to offer
    the button, and duplicating it here would mean two definitions that can disagree.
    The bridge answers what it is asked; the Mini App decides when to ask.
    """
    try:
        captured = tmux.capture(
            index, lines=settings.digest_tail_lines, scrollback=False, history=True
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return DigestResponse(**suggest.generate_digest(captured))


@app.get("/api/usage", response_model=UsageResponse, dependencies=[Depends(require_token)])
async def get_usage() -> UsageResponse:
    return UsageResponse(**suggest.get_usage())


# --- Self-serve onboarding (LAN-only; inert once configured) --------------- #

@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, t: str = Query(default="")) -> HTMLResponse:
    """Onboarding page where a customer pastes their own Anthropic key."""
    if settings.configured:
        return HTMLResponse(setup_mod.ALREADY_HTML, status_code=409)
    if not setup_mod.caller_is_local(request):
        return HTMLResponse(setup_mod.FORBIDDEN_HTML, status_code=403)
    return HTMLResponse(setup_mod.setup_page(t))


@app.post("/api/setup", response_model=SetupResponse)
async def api_setup(request: Request, body: SetupRequest) -> SetupResponse:
    if settings.configured:
        raise HTTPException(status_code=409, detail="already configured")
    if not setup_mod.caller_is_local(request):
        raise HTTPException(status_code=403, detail="setup allowed only from the local network")
    if not hmac.compare_digest(body.setup_token, settings.setup_token):
        raise HTTPException(status_code=401, detail="invalid or expired setup code")
    api_key = body.api_key.strip()
    if not api_key.startswith("sk-ant-"):
        raise HTTPException(status_code=400, detail="that doesn't look like an Anthropic API key (expected sk-ant-…)")
    token = setup_mod.provision(api_key)
    base = f"http://{request.url.hostname}:{settings.port}"
    return SetupResponse(
        ok=True,
        token=token,
        connectUrl=base,
        note="Enter this URL and token in the watch app's Settings, then pair.",
    )
