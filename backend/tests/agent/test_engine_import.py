"""R9: engine.py takes oc8.connections.direct_access lazily, so importing the
engine first in a fresh process hits no import cycle."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_the_engine_imports_on_its_own() -> None:
    """R9: engine.py takes oc8.connections.direct_access lazily -- a fresh
    process importing the engine first must not hit an import cycle."""
    backend = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(backend / "src")}
    done = subprocess.run(
        [sys.executable, "-c", "import oc8.agent.engine"],
        cwd=backend,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
