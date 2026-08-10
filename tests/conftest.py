"""Suite-wide guard: no test may touch the REAL usage-state file.

The meter writes to settings.usage_state on every recorded call, and settings is
built from the developer's own environment — so a test that legitimately exercises
the suggest path is one missing monkeypatch away from writing test artifacts into
the production meter. That is not hypothetical: the day local-run metering landed
(2026-08-10), two pre-existing backend tests served fake local suggestions and
stamped served_calls=2 / latency=0ms into the live state file, which the live
bridge then loaded as fact. Isolation is therefore AUTOUSE, not per-test courtesy.
"""
import pytest

from clawatch_bridge import suggest
from clawatch_bridge.config import settings


@pytest.fixture(autouse=True)
def _isolated_usage_meter(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "usage_state", str(tmp_path / "suggest-usage.json"))
    monkeypatch.setattr(suggest, "_usage",
                        {"calls": 0, "input_tokens": 0, "output_tokens": 0, "since": None,
                         "by_model": {}})
