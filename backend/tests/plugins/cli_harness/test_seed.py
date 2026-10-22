"""cli_harness.seed: what a CLI-harness sandbox starts with (the seed) and
what it keeps between legs (its CLI home, in RunStateStore). Nothing touches
the host filesystem any more."""

from __future__ import annotations

import io
import logging
import tarfile
import uuid
from typing import Any

import pytest
from cli_harness.seed import (
    STOP_GRACE_S,
    SeedDir,
    build_seed,
    load_home,
    sandbox_owner,
    save_home,
    stop_cli,
)

from oc8.config import get_settings
from oc8.runtime.run_state import RunStateStore, pack_files
from oc8.sandbox.types import SandboxHandle

_HANDLE = SandboxHandle(container_id="c1", image="i1")


def _members(tar: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as t:
        return {m.name: m for m in t.getmembers()}


def _content(tar: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as t:
        f = t.extractfile(name)
        assert f is not None
        return f.read()


def test_the_seed_owner_is_the_image_user_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "sandbox_user", "")
    assert sandbox_owner() == (1000, 1000)


def test_the_seed_owner_follows_the_configured_sandbox_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "sandbox_user", "2000:3000")
    assert sandbox_owner() == (2000, 3000)
    monkeypatch.setattr(get_settings(), "sandbox_user", "2000")
    assert sandbox_owner() == (2000, 2000)


def test_a_seed_holds_every_dir_owned_by_the_given_user_and_never_its_ancestors() -> None:
    seed = build_seed(
        [
            SeedDir("/home/node/.config/oc8", {"mcp.json": b'{"token": "t"}'}),
            SeedDir("/home/node/.claude", {"projects/-workspace/s.jsonl": b"{}\n"}),
            SeedDir("/workspace", {}),
        ],
        uid=1000,
        gid=1000,
    )
    members = _members(seed)

    assert _content(seed, "home/node/.config/oc8/mcp.json") == b'{"token": "t"}'
    assert _content(seed, "home/node/.claude/projects/-workspace/s.jsonl") == b"{}\n"
    assert members["workspace"].isdir()
    # The image owns /home/node and the mount points above a scratch dir.
    assert "home" not in members
    assert "home/node" not in members
    assert "home/node/.config" not in members
    assert all((m.uid, m.gid) == (1000, 1000) for m in members.values())
    assert members["home/node/.config/oc8/mcp.json"].mode == 0o600


def test_a_seed_refuses_a_relative_dir() -> None:
    with pytest.raises(ValueError):
        build_seed([SeedDir("home/node", {})], uid=1, gid=1)


class _Driver:
    def __init__(self, trees: dict[str, bytes]) -> None:
        self.trees = trees
        self.calls: list[tuple[str, int | None]] = []

    async def get_archive(
        self, handle: SandboxHandle, path: str, *, max_bytes: int | None = None
    ) -> bytes:
        self.calls.append((path, max_bytes))
        return self.trees[path]


async def test_a_saved_home_loads_back_without_the_excluded_files(minio_url: str) -> None:
    tenant, run = uuid.uuid4(), uuid.uuid4()
    home = pack_files(
        {
            "sessions/2026/10/22/rollout-x.jsonl": b'{"turn": 1}\n',
            "config.toml": b"secret config",
            "log/codex.log": b"noise",
        }
    )
    driver = _Driver({"/home/node/.codex": home})

    await save_home(
        driver,  # type: ignore[arg-type]
        _HANDLE,
        tenant_id=tenant,
        run_id=run,
        name="codex-home",
        path="/home/node/.codex",
        exclude=lambda rel: rel == "config.toml" or rel.startswith("log/"),
    )

    assert driver.calls == [("/home/node/.codex", get_settings().run_state_max_bytes)]
    assert await load_home(tenant_id=tenant, run_id=run, name="codex-home") == {
        "sessions/2026/10/22/rollout-x.jsonl": b'{"turn": 1}\n'
    }


async def test_nothing_saved_loads_as_none(minio_url: str) -> None:
    assert await load_home(tenant_id=uuid.uuid4(), run_id=uuid.uuid4(), name="claude-home") is None


async def test_a_failed_save_is_logged_not_raised(
    minio_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    class _Gone:
        async def get_archive(self, *a: Any, **kw: Any) -> bytes:
            raise RuntimeError("sandbox gone")

    tenant, run = uuid.uuid4(), uuid.uuid4()
    with caplog.at_level(logging.ERROR):
        saved = await save_home(
            _Gone(),  # type: ignore[arg-type]
            _HANDLE,
            tenant_id=tenant,
            run_id=run,
            name="claude-home",
            path="/home/node/.claude",
        )
    assert saved is False
    assert "could not save" in caplog.text
    assert await RunStateStore(tenant).load_files(run, "claude-home") is None


class _StoppableDriver:
    def __init__(self, *, exits: bool = True) -> None:
        self.calls: list[Any] = []
        self.exits = exits

    async def exec(self, handle: SandboxHandle, command: list[str], **kw: Any) -> Any:
        self.calls.append(("exec", command))

    async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
        self.calls.append(("wait", timeout_s))
        if not self.exits:
            raise TimeoutError("still running")
        return 143


async def test_stop_cli_signals_the_init_process_and_waits_for_it() -> None:
    """A parked leg's CLI is still running; its home (opencode's SQLite with
    its -wal file, a transcript being appended to) is read only once it has
    exited."""
    driver = _StoppableDriver()
    assert await stop_cli(driver, _HANDLE) is True  # type: ignore[arg-type]
    from oc8.sandbox.linger import STOP_COMMAND

    # Every process of the sandbox user, not PID 1: the lingering wrapper
    # traps TERM and must survive to record the exit and keep the home
    # readable.
    assert driver.calls == [("exec", STOP_COMMAND), ("wait", STOP_GRACE_S)]


async def test_stop_cli_gives_up_after_the_grace_period(caplog: pytest.LogCaptureFixture) -> None:
    driver = _StoppableDriver(exits=False)
    with caplog.at_level(logging.WARNING):
        assert await stop_cli(driver, _HANDLE) is False  # type: ignore[arg-type]
    assert "did not exit" in caplog.text
