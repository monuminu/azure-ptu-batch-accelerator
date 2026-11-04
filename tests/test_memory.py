import pytest

from ptu_batch.memory import LocalBlobStore, MemoryDocumentStore
from ptu_batch.ports import Conflict, Mutation


@pytest.mark.asyncio
async def test_transaction_rejects_stale_etag_without_partial_create():
    store = MemoryDocumentStore()
    await store.apply(
        "job", [Mutation("create", "job", {"id": "job", "pk": "job", "kind": "batch", "n": 0})]
    )
    old = await store.get("job", "job")
    await store.apply("job", [Mutation("replace", "job", {**old, "n": 1}, old["_etag"])])
    with pytest.raises(Conflict):
        await store.apply(
            "job",
            [
                Mutation("create", "event", {"id": "event", "pk": "job", "kind": "outbox"}),
                Mutation("replace", "job", {**old, "n": 2}, old["_etag"]),
            ],
        )
    assert await store.get("job", "event") is None
    assert (await store.get("job", "job"))["n"] == 1


@pytest.mark.asyncio
async def test_blob_is_immutable_and_supports_exact_request_ranges(tmp_path):
    store = LocalBlobStore(tmp_path)

    async def data():
        yield b"first\n"
        yield b"second\n"

    info = await store.put("inputs/file", data())
    assert info.size == 13
    assert b"".join([c async for c in store.read("inputs/file", offset=6, length=7)]) == b"second\n"
    with pytest.raises(Conflict):
        await store.put("inputs/file", data())
    assert b"".join([c async for c in store.read("inputs/file")]) == b"first\nsecond\n"


@pytest.mark.asyncio
async def test_failing_upload_does_not_publish_partial_blob(tmp_path):
    store = LocalBlobStore(tmp_path)

    async def broken():
        yield b"partial"
        raise ValueError("client disconnected")

    with pytest.raises(ValueError):
        await store.put("input", broken())
    assert not await store.exists("input")
