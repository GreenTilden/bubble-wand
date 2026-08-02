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
from fastapi.responses import HTMLResponse

from . import tmux, suggest, setup as setup_mod
from .config import settings
from .auth import require_token
from .models import (
    KeyRequest,
    PromptInfo,
    SendRequest,
    SendResponse,
    SetupRequest,
    SetupResponse,
    SuggestResponse,
    SummaryResponse,
    UsageResponse,
    TailResponse,
    Thread,
    ThreadsResponse,
)

log = logging.getLogger("clawatch")

app = FastAPI(title="clawatch-bridge", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    logging.basicConfig(level=logging.INFO)
    log.info("clawatch-bridge listening on %s:%s (tmux window %s)",
             settings.host, settings.port, settings.tmux_window)
    if settings.token_generated:
        log.warning("No CLAWATCH_TOKEN set — generated one for this run:")
        log.warning("    CLAWATCH_TOKEN=%s", settings.token)
        log.warning("Pass it to the watch app, or set CLAWATCH_TOKEN in the environment to pin it.")
    if not settings.configured:
        log.warning("UNCONFIGURED (no ANTHROPIC_API_KEY) — finish onboarding from a device on this LAN:")
        log.warning("    http://<this-box-LAN-IP>:%s/setup?t=%s", settings.port, settings.setup_token)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/api/threads", response_model=ThreadsResponse, dependencies=[Depends(require_token)])
async def get_threads() -> ThreadsResponse:
    try:
        threads = [Thread(**t) for t in tmux.list_threads()]
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return ThreadsResponse(threads=threads)


@app.get(
    "/api/threads/{index}/tail",
    response_model=TailResponse,
    dependencies=[Depends(require_token)],
)
async def get_tail(
    index: int,
    lines: int = Query(default=settings.default_tail_lines, ge=1, le=500),
    scrollback: bool = Query(default=False),
) -> TailResponse:
    try:
        captured = tmux.capture(index, lines=lines, scrollback=scrollback)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except tmux.TmuxError as e:
        raise HTTPException(status_code=502, detail=str(e))
    parsed = tmux.parse_prompt(captured)
    return TailResponse(
        index=index,
        pane=f"{settings.tmux_window}.{index}",
        lines=captured,
        capturedAt=datetime.now(timezone.utc).isoformat(),
        prompt=PromptInfo(**parsed) if parsed else None,
    )


@app.post(
    "/api/threads/{index}/send",
    response_model=SendResponse,
    dependencies=[Depends(require_token)],
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
    dependencies=[Depends(require_token)],
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
    "/api/threads/{index}/suggest",
    response_model=SuggestResponse,
    dependencies=[Depends(require_token)],
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
    dependencies=[Depends(require_token)],
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
    dependencies=[Depends(require_token)],
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
