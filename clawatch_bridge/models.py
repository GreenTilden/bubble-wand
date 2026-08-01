"""Pydantic request/response models mirrored by the watch app's DTOs."""
from __future__ import annotations

from pydantic import BaseModel, Field

from .config import settings


class Thread(BaseModel):
    index: int
    pane: str
    command: str
    status: str  # NEEDS_INPUT | WORKING | IDLE
    glyph: str
    title: str
    label: str
    hasPrompt: bool = False  # an interactive menu is on screen (tappable, no dictation)
    # Claude Code status-bar meter, recovered from the pane status line; null when absent.
    model: str | None = None
    ctxTokens: int | None = None
    ctxTier: str | None = None
    costUsd: float | None = None


class ThreadsResponse(BaseModel):
    threads: list[Thread]


class PromptOption(BaseModel):
    key: str            # the digit to send to select this option
    label: str
    selected: bool = False


class PromptInfo(BaseModel):
    """A parsed Claude Code interactive menu (permission gate, choice list, …)."""
    question: str = ""
    options: list[PromptOption]


class TailResponse(BaseModel):
    index: int
    pane: str
    lines: list[str]
    capturedAt: str
    prompt: PromptInfo | None = None


class SendRequest(BaseModel):
    text: str = Field(default="", max_length=settings.max_send_len)
    submit: bool = True


class KeyRequest(BaseModel):
    # Validated server-side against a fixed allowlist — never a raw key name.
    action: str  # escape | interrupt | clear | enter


class SendResponse(BaseModel):
    ok: bool


class SuggestResponse(BaseModel):
    suggestions: list[str] = []


class UsageResponse(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
