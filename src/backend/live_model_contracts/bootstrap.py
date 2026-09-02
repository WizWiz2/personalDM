from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_OLLAMA = "http://127.0.0.1:11434"
DEFAULT_NARRATOR = "gemma4:e4b"
DEFAULT_CONTROL = "qwen2.5:7b"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--narrator-model", default=DEFAULT_NARRATOR)
    parser.add_argument("--control-model", default=DEFAULT_CONTROL)
    parser.add_argument("--ollama", default=DEFAULT_OLLAMA)
    args, _unknown = parser.parse_known_args()
    return args


def _canonical_model_name(value: str) -> str:
    value = value.strip()
    leaf = value.rsplit("/", 1)[-1]
    if ":" not in leaf:
        return value + ":latest"
    return value


def _model_available(requested: str, available: set[str]) -> bool:
    wanted = _canonical_model_name(requested)
    return wanted in {_canonical_model_name(value) for value in available}


def _tags_url(ollama: str) -> str:
    return ollama.rstrip("/") + "/api/tags"


def _probe_models(ollama: str, *, timeout: float = 2.0) -> set[str] | None:
    try:
        with urllib.request.urlopen(_tags_url(ollama), timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None

    result: set[str] = set()
    for item in payload.get("models") or []:
        for key in ("name", "model"):
            value = str(item.get(key) or "").strip()
            if value:
                result.add(value)
    return result


def _is_local_endpoint(ollama: str) -> bool:
    host = (urllib.parse.urlparse(ollama).hostname or "").casefold()
    return host in {"127.0.0.1", "localhost", "::1"}


def _ollama_executable() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found

    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Ollama" / "ollama.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return None


def _start_ollama(executable: str, ollama: str) -> None:
    env = os.environ.copy()
    env["OLLAMA_HOST"] = ollama
    kwargs: dict = {
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([executable, "serve"], **kwargs)


def _wait_for_ollama(ollama: str, *, timeout: float = 20.0) -> set[str] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        available = _probe_models(ollama)
        if available is not None:
            return available
        time.sleep(0.5)
    return None


def _pull_model(executable: str, ollama: str, model: str) -> bool:
    env = os.environ.copy()
    env["OLLAMA_HOST"] = ollama
    print(f"[Setup] Pulling Ollama model {model} ...", flush=True)
    result = subprocess.run([executable, "pull", model], env=env, check=False)
    return result.returncode == 0


def ensure_runtime(
    ollama: str,
    narrator_model: str,
    control_model: str,
    *,
    startup_timeout: float = 20.0,
) -> tuple[set[str], bool]:
    available = _probe_models(ollama)
    started = False
    executable = _ollama_executable()

    if available is None:
        if not _is_local_endpoint(ollama):
            raise RuntimeError(
                f"Ollama endpoint is not reachable at {_tags_url(ollama)}. "
                "Automatic startup is only supported for localhost endpoints."
            )
        if not executable:
            raise RuntimeError(
                "Ollama is not running and ollama.exe was not found. Install Ollama or add it to PATH."
            )
        print(f"[Setup] Ollama is not running. Starting {executable} serve ...", flush=True)
        _start_ollama(executable, ollama)
        started = True
        available = _wait_for_ollama(ollama, timeout=startup_timeout)
        if available is None:
            raise RuntimeError(
                f"Ollama was started but did not become ready at {_tags_url(ollama)} "
                f"within {startup_timeout:g}s."
            )

    missing = [
        model
        for model in dict.fromkeys((narrator_model, control_model))
        if not _model_available(model, available)
    ]
    if missing:
        if not _is_local_endpoint(ollama):
            raise RuntimeError(
                "Required model(s) are missing on the configured Ollama endpoint: "
                + ", ".join(missing)
            )
        if not executable:
            raise RuntimeError(
                "Required Ollama model(s) are missing and the ollama CLI was not found: "
                + ", ".join(missing)
            )
        for model in missing:
            if not _pull_model(executable, ollama, model):
                raise RuntimeError(f"Failed to pull required Ollama model: {model}")
        refreshed = _probe_models(ollama, timeout=5.0)
        if refreshed is None:
            raise RuntimeError("Ollama became unreachable after pulling required models.")
        available = refreshed
        still_missing = [model for model in missing if not _model_available(model, available)]
        if still_missing:
            raise RuntimeError(
                "Ollama pull completed but required model(s) are still unavailable: "
                + ", ".join(still_missing)
            )

    return available, started


def main() -> int:
    args = _parse_args()
    try:
        available, started = ensure_runtime(
            args.ollama,
            args.narrator_model,
            args.control_model,
        )
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    state = "started by launcher" if started else "already running"
    print(f"[Setup] Ollama ready ({state}).", flush=True)
    print(
        "[Setup] Required models ready: "
        + ", ".join(dict.fromkeys((args.narrator_model, args.control_model))),
        flush=True,
    )
    if not available:
        print("[Setup] Ollama returned an empty model list.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
