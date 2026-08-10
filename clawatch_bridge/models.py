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
    # Durable identity. list_threads() has computed these since the Phase-0 fix, but
    # they were NOT declared here — and pydantic v2 drops extra kwargs silently, so
    # `Thread(**t)` in main.py discarded all three on the way out. They were computed
    # server-side and reached no client at all. Declared now because the pressure and
    # wash layers key on paneId, and a silently-dropped identity would have produced a
    # nudge system that keyed on pane INDEX — which is recomputed every poll.
    paneId: str | None = None
    repo: str | None = None     # cwd BASENAME only — never a full path
    # Claude Code status-bar meter, recovered from the pane status line; null when absent.
    model: str | None = None
    ctxTokens: int | None = None
    # The true precision of ctxTokens: it comes from a RENDERED, ROUNDED string
    # ("107k" -> 107000), so a consumer must never report a sub-resolution delta as
    # an improvement.
    ctxResolution: int | None = None
    ctxTier: str | None = None
    # Operator-threshold pressure (NONE | SOFT | HARD), computed by pressure.py.
    # DELIBERATELY NOT ctxTier: the vendor only emits a tier near its own ~1M limit,
    # so panes at 107k/98k carry no tier at all and the watch's meter reddened at
    # ~2.7x past the operator's actual hard stop.
    ctxPressure: str | None = None
    costUsd: float | None = None
    spendTokens: int | None = None  # cumulative session token spend (Σ in the bar)


class ThreadsResponse(BaseModel):
    threads: list[Thread]


class PromptOption(BaseModel):
    key: str            # the digit to send to select this option
    label: str
    selected: bool = False
    # Multi-select checkbox state ([ ]/[x] in the menu); null on single-select menus.
    checked: bool | None = None


class PromptInfo(BaseModel):
    """A parsed Claude Code interactive menu (permission gate, choice list, …)."""
    question: str = ""
    options: list[PromptOption]
    # True when the menu is a multi-select (digits TOGGLE; submission is separate).
    multiSelect: bool = False


class TailResponse(BaseModel):
    index: int
    pane: str
    lines: list[str]
    capturedAt: str
    prompt: PromptInfo | None = None
    # Paging fields. `before` echoes the request so a client that fired two pages
    # concurrently can tell which one landed -- the tail poll and an older-page
    # pull are both in flight on a phone more often than not.
    before: int = 0
    # Whether anything exists ABOVE the returned window. This is the only honest
    # way for a client to disable its "older" control: a short page means "the
    # buffer ended" only if you already know how deep the buffer went, and the
    # client does not. Computed from a row captured beyond the window and then
    # discarded, so it costs no extra tmux call.
    hasOlder: bool = False


class SendRequest(BaseModel):
    text: str = Field(default="", max_length=settings.max_send_len)
    submit: bool = True


class KeyRequest(BaseModel):
    # Validated server-side against a fixed allowlist — never a raw key name.
    action: str  # escape | interrupt | clear | enter | tab | up | down | left | right


class SendResponse(BaseModel):
    ok: bool


class SubmitMenuResponse(BaseModel):
    ok: bool
    # submitted: Enter was pressed on the ✔ Submit tab. advanced: Tab landed on
    # another question instead, so the watch should re-scrape and keep answering.
    submitted: bool = False
    advanced: bool = False


class WashStartResponse(BaseModel):
    """202 + a washId. The POST takes NO body: a wash has exactly one meaning, and
    a body would invite a client to start specifying how to type into a live
    session."""
    washId: str
    stage: str = "QUEUED"


class WashStatusResponse(BaseModel):
    washId: str
    stage: str            # QUEUED | CLEAR | VERIFY | RESEED | DONE
    outcome: str | None = None   # ok | cleared_not_reseeded | failed | blocked
    reseed: str | None = None    # command | none


class SuggestResponse(BaseModel):
    suggestions: list[str] = []


class SummaryResponse(BaseModel):
    # One short present-tense line describing what a WORKING thread is doing;
    # empty string on any failure so the watch degrades to the plain status.
    summary: str = ""


class ModelUsage(BaseModel):
    """One model's slice of the meter, priced where the price table lives.

    Declared as a MODEL rather than left inside a dict[str, dict[str, int]]: that
    annotation predates per-model dollars, and under it a fractional cost is a
    hard ValidationError (a 500, not a silent zero -- checked, not assumed). Loud
    is better than lossy, but declaring the real shape is better than both.
    """
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    # False when the model has no entry in the price table and the co-pilot rate
    # was used instead. Travels with the number so an estimate cannot be read as
    # a quote -- the same reason cost_basis exists on the total.
    priced: bool = False


class UnattributedUsage(BaseModel):
    """Spend the per-model split cannot account for -- tokens recorded before
    per-model metering existed. Present so the parts sum to the headline; a
    breakdown that quietly does not is how a meter loses its credibility."""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


class UsageResponse(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    # A response_model DROPS anything it does not declare, silently. Both fields below
    # existed in suggest.get_usage() for a whole loop while the HTTP surface quietly
    # served the old shape -- unit tests asserted the function, not the route. Any new
    # key in get_usage() must be declared here too; test_api_usage_exposes_* pins it.
    cost_basis: str = ""      # tokens are measured; dollars are a list-price estimate
    since: str | None = None  # first-ever recorded call; a cumulative total needs a start
    # Per-model token split. Declared for the same reason cost_basis and since had to
    # be: an undeclared key is dropped silently by the response_model, and the route
    # would serve a shape the unit tests never see because they test the function.
    by_model: dict[str, ModelUsage] = {}
    unattributed: UnattributedUsage = Field(default_factory=UnattributedUsage)


class DigestResponse(BaseModel):
    """Catch-me-up digest for a paused pane. All three fields degrade to empty on any
    Haiku/Sonnet failure -- an empty digest is 'nothing to catch up on', while a 502
    from the route means the bridge was never reached. They must stay tellable apart."""
    recap: list[str] = []
    state: str = ""
    options: list[str] = []


class SetupRequest(BaseModel):
    setup_token: str
    api_key: str = Field(min_length=8, max_length=300)


class SetupResponse(BaseModel):
    ok: bool
    token: str
    connectUrl: str
    note: str = ""


class HistoryResponse(BaseModel):
    """Paged session history, read from Claude's transcript rather than tmux.

    `lines` are LOGICAL lines -- a paragraph arrives whole, however long -- so the
    client wraps them to its own width. That is the property a captured pane can
    never have, and the reason this route exists at all.
    """
    index: int
    pane: str
    lines: list[str]
    capturedAt: str
    before: int = 0
    hasOlder: bool = False
    # Which transcript this came from, and how sure we are it is THIS pane's.
    # "matched" = the pane's own screen was found in the file · "only" = nothing
    # to disambiguate · "mtime" = guessed by recency, and the client must say so
    # · "none" = no transcript for this cwd at all.
    session: str | None = None
    confidence: str = "none"

