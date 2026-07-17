"""Compatibility launcher for the canonical backend simulation script.

Run from the repository root:
    python test/backend/tests/run_persistent_simulation.py
"""

from pathlib import Path
import runpy
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPOSITORY_ROOT / "src" / "backend"
CANONICAL_SCRIPT = BACKEND_ROOT / "tests" / "run_persistent_simulation.py"

sys.path.insert(0, str(BACKEND_ROOT))
runpy.run_path(str(CANONICAL_SCRIPT), run_name="__main__")
