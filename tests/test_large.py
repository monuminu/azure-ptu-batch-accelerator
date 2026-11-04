"""Large local acceptance cases. No Azure resource or inference endpoint is used."""

import hashlib
import json

import httpx
import pytest
from test_engine import row

from ptu_batch.api import create_app
from ptu_batch.engine import Engine
from ptu_batch.memory import MemoryDocumentStore

pytest_plugins = ("test_engine",)


MIB = 1024 * 1024
FILE_LIMIT = 200 * MIB


class CountingStore(MemoryDocumentStore):
    def __init__(self):
        super().__init__()
        self.transaction_sizes = []

    async def apply(self, pk, mutations):
        if pk.startswith("batch_"):
            self.transaction_sizes.append(len(mutations))
        return await super().apply(pk, mutations)


async def multipart_bytes(size, *, boundary, yielded):
    header = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\n'
        f"batch\r\n--{boundary}\r\nContent-Disposition: form-data; "
        f'name="expires_after[anchor]"\r\n\r\ncreated_at\r\n--{boundary}\r\n'
        f'Content-Disposition: form-data; name="expires_after[seconds]"\r\n\r\n'
        f"1209600\r\n--{boundary}\r\nContent-Disposition: form-data; "
        f'name="file"; filename="large.jsonl"\r\nContent-Type: application/jsonl\r\n\r\n'
    ).encode()
    yield header
    chunk = b"x" * MIB
    full, remainder = divmod(size, MIB)
    for _ in range(full):
        yielded.append(len(chunk))
        yield chunk
    if remainder:
        yielded.append(remainder)
        yield chunk[:remainder]
    yield f"\r\n--{boundary}--\r\n".encode()


@pytest.mark.large
async def test_real_asgi_stream_accepts_exact_200_mib_and_rejects_next_byte(rig):
    rt, _, _ = rig
    rt.settings.max_file_bytes = FILE_LIMIT
    app = create_app(rt)
    headers = {
        "Authorization": "Bearer local-dev-token",
        "Content-Type": "multipart/form-data; boundary=large-acceptance",
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local.test", timeout=180
        ) as client:
            yielded = []
            accepted = await client.post(
                "/openai/v1/files",
                headers=headers,
                content=multipart_bytes(FILE_LIMIT, boundary="large-acceptance", yielded=yielded),
            )
            assert accepted.status_code == 200, accepted.text
            uploaded = accepted.json()
            assert uploaded["bytes"] == FILE_LIMIT
            assert uploaded["status"] == "processed"
            assert uploaded["expires_at"] == rt.now() + 1209600
            assert accepted.headers["x-request-id"].startswith("req_")
            assert max(yielded) == MIB and sum(yielded) == FILE_LIMIT
            stored = await rt.store.get("file:" + uploaded["id"], uploaded["id"])
            expected_digest = hashlib.sha256()
            for _ in range(200):
                expected_digest.update(b"x" * MIB)
            assert stored["sha256"] == expected_digest.hexdigest()
            assert (rt.blobs.root / stored["blob_name"]).stat().st_size == FILE_LIMIT

            yielded = []
            rejected = await client.post(
                "/openai/v1/files",
                headers=headers,
                content=multipart_bytes(
                    FILE_LIMIT + 1, boundary="large-acceptance", yielded=yielded
                ),
            )
            assert rejected.status_code == 413, rejected.text
            assert rejected.json()["error"]["code"] == "file_too_large"
            assert sum(yielded) == FILE_LIMIT + 1
            assert (
                len([item for item in rt.store._items.values() if item.get("kind") == "file"]) == 1
            )
            assert not list(rt.blobs.root.glob("inputs/.upload-*"))

            details = await client.get(
                "/openai/v1/files/" + uploaded["id"],
                headers={"Authorization": "Bearer local-dev-token"},
            )
            assert details.status_code == 200
            assert details.headers["x-request-id"].startswith("req_")
            assert details.json()["bytes"] == FILE_LIMIT
            rt.clock.value += 1209600
            expired = await client.get(
                "/openai/v1/files/" + uploaded["id"],
                headers={"Authorization": "Bearer local-dev-token"},
            )
            assert expired.status_code == 404
            assert expired.headers["x-request-id"].startswith("req_")


async def jsonl_rows(count, *, invalid_at=None):
    for number in range(1, count + 1):
        current = row(f"id-{number:06d}")
        if number == invalid_at:
            current["method"] = "GET"
        yield (json.dumps(current, separators=(",", ":")) + "\n").encode()


@pytest.mark.large
async def test_100000_lines_are_admitted_in_bounded_transactions_without_inference(
    rig, monkeypatch
):
    rt, _, principal = rig
    store = CountingStore()
    rt.store = store
    engine = Engine(rt)
    dispatches = []

    async def defer_dispatch(batch_id):
        dispatches.append(batch_id)

    monkeypatch.setattr(engine, "_dispatch_staged", defer_dispatch)
    uploaded = await engine.upload_file(principal, "100000.jsonl", jsonl_rows(100000))
    batch = await engine.create_batch(
        principal,
        {
            "input_file_id": uploaded["id"],
            "endpoint": "/chat/completions",
            "completion_window": "24h",
        },
    )
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    result = await engine.get_batch(principal, batch["id"])
    assert result["status"] == "in_progress"
    assert result["request_counts"]["total"] == 100000
    assert dispatches == [batch["id"]]
    assert not rt.inference.calls
    assert sum(doc.get("kind") == "request" for doc in store._items.values()) == 100000
    assert max(store.transaction_sizes) <= 40
    assert sum(size == 40 for size in store.transaction_sizes) == 2500


@pytest.mark.large
async def test_100001st_line_is_rejected_before_dispatch_or_inference(rig):
    rt, _, principal = rig
    rt.store = CountingStore()
    engine = Engine(rt)
    uploaded = await engine.upload_file(principal, "100001.jsonl", jsonl_rows(100001))
    batch = await engine.create_batch(
        principal,
        {
            "input_file_id": uploaded["id"],
            "endpoint": "/chat/completions",
            "completion_window": "24h",
        },
    )
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    result = await engine.get_batch(principal, batch["id"])
    assert result["status"] == "failed"
    assert result["errors"]["data"][0]["code"] == "too_many_requests"
    assert result["errors"]["data"][0]["line"] == 100001
    assert result["request_counts"]["total"] == 0
    assert not rt.inference.calls
    assert not any(
        doc.get("kind") == "outbox" and doc["payload"]["kind"] == "infer"
        for doc in rt.store._items.values()
    )
    assert max(rt.store.transaction_sizes) <= 40


async def test_invalid_first_line_never_dispatches_or_invokes_inference(rig):
    rt, _, principal = rig
    engine = Engine(rt)
    uploaded = await engine.upload_file(principal, "invalid.jsonl", jsonl_rows(2, invalid_at=1))
    batch = await engine.create_batch(
        principal,
        {
            "input_file_id": uploaded["id"],
            "endpoint": "/chat/completions",
            "completion_window": "24h",
        },
    )
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    assert (await engine.get_batch(principal, batch["id"]))["status"] == "failed"
    assert not rt.inference.calls
    assert not any(doc.get("kind") == "request" for doc in rt.store._items.values())


async def test_dispatch_transactions_fit_under_the_100_mutation_limit(rig):
    rt, _, principal = rig
    rt.store = CountingStore()
    engine = Engine(rt)
    uploaded = await engine.upload_file(principal, "41.jsonl", jsonl_rows(41))
    batch = await engine.create_batch(
        principal,
        {
            "input_file_id": uploaded["id"],
            "endpoint": "/chat/completions",
            "completion_window": "24h",
        },
    )
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    assert 81 in rt.store.transaction_sizes
    assert max(rt.store.transaction_sizes) <= 100
    assert (
        sum(
            doc.get("kind") == "outbox" and doc["payload"]["kind"] == "infer"
            for doc in rt.store._items.values()
        )
        == 41
    )
    assert not rt.inference.calls
