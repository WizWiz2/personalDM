from __future__ import annotations

import urllib.error
from unittest.mock import Mock

import pytest

from live_model_contracts import transport


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FailThenSucceedOpener:
    def __init__(self):
        self.calls = 0

    def open(self, url, *, timeout):
        del url, timeout
        self.calls += 1
        if self.calls == 1:
            raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        return _Response()


def test_loopback_transport_restarts_ollama_then_retries(monkeypatch) -> None:
    opener = _FailThenSucceedOpener()
    restarts: list[tuple[str, float]] = []
    monkeypatch.setattr(transport, "_local_opener", lambda: opener)
    monkeypatch.setattr(
        transport,
        "_restart_local_ollama",
        lambda url, *, timeout: restarts.append((url, timeout)) or True,
    )

    response = transport.open_endpoint("http://127.0.0.1:11434/api/tags", timeout=5)

    assert isinstance(response, _Response)
    assert opener.calls == 2
    assert restarts == [("http://127.0.0.1:11434/api/tags", 5)]


def test_loopback_transport_preserves_original_error_when_restart_fails(monkeypatch) -> None:
    class _AlwaysFails:
        def open(self, url, *, timeout):
            del url, timeout
            raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    monkeypatch.setattr(transport, "_local_opener", lambda: _AlwaysFails())
    monkeypatch.setattr(transport, "_restart_local_ollama", lambda url, *, timeout: False)

    try:
        transport.open_endpoint("http://localhost:11434/api/tags", timeout=5)
    except urllib.error.URLError as exc:
        assert isinstance(exc.reason, ConnectionRefusedError)
    else:
        raise AssertionError("connection failure must remain visible when Ollama cannot restart")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/api/tags",
        "http://[::1]:11434/api/tags",
        "http://localhost:11434/api/tags",
    ],
)
def test_loopback_probe_does_not_use_environment_proxy(monkeypatch, url):
    opener = Mock()
    build = Mock(return_value=opener)
    monkeypatch.setattr(transport.urllib.request, "build_opener", build)
    monkeypatch.setattr(
        transport.urllib.request,
        "urlopen",
        Mock(side_effect=AssertionError("environment proxy used")),
    )
    transport.open_endpoint(url, timeout=2)
    assert build.call_args.args[0].proxies == {}
    opener.open.assert_called_once_with(url, timeout=2)


def test_remote_probe_preserves_configured_transport(monkeypatch):
    request = Mock()
    monkeypatch.setattr(transport.urllib.request, "urlopen", request)
    transport.open_endpoint("https://model.example/api/tags", timeout=3)
    request.assert_called_once_with("https://model.example/api/tags", timeout=3)
