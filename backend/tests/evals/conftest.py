"""The eval runner (`backend/evals/oc8_evals`) is not an installed package:
it runs with `PYTHONPATH=evals` in the backend container. Its smoke tests
here import it the same way."""

from __future__ import annotations

import sys
from pathlib import Path

_EVALS = str(Path(__file__).resolve().parents[2] / "evals")
if _EVALS not in sys.path:
    sys.path.insert(0, _EVALS)
