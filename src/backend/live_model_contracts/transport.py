from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _local_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _ollama_executable() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    if sys.platform == "win32":
        local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
        for candidate in (
            local_app_data / "Programs" / "Ollama" / "ollama.exe",
            local_app_data / "Ollama" / "ollama.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return None


def _restart_local_ollama(base_url: str, *, timeout: float) -> bool:
    """Start a disappeared localhost Ollama once and wait for its tags endpoint.

    Each live-contract repetition runs in an isolated Python process. A long suite must therefore be
    able to recover when Ollama itself crashes between children instead of turning every remaining
    contract into the same WinError 10061 infrastructure failure.
    """
    executable = _ollama_executable()
    if not executable:
        return False

    parsed = urllib.parse.urlparse(base_url)
    origin = f"{parsed.scheme or 'http'}://{parsed.netloc}"
    env = os.environ.copy()
    env["OLLAMA_HOST"] = origin
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

    try:
        subprocess.Popen([executable, "serve"], **kwargs)
    except OSError:
        return False

    probe = origin.rstrip("/") + "/api/tags"
    deadline = time.monotonic() + max(5.0, min(timeout, 20.0))
    opener = _local_opener()
    while time.monotonic() < deadline:
        try:
            with opener.open(probe, timeout=min(2.0, timeout)):
                return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    return False


def open_endpoint(url: str, *, timeout: float):
    """Open a live-contract endpoint, bypassing proxies and recovering local Ollama once.

    Windows developer machines may have HTTP(S)_PROXY configured for corporate traffic. urllib would
    otherwise route http://127.0.0.1:11434 through that proxy. Long isolated live suites can also
    outlive the Ollama server process itself. Loopback requests therefore bypass proxies and, on one
    failed connection, attempt a local Ollama restart before the request is retried. Non-local URLs
    retain urllib's normal behavior and are never auto-started.
    """
    host = (urllib.parse.urlparse(url).hostname or "").casefold()
    if host not in _LOCAL_HOSTS:
        return urllib.request.urlopen(url, timeout=timeout)

    opener = _local_opener()
    try:
        return opener.open(url, timeout=timeout)
    except (OSError, urllib.error.URLError):
        if not _restart_local_ollama(url, timeout=timeout):
            raise
        return opener.open(url, timeout=timeout)


__all__ = ["open_endpoint"]
