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

        # Bearer token. If unset, generate one and log it at startup so local
        # testing "just works"; in production pass CLAWATCH_TOKEN explicitly.
        token = os.getenv("CLAWATCH_TOKEN")
        self.token_generated: bool = token is None
        self.token: str = token or secrets.token_urlsafe(24)


settings = Settings()
