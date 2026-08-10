"""Runtime configuration, read from the environment.

Kept dependency-free (plain os.getenv) so the only install is fastapi + uvicorn.
"""
from __future__ import annotations

import os
import secrets


class Settings:
    def __init__(self) -> None:
        # Bind address. Default 0.0.0.0 so a watch anywhere on the LAN can reach it;
        # set CLAWATCH_HOST=<your-lan-ip> to restrict it to one interface.
        self.host: str = os.getenv("CLAWATCH_HOST", "0.0.0.0")
        self.port: int = int(os.getenv("CLAWATCH_PORT", "8793"))

        # The tmux panes that ARE the threads. Two forms, told apart by the colon
        # (tmux's own convention -- a session name cannot contain one):
        #
        #   dev:1   ONE WINDOW      every pane in that window
        #   dev     WHOLE SESSION   every pane in every window
        #
        # The session form exists because a window's panes all share one width,
        # and the narrowest client viewing that window sets it. Five Claude panes
        # tiled in one window is therefore five threads whose print width is
        # decided by whoever attached last -- a 53-column phone divides to ~10
        # columns each, and Claude hard-wraps its output at that width
        # PERMANENTLY (it emits real newlines; no later resize reflows them).
        # One pane per window decouples them: tmux sizes each window from the
        # clients actually viewing it, so the phone only narrows the one it is
        # looking at.
        #
        # Default is the window form, unchanged, and CLAWATCH_TMUX_WINDOW is
        # still read -- a deployed unit keeps its current behaviour byte for byte
        # until someone edits the env on purpose.
        #
        # The client only ever sends an integer index; the server resolves it
        # against its own enumeration of this scope, so no user-controlled string
        # ever reaches a tmux target argument.
        self.tmux_scope: str = (
            os.getenv("CLAWATCH_TMUX_SCOPE")
            or os.getenv("CLAWATCH_TMUX_WINDOW")
            or "dev:1"
        )

        # Panes whose title/label/repo/command contain any of these substrings are
        # not exposed to ANY client -- absent from the list, 404 when addressed.
        # Empty default is deliberate: this repo is public, so the mechanism ships
        # and the patterns stay in the deployed unit. See pane_filter.py.
        self.excluded_pane_patterns: tuple[str, ...] = tuple(
            p.strip()
            for p in os.getenv("CLAWATCH_EXCLUDED_PANE_PATTERNS", "").split(",")
            if p.strip()
        )

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
        # A working-thread summary is one short line; keep the budget tiny.
        self.summary_max_tokens: int = int(os.getenv("CLAWATCH_SUMMARY_MAX_TOKENS", "40"))
        self.prompt_summary_max_tokens: int = int(os.getenv("CLAWATCH_PROMPT_SUMMARY_MAX_TOKENS", "60"))
        self.suggest_price_in: float = float(os.getenv("CLAWATCH_SUGGEST_PRICE_IN", "1.0"))
        self.suggest_price_out: float = float(os.getenv("CLAWATCH_SUGGEST_PRICE_OUT", "5.0"))

        # --- Catch-me-up digest ----------------------------------------------
        #
        # A different job from the three co-pilot calls above, so it gets its own
        # model tier rather than sharing suggest_model. Those answer "is it still
        # going" in 3-8 words off a 40-line tail; this reads ~150 lines and has to
        # work out what CHANGED and what the operator's moves are. On-demand only --
        # nothing auto-fires it, which is what makes the more expensive tier
        # affordable.
        self.digest_model: str = os.getenv("CLAWATCH_DIGEST_MODEL", "claude-sonnet-5")
        self.digest_tail_lines: int = int(os.getenv("CLAWATCH_DIGEST_TAIL_LINES", "150"))
        # Sonnet 5 runs ADAPTIVE THINKING when `thinking` is omitted, and max_tokens
        # caps thinking + response text TOGETHER. Sized for both: the visible answer
        # is ~300 tokens, the rest is headroom so a thoughtful digest cannot truncate
        # mid-sentence. Sizing this like the 150-token suggest budget would have
        # produced empty-looking digests on exactly the hard panes that need one.
        self.digest_max_tokens: int = int(os.getenv("CLAWATCH_DIGEST_MAX_TOKENS", "2000"))
        # Longer than suggest's 6s: this is a deliberate tap with a spinner, not a
        # call racing a 30s poll window.
        self.digest_timeout: float = float(os.getenv("CLAWATCH_DIGEST_TIMEOUT", "30"))

        # Per-model list prices, $/Mtok. The meter used to hold ONE pair, which was
        # fine while every call was Haiku and silently wrong the moment a second
        # tier existed -- a Sonnet digest priced at Haiku's rates under-reports by
        # ~3x, and it under-reports the exact call the operator is deciding whether
        # to tap again. Unknown models fall back to the digest rate: over-stating a
        # cost is a recoverable error, under-stating it is the one that misleads.
        # (Sonnet 5 carries an introductory $2/$10 through 2026-08-31; the standard
        # $3/$15 is used so the figure does not quietly go under on 09-01.)
        self.model_prices: dict[str, tuple[float, float]] = {
            self.suggest_model: (self.suggest_price_in, self.suggest_price_out),
            self.digest_model: (
                float(os.getenv("CLAWATCH_DIGEST_PRICE_IN", "3.0")),
                float(os.getenv("CLAWATCH_DIGEST_PRICE_OUT", "15.0")),
            ),
        }

        # Bearer token. If unset, generate one and log it at startup so local
        # testing "just works"; in production pass CLAWATCH_TOKEN explicitly.
        token = os.getenv("CLAWATCH_TOKEN")
        self.token_generated: bool = token is None
        self.token: str = token or secrets.token_urlsafe(24)

        # --- Self-serve provisioning (the sellable, own-your-key path) -------
        # Where provisioned secrets are persisted. A customer instance points
        # CLAWATCH_ENV_FILE at its own file so instances never share secrets.
        self.env_file: str = os.getenv(
            "CLAWATCH_ENV_FILE", os.path.expanduser("~/clawatch-bridge/clawatch.env")
        )
        # One-time code gating /setup: taken from CLAWATCH_SETUP_TOKEN if the
        # installer set one (so it can print a ready-to-open URL), else minted
        # fresh each boot. Only meaningful while UNCONFIGURED; inert after.
        self.setup_token: str = os.getenv("CLAWATCH_SETUP_TOKEN") or secrets.token_urlsafe(16)
        # Onboarding is LAN/loopback-only by default; escape hatch for odd nets.
        self.allow_remote_setup: bool = os.getenv(
            "CLAWATCH_ALLOW_REMOTE_SETUP", ""
        ).lower() in ("1", "true", "yes")

        # --- Context pressure, nudges, and the server-side wash --------------
        # OPERATOR thresholds, NOT the vendor's ctxTier: the vendor only emits a
        # tier near its own ~1M limit, so panes at 107k/98k carry none at all.
        self.ctx_soft_tokens: int = int(os.getenv("CLAWATCH_CTX_SOFT_TOKENS", "120000"))
        self.ctx_hard_tokens: int = int(os.getenv("CLAWATCH_CTX_HARD_TOKENS", "150000"))
        # Re-arm at 0.8x so a thread parked just over a threshold buzzes ONCE,
        # not on every poll forever.
        self.ctx_rearm_ratio: float = float(os.getenv("CLAWATCH_CTX_REARM_RATIO", "0.8"))
        # A thread first seen ABOVE a threshold starts disarmed, so a bridge
        # restart never fires a burst. Set 0 for threshold testing.
        self.ctx_seed_baseline: bool = os.getenv(
            "CLAWATCH_CTX_SEED_BASELINE", "1"
        ).lower() not in ("0", "false", "no")
        self.ctx_sample_delta: int = int(os.getenv("CLAWATCH_CTX_SAMPLE_DELTA", "5000"))
        self.ctx_sample_interval: float = float(
            os.getenv("CLAWATCH_CTX_SAMPLE_INTERVAL", "300"))

        # Append-only JSONL, 0600. NOT /var/log: this is an unprivileged
        # `systemd --user` unit and a fleet path cannot be a committed default.
        self.event_log: str = os.getenv("CLAWATCH_EVENT_LOG") or os.path.join(
            os.getenv("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
            "bubble-wand", "wash-events.jsonl",
        )
        # Cumulative co-pilot spend. Same state root as the event log, same reasoning.
        # DURABLE by design: an in-memory counter silently means "since whatever restart
        # last happened", which is a worse lie than no number at all — you cannot tell a
        # quiet day from a service bounce. Carries no pane content, only totals.
        self.usage_state: str = os.getenv("CLAWATCH_USAGE_STATE") or os.path.join(
            os.getenv("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
            "bubble-wand", "suggest-usage.json",
        )
        self.event_max_bytes: int = int(os.getenv("CLAWATCH_EVENT_MAX_BYTES", "5000000"))
        self.event_keep: int = int(os.getenv("CLAWATCH_EVENT_KEEP", "3"))
        # Strict mode RAISES on a schema violation instead of dropping the field.
        # Tests only — in production a telemetry bug must never break a poll.
        self.event_strict: bool = os.getenv(
            "CLAWATCH_EVENT_STRICT", "").lower() in ("1", "true", "yes")

        # A wash types /clear into whatever is running in the pane. ALLOWLIST,
        # never a denylist: a pane that has dropped to a shell must not receive
        # "/clear" as a bash command.
        self.wash_enabled: bool = os.getenv(
            "CLAWATCH_WASH_ENABLED", "1").lower() not in ("0", "false", "no")
        self.wash_pane_commands: tuple[str, ...] = tuple(
            c.strip().lower()
            for c in os.getenv("CLAWATCH_WASH_PANE_COMMANDS", "claude").split(",")
            if c.strip()
        )
        self.wash_clear_attempts: int = int(os.getenv("CLAWATCH_WASH_CLEAR_ATTEMPTS", "2"))
        # The re-seed: paste the pre-clear tail back, on every pane. There is no
        # slash-command lane any more -- CLAWATCH_RESEED_COMMAND and
        # CLAWATCH_RESEED_PROBE are no longer read (removed 2026-08-08; one lane
        # of work is one lane of failure to test) and can be dropped from env.
        # Bounded on BOTH axes -- an unbounded paste would refill the context the
        # wash just emptied, which is a self-defeating re-seed, and a long paste
        # is also a long window in which the pane can change under us.
        self.reseed_tail_enabled: bool = os.getenv(
            "CLAWATCH_RESEED_TAIL", "1").lower() not in ("0", "false", "no")
        self.reseed_tail_lines: int = int(os.getenv("CLAWATCH_RESEED_TAIL_LINES", "120"))
        self.reseed_tail_max_chars: int = int(
            os.getenv("CLAWATCH_RESEED_TAIL_MAX_CHARS", "8000"))
        # The automation seam. Ships OFF; turning it on is this one variable.
        self.autowash_enabled: bool = os.getenv(
            "CLAWATCH_AUTOWASH", "").lower() in ("1", "true", "yes")

    @property
    def configured(self) -> bool:
        """Configured once a customer API key is present. Until then the bridge
        serves /setup; after, /setup is inert (409). An operator instance with a
        key already in env is therefore 'configured' and unaffected."""
        return bool(self.anthropic_api_key)

    def apply_provision(self, api_key: str, token: str) -> None:
        """Live-apply provisioned secrets to the running process (no restart)."""
        self.anthropic_api_key = api_key
        self.suggest_enabled = True
        self.token = token
        self.token_generated = False


settings = Settings()
