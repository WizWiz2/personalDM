from __future__ import annotations

import pytest

from live_model_contracts import bootstrap


def test_model_availability_requires_the_requested_tag() -> None:
    available = {"qwen2.5:14b", "gemma4:e4b", "plain:latest"}

    assert bootstrap._model_available("gemma4:e4b", available)
    assert bootstrap._model_available("plain", available)
    assert not bootstrap._model_available("qwen2.5:7b", available)


def test_local_bootstrap_starts_ollama_and_pulls_missing_model(monkeypatch) -> None:
    probes = iter([None, {"gemma4:e4b", "qwen2.5:7b"}])
    started: list[tuple[str, str]] = []
    pulled: list[tuple[str, str, str]] = []

    monkeypatch.setattr(bootstrap, "_probe_models", lambda *_args, **_kwargs: next(probes))
    monkeypatch.setattr(bootstrap, "_ollama_executable", lambda: "ollama.exe")
    monkeypatch.setattr(
        bootstrap,
        "_start_ollama",
        lambda executable, endpoint: started.append((executable, endpoint)),
    )
    monkeypatch.setattr(
        bootstrap,
        "_wait_for_ollama",
        lambda *_args, **_kwargs: {"gemma4:e4b"},
    )
    monkeypatch.setattr(
        bootstrap,
        "_pull_model",
        lambda executable, endpoint, model: pulled.append((executable, endpoint, model)) or True,
    )

    available, was_started = bootstrap.ensure_runtime(
        "http://127.0.0.1:11434",
        "gemma4:e4b",
        "qwen2.5:7b",
    )

    assert was_started is True
    assert started == [("ollama.exe", "http://127.0.0.1:11434")]
    assert pulled == [("ollama.exe", "http://127.0.0.1:11434", "qwen2.5:7b")]
    assert bootstrap._model_available("qwen2.5:7b", available)


def test_remote_endpoint_is_never_started_implicitly(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "_probe_models", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bootstrap, "_ollama_executable", lambda: "ollama")

    with pytest.raises(RuntimeError, match="Automatic startup is only supported"):
        bootstrap.ensure_runtime(
            "http://models.example.test:11434",
            "gemma4:e4b",
            "qwen2.5:7b",
        )
