"""FastAPI app exposing the tmux `dev` group to the wrist co-pilot.

Endpoints (all under /api require `Authorization: Bearer <CLAWATCH_TOKEN>`):
  GET  /healthz                         -> {"ok": true}                (no auth)
  GET  /api/threads                     -> {"threads": [...]}
  GET  /api/threads/{index}/tail        -> {"index","pane","lines","capturedAt","prompt"}
  POST /api/threads/{index}/send        -> {"ok": true}
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query

from . import tmux, suggest
from .config import settings
from .auth import require_token
from .models import (
    KeyRequest,
    PromptInfo,
    SendRequest,
    SendResponse,
    SuggestResponse,
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
