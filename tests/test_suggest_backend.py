"""The local (Ollama) suggest backend: routing, fallback, and the think-field retry.

The contract under test is the module's standing one — no failure mode may
surface to the client — plus the seam's own rule: local first, cloud as the
fallback, and the cloud path untouched when the backend is "anthropic".
"""
import io
import json
import urllib.error
import urllib.request

import pytest

from clawatch_bridge import suggest
from clawatch_bridge.config import settings


def _ollama_response(content):
    body = json.dumps({"message": {"role": "assistant", "content": content}}).encode()

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp(body)


@pytest.fixture
def local_backend(monkeypatch):
    monkeypatch.setattr(settings, "suggest_backend", "ollama")
    # No cloud client: a test that leaks past the local path must degrade to
    # [], never place a network call.
    monkeypatch.setattr(suggest, "_client", None)
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "suggest_enabled", False)


def test_local_backend_serves_suggestions(local_backend, monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["payload"] = json.loads(req.data.decode())
        seen["timeout"] = timeout
        return _ollama_response('["yes, go ahead", "explain first"]')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = suggest.generate_suggestions(["some terminal output"], None)
    assert out == ["yes, go ahead", "explain first"]
    assert settings.ollama_host in seen["url"]
    assert seen["payload"]["model"] == settings.ollama_model
    assert seen["payload"]["think"] is False
    # The wrist budget applies to the local call too.
    assert seen["timeout"] == settings.suggest_timeout


def test_local_failure_degrades_to_empty_without_cloud(local_backend, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise TimeoutError("cold load")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert suggest.generate_suggestions(["tail"], None) == []


def test_timeout_spawns_background_warm(local_backend, monkeypatch):
    warmed = []
    monkeypatch.setattr(suggest, "_warm_local_model", lambda: warmed.append(True))

    def fake_urlopen(req, timeout=None):
        raise TimeoutError("cold load")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    suggest.generate_suggestions(["tail"], None)
    assert warmed, "a timed-out local call must schedule a model warm"


def test_connection_refused_does_not_warm(local_backend, monkeypatch):
    """A dead host is not a cold model — warming would spin a useless thread
    per tap against a server that is not there."""
    warmed = []
    monkeypatch.setattr(suggest, "_warm_local_model", lambda: warmed.append(True))

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert suggest.generate_suggestions(["tail"], None) == []
    assert not warmed


def test_think_field_retried_once_on_http_error(local_backend, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        payload = json.loads(req.data.decode())
        calls.append(payload)
        if "think" in payload:
            raise urllib.error.HTTPError(req.full_url, 400, "bad think", None, None)
        return _ollama_response('["retry worked"]')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = suggest.generate_suggestions(["tail"], None)
    assert out == ["retry worked"]
    assert len(calls) == 2 and "think" not in calls[1]


def test_anthropic_backend_never_touches_ollama(monkeypatch):
    monkeypatch.setattr(settings, "suggest_backend", "anthropic")
    monkeypatch.setattr(suggest, "_client", None)
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "suggest_enabled", False)

    def boom(req, timeout=None):
        raise AssertionError("ollama was called on the anthropic backend")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    # No key + default backend -> the original degrade-to-[] contract.
    assert suggest.generate_suggestions(["tail"], None) == []
