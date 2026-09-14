"""PyInstaller entrypoint for PersonalDM.exe."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _prepare_frozen_env() -> None:
    if not getattr(sys, "frozen", False):
        return
    install = Path(sys.executable).resolve().parent
    os.chdir(install)
    # Ensure bundled package root is importable (onedir puts modules under _MEIPASS).
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and meipass not in sys.path:
        sys.path.insert(0, meipass)


def main() -> int:
    _prepare_frozen_env()
    from launcher import main as launcher_main

    return launcher_main()


if __name__ == "__main__":
    raise SystemExit(main())
