"""Runtime configuration, read from the environment.

Kept dependency-free (plain os.getenv) so the only install is fastapi + uvicorn.
"""
from __future__ import annotations

import os
import secrets


class Settings:
    def __init__(self) -> None:
        # Bind address. Default 0.0.0.0 so a watch anywhere on the LAN can reach it;
        # set CLAWATCH_HOST=192.168.0.22 to restrict to fenton's LAN interface.
        self.host: str = os.getenv("CLAWATCH_HOST", "0.0.0.0")
        self.port: int = int(os.getenv("CLAWATCH_PORT", "8793"))

        # The tmux window whose panes ARE the threads. Panes are addressed as
        # "<window>.<index>", e.g. dev:1.1 .. dev:1.N. The client only ever sends
        # the integer index; the server constructs the target, so there is no
        # user-controlled string in the tmux target argument.
        self.tmux_window: str = os.getenv("CLAWATCH_TMUX_WINDOW", "dev:1")

        self.default_tail_lines: int = int(os.getenv("CLAWATCH_TAIL_LINES", "40"))
        self.max_send_len: int = int(os.getenv("CLAWATCH_MAX_SEND_LEN", "4000"))

        # LLM-backed momentum suggestions (wrist co-pilot). Disabled unless an
        # ANTHROPIC_API_KEY is present (read explicitly so we can skip building the
        # client and return [] instead of erroring). Key lives in clawatch.env.
        self.anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
        self.suggest_enabled: bool = bool(self.anthropic_api_key)
        self.suggest_model: str = os.getenv("CLAWATCH_SUGGEST_MODEL", "claude-haiku-4-5-20251001")
        self.suggest_max_tokens: int = int(os.getenv("CLAWATCH_SUGGEST_MAX_TOKENS", "150"))
        self.suggest_timeout: float = float(os.getenv("CLAWATCH_SUGGEST_TIMEOUT", "6"))
        self.suggest_tail_lines: int = int(os.getenv("CLAWATCH_SUGGEST_TAIL_LINES", "40"))

        # Bearer token. If unset, generate one and log it at startup so local
        # testing "just works"; in production pass CLAWATCH_TOKEN explicitly.
        token = os.getenv("CLAWATCH_TOKEN")
        self.token_generated: bool = token is None
        self.token: str = token or secrets.token_urlsafe(24)


settings = Settings()
