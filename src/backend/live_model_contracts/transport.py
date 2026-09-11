from __future__ import annotations

import urllib.parse
import urllib.request
from typing import BinaryIO

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def open_endpoint(url: str, *, timeout: float):
    """Open a live-contract endpoint without leaking desktop proxy settings into local Ollama.

    Windows developer machines may have HTTP(S)_PROXY configured for corporate traffic. urllib would
    otherwise route http://127.0.0.1:11434 through that proxy, making an available local Ollama look
    unreachable. Only loopback endpoints bypass proxies; non-local URLs keep urllib's normal handler.
    """
    host = (urllib.parse.urlparse(url).hostname or "").casefold()
    if host in _LOCAL_HOSTS:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(url, timeout=timeout)
    return urllib.request.urlopen(url, timeout=timeout)


__all__ = ["open_endpoint"]
