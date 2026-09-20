"""File-backed state for the mock MCP servers.

A mock runs as a stdio subprocess that the runtime launches per session -- for
the isolated runtime that is once per TOOL CALL -- and the in-process engine
(worker container) and the isolated control plane (backend container) do not
share memory. They do share the OC8_RUNTIME_SESSION_ROOT bind mount, so the
state is one JSON file there, written under an exclusive lock.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

ENV_VAR = "OC8_EVAL_STATE_FILE"


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def from_env(cls) -> Store:
        raw = os.environ.get(ENV_VAR)
        if not raw:
            raise RuntimeError(f"{ENV_VAR} is not set; the eval runner sets it per task")
        return cls(Path(raw))

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text() or "{}")
        return data if isinstance(data, dict) else {}

    def update(self, fn: Callable[[dict[str, Any]], T]) -> T:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.read()
            result = fn(state)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
            os.replace(tmp, self.path)
            return result

    def next_id(self, state: dict[str, Any], prefix: str) -> str:
        counters = state.setdefault("_counters", {})
        counters[prefix] = int(counters.get(prefix, 0)) + 1
        return f"{prefix}-{counters[prefix]}"
