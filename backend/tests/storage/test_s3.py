import pytest

from oc8.storage import s3

pytestmark = pytest.mark.asyncio


async def test_put_get_delete_round_trip(minio_url: str) -> None:
    # minio_url fixture: see conftest.py addition below -- yields a running
    # MinIO container's endpoint and ensures oc8.storage.s3's settings/client
    # cache points at it for the duration of the test.
    key = "test-tenant/chat_message/abc-hello.txt"
    await s3.put_object(key, b"hello world", "text/plain")
    assert await s3.get_object(key) == b"hello world"
    await s3.delete_object(key)
    with pytest.raises(s3.ObjectNotFound):
        await s3.get_object(key)


async def test_object_exists(minio_url: str) -> None:
    key = "test-tenant/chat_message/def-exists.txt"
    assert await s3.object_exists(key) is False
    await s3.put_object(key, b"hi", "text/plain")
    assert await s3.object_exists(key) is True
    await s3.delete_object(key)
    assert await s3.object_exists(key) is False


async def test_delete_prefix_removes_every_object_under_it_and_nothing_else(
    minio_url: str,
) -> None:
    import asyncio
    import uuid

    # More than one listing page (1 000 keys) and one delete_objects batch.
    prefix = f"{uuid.uuid4()}/"
    sibling = f"{prefix[:-1]}x/kept.txt"
    keys = [f"{prefix}chat_message/{i:04d}.txt" for i in range(1005)]
    await asyncio.gather(*(s3.put_object(k, b"x", "text/plain") for k in keys))
    await s3.put_object(sibling, b"x", "text/plain")

    assert await s3.delete_prefix(prefix) == 1005

    for key in (keys[0], keys[999], keys[-1]):
        assert await s3.object_exists(key) is False
    assert await s3.object_exists(sibling) is True
    assert await s3.delete_prefix(prefix) == 0
    await s3.delete_object(sibling)


async def test_delete_prefix_refuses_an_empty_prefix(minio_url: str) -> None:
    with pytest.raises(ValueError):
        await s3.delete_prefix("")
