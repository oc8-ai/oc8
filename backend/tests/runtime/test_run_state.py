"""RunStateStore: a run's saved directories (a CLI home, for instance) kept in
object storage between legs, so leg 2 can resume where leg 1 stopped on any
worker. The tar a sandbox hands back is untrusted, so it is checked when it is
saved and again when it is loaded."""

from __future__ import annotations

import io
import tarfile
import uuid

import pytest

from oc8.config import get_settings
from oc8.runtime.run_state import RunStateRejected, RunStateStore, pack_files
from oc8.sandbox.archive import safe_extract_to_memory
from oc8.storage import s3

pytestmark = pytest.mark.asyncio


def _tar(entries: list[tuple[str, bytes | None, str]]) -> bytes:
    """entries: (name, content, kind) where kind is "file", "dir" or "symlink"."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = (content or b"").decode()
                tar.addfile(info)
            else:
                data = content or b""
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _files(tar: bytes) -> dict[str, bytes]:
    return safe_extract_to_memory(tar, max_members=1000, max_total_bytes=1 << 20)


@pytest.fixture
def ids() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


async def test_save_then_load_round_trips_the_files(
    minio_url: str, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, run = ids
    store = RunStateStore(tenant)
    tar = _tar(
        [
            ("projects", None, "dir"),
            ("projects/a.jsonl", b'{"turn": 1}\n', "file"),
            ("settings.json", b"{}", "file"),
        ]
    )

    await store.save(run, "cli-home", tar)
    loaded = await store.load(run, "cli-home")

    assert loaded is not None
    assert _files(loaded) == {"projects/a.jsonl": b'{"turn": 1}\n', "settings.json": b"{}"}
    assert await store.load_files(run, "cli-home") == {
        "projects/a.jsonl": b'{"turn": 1}\n',
        "settings.json": b"{}",
    }


async def test_missing_state_loads_as_none(
    minio_url: str, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, run = ids
    store = RunStateStore(tenant)
    assert await store.load(run, "cli-home") is None
    assert await store.load_files(run, "cli-home") is None


async def test_state_lives_under_the_tenant_prefix_so_tenant_removal_deletes_it(
    minio_url: str, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, run = ids
    store = RunStateStore(tenant)
    await store.save(run, "cli-home", _tar([("a.txt", b"a", "file")]))

    assert RunStateStore.key(tenant, run, "cli-home") == f"{tenant}/runs/{run}/state/cli-home.tar"
    assert await s3.object_exists(f"{tenant}/runs/{run}/state/cli-home.tar")

    await s3.delete_prefix(f"{tenant}/")
    assert await store.load(run, "cli-home") is None


async def test_one_tenant_cannot_load_another_tenants_state(
    minio_url: str, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, run = ids
    await RunStateStore(tenant).save(run, "cli-home", _tar([("a.txt", b"a", "file")]))
    assert await RunStateStore(uuid.uuid4()).load(run, "cli-home") is None


async def test_saving_again_replaces_the_previous_state(
    minio_url: str, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, run = ids
    store = RunStateStore(tenant)
    await store.save(run, "cli-home", _tar([("a.txt", b"leg 1", "file")]))
    await store.save(run, "cli-home", _tar([("a.txt", b"leg 2", "file")]))
    assert await store.load_files(run, "cli-home") == {"a.txt": b"leg 2"}


@pytest.mark.parametrize(
    "entries",
    [
        [("../escape.txt", b"x", "file")],
        [("/etc/passwd", b"x", "file")],
        [("link", b"/etc/shadow", "symlink")],
    ],
    ids=["dotdot", "absolute", "symlink"],
)
async def test_an_unsafe_archive_is_refused_and_nothing_is_stored(
    minio_url: str,
    ids: tuple[uuid.UUID, uuid.UUID],
    entries: list[tuple[str, bytes | None, str]],
) -> None:
    tenant, run = ids
    store = RunStateStore(tenant)
    with pytest.raises(RunStateRejected):
        await store.save(run, "cli-home", _tar(entries))
    assert not await s3.object_exists(RunStateStore.key(tenant, run, "cli-home"))


async def test_a_tampered_stored_object_is_refused_on_load(
    minio_url: str, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, run = ids
    store = RunStateStore(tenant)
    await s3.put_object(
        RunStateStore.key(tenant, run, "cli-home"),
        _tar([("../../escape", b"x", "file")]),
        "application/x-tar",
    )
    with pytest.raises(RunStateRejected):
        await store.load(run, "cli-home")


async def test_state_over_the_size_limit_is_refused(
    minio_url: str, ids: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, run = ids
    monkeypatch.setenv("OC8_RUN_STATE_MAX_BYTES", "4096")
    get_settings.cache_clear()
    try:
        with pytest.raises(RunStateRejected):
            await RunStateStore(tenant).save(run, "cli-home", _tar([("big", b"x" * 8192, "file")]))
    finally:
        monkeypatch.delenv("OC8_RUN_STATE_MAX_BYTES")
        get_settings.cache_clear()
    assert not await s3.object_exists(RunStateStore.key(tenant, run, "cli-home"))


async def test_state_with_too_many_files_is_refused(
    minio_url: str, ids: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, run = ids
    monkeypatch.setenv("OC8_RUN_STATE_MAX_FILES", "2")
    get_settings.cache_clear()
    try:
        with pytest.raises(RunStateRejected):
            await RunStateStore(tenant).save(
                run, "cli-home", _tar([(f"f{i}", b"x", "file") for i in range(3)])
            )
    finally:
        monkeypatch.delenv("OC8_RUN_STATE_MAX_FILES")
        get_settings.cache_clear()


@pytest.mark.parametrize("name", ["", "../x", "a/b", ".hidden", "with space"])
async def test_a_bad_state_name_is_refused(ids: tuple[uuid.UUID, uuid.UUID], name: str) -> None:
    tenant, run = ids
    with pytest.raises(ValueError):
        RunStateStore.key(tenant, run, name)


async def test_load_can_hand_the_files_to_the_sandbox_user(
    minio_url: str, ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant, run = ids
    store = RunStateStore(tenant)
    await store.save(run, "cli-home", _tar([("sessions/s.jsonl", b"t", "file")]))

    loaded = await store.load(run, "cli-home", uid=1000, gid=1001)
    assert loaded is not None
    with tarfile.open(fileobj=io.BytesIO(loaded), mode="r:") as tar:
        members = {mem.name: mem for mem in tar}
    # The parent directory comes first, owned by the same user, so the CLI can
    # add files next to the restored ones.
    assert list(members) == ["sessions", "sessions/s.jsonl"]
    assert members["sessions"].isdir()
    for mem in members.values():
        assert (mem.uid, mem.gid) == (1000, 1001)


async def test_pack_files_puts_the_files_under_a_prefix() -> None:
    tar = pack_files({"a/b.txt": b"b", "c.txt": b"c"}, prefix="root/.codex", uid=5, gid=6)
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as t:
        names = [mem.name for mem in t]
    assert names == [
        "root",
        "root/.codex",
        "root/.codex/a",
        "root/.codex/a/b.txt",
        "root/.codex/c.txt",
    ]
    assert _files(tar) == {"root/.codex/a/b.txt": b"b", "root/.codex/c.txt": b"c"}


async def test_pack_files_refuses_a_name_that_leaves_its_root() -> None:
    with pytest.raises(ValueError):
        pack_files({"../x": b"x"})
