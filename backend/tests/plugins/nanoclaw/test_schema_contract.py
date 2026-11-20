"""The fixture schema in test_session_db.py is a copy. This is the original.

If a nanoclaw pin bump renames a column oc8 writes or reads, this test goes red
and test_session_db.py stays green -- which is exactly why it exists.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

# tests/plugins/nanoclaw/test_schema_contract.py -> repo root -> plugins/nanoclaw_runtime.
# See test_messages.py for why this is per-file rather than pytest's `pythonpath`.
# Module-top form, because this file's plugin import is module-level (collection time).
PLUGIN_ROOT = Path(__file__).resolve().parents[4] / "capas" / "nanoclaw_runtime"


def _evict() -> None:
    """Drop every cached `runtime`/`runtime.*` module from sys.modules.

    `runtime` is the package name EVERY `runtime_adapter` plugin now ships --
    nanoclaw_runtime, claude_code_runtime, codex_runtime and opencode_runtime,
    four plugins behind one top-level module name (design §2) -- and
    sys.modules is keyed by NAME, not by path. Called SYMMETRICALLY, before
    the import AND after it: before, so a sibling runtime's cached copy cannot
    answer ours; after, so nothing generic is left cached for anyone else.
    The trailing half is the load-bearing one -- `loader.import_entry_point`
    (Task 6's collision fix) only evicts modules IT ITSELF introduced, so a
    `runtime` left cached here makes a later `find_plugin`/`load_plugin` for
    one of the other three silently hand back THIS plugin's `register`.
    """
    for _stale in [n for n in sys.modules if n == "runtime" or n.startswith("runtime.")]:
        del sys.modules[_stale]


_evict()
sys.path.insert(0, str(PLUGIN_ROOT))

from runtime.session import provision_session, provisioner_image  # noqa: E402

from oc8.sandbox import get_sandbox_driver  # noqa: E402

# Symmetric with the insert above -- see `_evict`'s docstring for why the
# trailing half is the load-bearing one.
_evict()
sys.path.remove(str(PLUGIN_ROOT))

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")

_WRITTEN = {"id", "seq", "kind", "timestamp", "status", "trigger", "content"}
_READ_OUT = {"seq", "kind", "content", "timestamp"}
_READ_ACK = {"message_id", "status"}


def _columns(db: str, table: str) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


async def test_the_pinned_image_creates_the_columns_oc8_uses(tmp_path: Path) -> None:
    """Provision through the plugin's OWN function, not a hand-written docker
    command: a test that invokes the image differently would keep passing while
    the real call site is broken."""
    agent, run = uuid.uuid4(), uuid.uuid4()
    session = await provision_session(
        str(tmp_path),
        tenant_id=uuid.uuid4(),
        agent_id=agent,
        run_id=run,
        image=provisioner_image(),
        driver=get_sandbox_driver(),
    )
    tmp_path = type(tmp_path)(session)  # assertions below read the session dir

    assert _WRITTEN <= _columns(f"{tmp_path}/inbound.db", "messages_in")
    assert _READ_OUT <= _columns(f"{tmp_path}/outbound.db", "messages_out")
    assert _READ_ACK <= _columns(f"{tmp_path}/outbound.db", "processing_ack")


async def test_the_session_dbs_use_the_journal_mode_the_harness_requires(tmp_path: Path) -> None:
    """Upstream's connection.ts states inbound.db MUST be journal_mode=DELETE:
    the container's reader can otherwise sit on a stale snapshot and never see
    the host's writes. Today SQLite's default happens to match, which is luck,
    not a contract -- so assert the contract."""
    agent, run = uuid.uuid4(), uuid.uuid4()
    session = await provision_session(
        str(tmp_path),
        tenant_id=uuid.uuid4(),
        agent_id=agent,
        run_id=run,
        image=provisioner_image(),
        driver=get_sandbox_driver(),
    )

    for name in ("inbound.db", "outbound.db"):
        with sqlite3.connect(f"{session}/{name}") as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "delete", f"{name} is in {mode}"
