"""The co-pilot's spend counter must survive a restart.

An in-memory total silently means "since whatever restart last happened", so a quiet
day and a service bounce read identically — which is a worse number than none. These
tests pin the durability, the `since` stamp that makes a cumulative total readable,
and the rule that no accounting failure may ever break a suggestion.

della cycle-66 L14 · duckminster elimination-ledger v1.2 (product-embedded inference).
"""
import json
import os

import pytest

from clawatch_bridge import suggest
from clawatch_bridge.config import settings


class FakeUsage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


@pytest.fixture
def state(tmp_path, monkeypatch):
    p = tmp_path / "nested" / "suggest-usage.json"     # nested: makedirs must handle it
    monkeypatch.setattr(settings, "usage_state", str(p))
    monkeypatch.setattr(suggest, "_usage",
                        {"calls": 0, "input_tokens": 0, "output_tokens": 0, "since": None})
    return p


def test_counters_survive_a_restart(state):
    suggest._record_usage(FakeUsage(100, 20))
    suggest._record_usage(FakeUsage(50, 10))

    # simulate a process restart: wipe memory, reload from disk
    suggest._usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "since": None}
    suggest._load_usage()

    u = suggest.get_usage()
    assert (u["calls"], u["input_tokens"], u["output_tokens"]) == (2, 150, 30)


def test_since_is_stamped_once_and_never_moves(state):
    suggest._record_usage(FakeUsage(1, 1))
    first = suggest.get_usage()["since"]
    assert first, "a cumulative total with no start date is unreadable"

    suggest._record_usage(FakeUsage(1, 1))
    suggest._usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "since": None}
    suggest._load_usage()
    assert suggest.get_usage()["since"] == first


def test_cost_carries_its_basis(state):
    suggest._record_usage(FakeUsage(1_000_000, 0))
    u = suggest.get_usage()
    # dollars are list-price arithmetic; the payload must say so, so this figure can
    # never be quietly summed with a metered one.
    assert u["cost_basis"] == "list-price estimate, not a metered bill"
    assert u["estimated_cost_usd"] == pytest.approx(settings.suggest_price_in, abs=1e-4)


def test_corrupt_state_starts_clean_and_does_not_raise(state):
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{not json at all")
    suggest._load_usage()
    assert suggest.get_usage()["calls"] == 0
    suggest._record_usage(FakeUsage(5, 5))          # and still records afterwards
    assert suggest.get_usage()["calls"] == 1


def test_unwritable_state_never_breaks_recording(state, monkeypatch):
    monkeypatch.setattr(settings, "usage_state", "/proc/nope/suggest-usage.json")
    suggest._record_usage(FakeUsage(7, 3))          # must not raise
    assert suggest.get_usage()["input_tokens"] == 7


def test_write_is_atomic_and_leaves_no_temp(state):
    suggest._record_usage(FakeUsage(1, 1))
    assert json.loads(state.read_text())["calls"] == 1
    assert not os.path.exists(f"{state}.tmp")


def test_api_usage_exposes_every_field_get_usage_returns(state):
    """The route's response_model must not silently drop a field.

    It did: cost_basis and since shipped in get_usage() while /api/usage served the old
    shape, because pydantic drops undeclared keys without a word and the other tests all
    called the function directly. Assert the CONTRACT, not the helper.
    """
    from clawatch_bridge.models import UsageResponse

    suggest._record_usage(FakeUsage(3, 4))
    returned = suggest.get_usage()
    served = UsageResponse(**returned).model_dump()

    assert set(served) == set(returned), (
        f"UsageResponse drops {set(returned) - set(served)} — declare it in models.py")
    assert served == returned


def test_state_holds_totals_only_never_content(state):
    suggest._record_usage(FakeUsage(1, 1))
    saved = json.loads(state.read_text())
    assert set(saved) == {"calls", "input_tokens", "output_tokens", "since"}
