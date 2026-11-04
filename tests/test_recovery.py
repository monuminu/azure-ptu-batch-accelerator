"""Fault and lease tests for the durable Engine, using only local adapters."""

import asyncio
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from ptu_batch.engine import Engine
from ptu_batch.memory import (
    EchoInference,
    LocalAuthenticator,
    LocalBlobStore,
    MemoryBroker,
    MemoryDocumentStore,
)
from ptu_batch.ports import Mutation, Principal
from ptu_batch.runtime import Runtime
from ptu_batch.settings import Settings


@dataclass
class Clock:
    value: int = 1_800_000_000

    def __call__(self):
        return self.value


class CountingInference(EchoInference):
    def __init__(self):
        self.calls = 0

    async def invoke(self, deployment, endpoint, body):
        self.calls += 1
        return await super().invoke(deployment, endpoint, body)


class GateInference(CountingInference):
    def __init__(self, expected_started=1):
        super().__init__()
        self.first_started = asyncio.Event()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.expected_started = expected_started
        self.active = 0
        self.max_active = 0

    async def invoke(self, deployment, endpoint, body):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.first_started.set()
        if self.calls >= self.expected_started:
            self.started.set()
        try:
            await self.release.wait()
            return await EchoInference.invoke(self, deployment, endpoint, body)
        finally:
            self.active -= 1


@pytest.fixture
async def rig(tmp_path):
    settings = Settings(
        mode="local",
        local_autorun=False,
        inference_concurrency=2,
        request_lease_seconds=60,
        control_lease_seconds=60,
        cancellation_drain_seconds=2,
        deployments={
            "original-batch": {
                "resource_endpoint": "https://example.openai.azure.com/",
                "deployment_name": "existing-ptu",
                "resource_id": "/subscriptions/local/deployments/existing-ptu",
            }
        },
    )
    runtime = Runtime(
        settings,
        MemoryDocumentStore(),
        LocalBlobStore(tmp_path),
        MemoryBroker(),
        CountingInference(),
        LocalAuthenticator(),
        clock=Clock(),
    )
    yield runtime, Engine(runtime), Principal("tenant", "alice", frozenset({"Batch.ReadWrite"}))
    await runtime.close()


def row(custom_id):
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {"model": "original-batch", "messages": [{"role": "user", "content": "hello"}]},
    }


async def submit(engine, principal, rows, *, expires_after=None):
    data = b"".join(json.dumps(item).encode() + b"\n" for item in rows)

    async def chunks():
        yield data

    file = await engine.upload_file(principal, "input.jsonl", chunks(), expires_after=expires_after)
    batch = await engine.create_batch(
        principal,
        {
            "input_file_id": file["id"],
            "endpoint": "/chat/completions",
            "completion_window": "24h",
        },
    )
    return file, batch


async def requests(runtime, batch_id):
    return [request async for request in runtime.store.scan(pk=batch_id, kind="request")]


def message(batch_id, request):
    return {
        "batch_id": batch_id,
        "request_id": request["id"],
        "generation": request["generation"],
    }


async def drain(runtime, engine):
    for _ in range(1000):
        await engine.publish_outbox()
        item = runtime.broker.pop_ready(runtime.now())
        if item is None:
            return
        if item["queue"] == runtime.settings.inference_queue:
            await engine.handle_inference(item["payload"])
        else:
            await engine.handle_control(item["payload"])
    raise AssertionError("local queue did not settle")


async def test_functions_cache_one_real_local_engine_and_imports_without_cloud_config(monkeypatch):
    from ptu_batch.functions_support import close_runtime, get_engine

    monkeypatch.setenv("PTU_BATCH_MODE", "local")
    root = Path(__file__).resolve().parents[1]
    for app_name in ("control", "inference"):
        path = root / "functions" / app_name / "function_app.py"
        spec = importlib.util.spec_from_file_location(f"recovery_{app_name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.app.get_functions()

    first, second = await asyncio.gather(get_engine(), get_engine())
    assert first is second
    assert isinstance(first, Engine)
    assert first.rt.settings.mode == "local"
    assert first._semaphore is second._semaphore
    await close_runtime()
    assert first.rt._closed


async def test_inference_concurrency_never_exceeds_configured_limit(rig):
    runtime, engine, principal = rig
    gate = GateInference(expected_started=2)
    runtime.inference = gate
    _, batch = await submit(engine, principal, [row(str(i)) for i in range(4)])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    pending = await requests(runtime, batch["id"])

    first = asyncio.create_task(engine.handle_inference(message(batch["id"], pending[0])))
    await asyncio.wait_for(gate.first_started.wait(), timeout=1)
    second = asyncio.create_task(engine.handle_inference(message(batch["id"], pending[1])))
    await asyncio.wait_for(gate.started.wait(), timeout=1)
    later = [
        asyncio.create_task(engine.handle_inference(message(batch["id"], request)))
        for request in pending[2:]
    ]
    await asyncio.sleep(0)
    assert gate.active == 2
    assert gate.calls == 2
    gate.release.set()
    await asyncio.gather(first, second, *later)
    await drain(runtime, engine)
    assert gate.max_active == 2
    assert (await engine.get_batch(principal, batch["id"]))["status"] == "completed"


async def test_crash_after_dispatch_becomes_unknown_without_replay(rig):
    runtime, engine, principal = rig
    gate = GateInference()
    runtime.inference = gate
    _, batch = await submit(engine, principal, [row("a")])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    request = (await requests(runtime, batch["id"]))[0]
    notification = message(batch["id"], request)
    task = asyncio.create_task(engine.handle_inference(notification))
    await asyncio.wait_for(gate.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await runtime.store.get(batch["id"], request["id"]))["state"] == "dispatched"

    runtime.clock.value += runtime.settings.request_lease_seconds + 1
    await engine.recover()
    await engine.handle_inference(notification)
    await drain(runtime, engine)
    state = await engine.get_batch(principal, batch["id"])
    assert state["status"] == "completed"
    assert state["request_counts"] == {"total": 1, "completed": 0, "failed": 1}
    assert gate.calls == 1


async def test_crash_after_success_blob_before_ledger_commit_emits_unknown_error(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row("a")])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    request = (await requests(runtime, batch["id"]))[0]
    original_apply = runtime.store.apply
    failed = False

    async def crash_before_outcome_commit(pk, mutations):
        nonlocal failed
        if not failed and any(
            mutation.op == "replace"
            and mutation.id == request["id"]
            and mutation.document["state"] == "succeeded"
            for mutation in mutations
        ):
            failed = True
            raise RuntimeError("crash after result blob write")
        await original_apply(pk, mutations)

    runtime.store.apply = crash_before_outcome_commit
    with pytest.raises(RuntimeError, match="crash after result blob write"):
        await engine.handle_inference(message(batch["id"], request))
    runtime.store.apply = original_apply
    assert (await runtime.store.get(batch["id"], request["id"]))["state"] == "dispatched"

    runtime.clock.value += runtime.settings.request_lease_seconds + 1
    await engine.recover()
    state = await engine.get_batch(principal, batch["id"])
    assert state["request_counts"] == {"total": 1, "completed": 0, "failed": 1}
    error_file = await engine.get_file(principal, state["error_file_id"])
    content = b"".join([part async for part in runtime.blobs.read(error_file["blob_name"])])
    envelope = json.loads(content)
    assert envelope["response"] is None
    assert envelope["error"]["code"] == "request_outcome_unknown"


async def test_cancel_during_inflight_inference_does_not_start_queued_work(rig):
    runtime, engine, principal = rig
    gate = GateInference()
    runtime.inference = gate
    file, batch = await submit(engine, principal, [row("first"), row("second")])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    first, second = await requests(runtime, batch["id"])
    task = asyncio.create_task(engine.handle_inference(message(batch["id"], first)))
    await asyncio.wait_for(gate.started.wait(), timeout=1)
    await engine.cancel_batch(principal, batch["id"])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await engine.handle_inference(message(batch["id"], second))
    runtime.clock.value += runtime.settings.cancellation_drain_seconds + 1
    await engine.recover()
    await drain(runtime, engine)
    state = await engine.get_batch(principal, batch["id"])
    assert state["status"] == "cancelled"
    assert state["request_counts"]["completed"] == 0
    assert gate.calls == 1
    assert [pin async for pin in runtime.store.scan(pk="file:" + file["id"], kind="pin")] == []


async def test_duplicate_validation_and_finalization_have_one_output(rig):
    runtime, engine, principal = rig
    file, batch = await submit(engine, principal, [row("a")])
    control = {"kind": "validate", "batch_id": batch["id"]}
    await asyncio.gather(engine.handle_control(control), engine.handle_control(control))
    pending = await requests(runtime, batch["id"])
    assert len(pending) == 1
    await engine.handle_inference(message(batch["id"], pending[0]))
    finalize = {"kind": "finalize", "batch_id": batch["id"]}
    await asyncio.gather(engine.handle_control(finalize), engine.handle_control(finalize))
    state = await engine.get_batch(principal, batch["id"])
    assert state["status"] == "completed"
    assert state["request_counts"] == {"total": 1, "completed": 1, "failed": 0}
    outputs = [
        item async for item in runtime.store.scan(kind="file", filters={"purpose": "batch_output"})
    ]
    assert len(outputs) == 1
    assert [pin async for pin in runtime.store.scan(pk="file:" + file["id"], kind="pin")] == []


async def test_crash_after_output_blob_before_file_record_does_not_leave_orphan(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row("a")])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    request = (await requests(runtime, batch["id"]))[0]
    await engine.handle_inference(message(batch["id"], request))
    original_apply = runtime.store.apply
    failed = False

    async def crash_before_output_record(pk, mutations):
        nonlocal failed
        if not failed and any(
            mutation.op == "create"
            and mutation.document.get("kind") == "file"
            and mutation.document.get("purpose") == "batch_output"
            for mutation in mutations
        ):
            failed = True
            raise RuntimeError("crash after finalized output blob")
        await original_apply(pk, mutations)

    runtime.store.apply = crash_before_output_record
    with pytest.raises(RuntimeError, match="crash after finalized output blob"):
        await engine.handle_control({"kind": "finalize", "batch_id": batch["id"]})
    runtime.store.apply = original_apply
    before = list((runtime.blobs.root / "outputs" / batch["id"]).rglob("*.jsonl"))
    assert len(before) == 1

    runtime.clock.value += runtime.settings.control_lease_seconds + 1
    await engine.recover()
    state = await engine.get_batch(principal, batch["id"])
    assert state["status"] == "completed"
    after = list((runtime.blobs.root / "outputs" / batch["id"]).rglob("*.jsonl"))
    assert len(after) == 1


async def test_outbox_republish_after_publish_before_marker_uses_same_message_identity(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row("a")])
    original_publish = runtime.broker.publish
    failed = False

    async def publish_then_crash(*args, **kwargs):
        nonlocal failed
        await original_publish(*args, **kwargs)
        if not failed:
            failed = True
            raise RuntimeError("crash before outbox marker")

    runtime.broker.publish = publish_then_crash
    with pytest.raises(RuntimeError, match="crash before outbox marker"):
        await engine.publish_outbox()
    runtime.broker.publish = original_publish
    await engine.publish_outbox()
    events = [item async for item in runtime.store.scan(pk=batch["id"], kind="outbox")]
    assert len(events) == 1
    assert events[0]["state"] == "published"
    assert len(runtime.broker.messages) == 1
    assert runtime.broker.messages[0]["message_id"] == events[0]["id"]


async def test_expired_input_remains_pinned_until_batch_finalizes(rig):
    runtime, engine, principal = rig
    file, batch = await submit(
        engine,
        principal,
        [row("a")],
        expires_after={"anchor": "created_at", "seconds": 14 * 86400},
    )
    runtime.clock.value += 14 * 86400
    await engine.recover()
    stored = await runtime.store.get("file:" + file["id"], file["id"])
    assert not stored["deleted"]
    assert await runtime.blobs.exists(file["blob_name"])
    assert len([pin async for pin in runtime.store.scan(pk=stored["pk"], kind="pin")]) == 1

    request = (await requests(runtime, batch["id"]))[0]
    await engine.handle_inference(message(batch["id"], request))
    await drain(runtime, engine)
    await engine.recover()
    stored = await runtime.store.get(stored["pk"], file["id"])
    assert stored["deleted"]
    assert not await runtime.blobs.exists(file["blob_name"])


async def test_expired_control_lease_restarts_partial_validation_without_duplicate_rows(rig):
    runtime, engine, principal = rig
    rows = [row(str(i)) for i in range(41)]
    _, batch = await submit(engine, principal, rows)
    first_chunk = sum(len(json.dumps(item).encode() + b"\n") for item in rows[:40])
    original_read = runtime.blobs.read
    failed = False

    async def read_then_crash(name, *, offset=0, length=None):
        nonlocal failed
        async for data in original_read(name, offset=offset, length=length):
            if name.startswith("inputs/") and not failed:
                failed = True
                yield data[:first_chunk]
                raise OSError("reader died after staged checkpoint")
            yield data

    runtime.blobs.read = read_then_crash
    with pytest.raises(OSError, match="reader died"):
        await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    staged = await requests(runtime, batch["id"])
    assert len(staged) == 40
    assert all(item["state"] == "staged" for item in staged)
    assert runtime.inference.calls == 0

    runtime.blobs.read = original_read
    runtime.clock.value += runtime.settings.control_lease_seconds + 1
    await engine.recover()
    all_requests = await requests(runtime, batch["id"])
    assert len(all_requests) == 41
    assert all(item["state"] == "pending" for item in all_requests)
    await drain(runtime, engine)
    state = await engine.get_batch(principal, batch["id"])
    assert state["status"] == "completed"
    assert state["request_counts"] == {"total": 41, "completed": 41, "failed": 0}


async def test_retention_reclaims_intermediate_results_after_terminal_grace(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row("a")])
    await drain(runtime, engine)
    request = (await requests(runtime, batch["id"]))[0]
    assert await runtime.blobs.exists(request["result_blob"])
    final = await engine.get_batch(principal, batch["id"])
    output = await engine.get_file(principal, final["output_file_id"])
    runtime.clock.value += runtime.settings.control_lease_seconds + 1
    await engine.recover()
    assert not await runtime.blobs.exists(request["result_blob"])
    assert await runtime.blobs.exists(output["blob_name"])


async def test_dispatch_does_not_skip_rows_with_non_snapshot_query_pagination(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row(str(i)) for i in range(85)])
    original = runtime.store.scan

    async def paginated(**kwargs):
        if (kwargs.get("filters") or {}).get("state") == "staged":
            cursor = 0
            while True:
                live = [doc async for doc in original(**kwargs)]
                page = live[cursor : cursor + 20]
                if not page:
                    return
                for doc in page:
                    yield doc
                cursor += 20
        else:
            async for doc in original(**kwargs):
                yield doc

    runtime.store.scan = paginated
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    staged = await requests(runtime, batch["id"])
    assert len(staged) == 85
    assert all(request["state"] == "pending" for request in staged)


async def test_lost_file_metadata_commit_response_does_not_delete_committed_input(rig):
    runtime, engine, principal = rig
    original = runtime.store.apply
    failed = False

    async def lost_response(pk, mutations):
        nonlocal failed
        await original(pk, mutations)
        if not failed and any(
            m.op == "create" and m.document.get("kind") == "file" for m in mutations
        ):
            failed = True
            raise RuntimeError("metadata commit response lost")

    runtime.store.apply = lost_response

    async def chunks():
        yield json.dumps(row("a")).encode()

    with pytest.raises(RuntimeError, match="metadata commit response lost"):
        await engine.upload_file(principal, "input.jsonl", chunks())
    runtime.store.apply = original
    files = [file async for file in runtime.store.scan(kind="file")]
    assert len(files) == 1
    assert await runtime.blobs.exists(files[0]["blob_name"])
    batch = await engine.create_batch(
        principal,
        {
            "input_file_id": files[0]["id"],
            "endpoint": "/chat/completions",
            "completion_window": "24h",
        },
    )
    await drain(runtime, engine)
    assert (await engine.get_batch(principal, batch["id"]))["status"] == "completed"


async def test_stale_missing_read_after_lost_file_commit_keeps_payload(rig):
    runtime, engine, principal = rig
    original_apply = runtime.store.apply
    original_get = runtime.store.get
    committed_id = None

    async def lost_response(pk, mutations):
        nonlocal committed_id
        await original_apply(pk, mutations)
        if any(m.op == "create" and m.document.get("kind") == "file" for m in mutations):
            committed_id = mutations[0].id
            raise RuntimeError("write response lost")

    async def stale_get(pk, id):
        if id == committed_id:
            return None
        return await original_get(pk, id)

    runtime.store.apply = lost_response
    runtime.store.get = stale_get

    async def chunks():
        yield json.dumps(row("a")).encode()

    with pytest.raises(RuntimeError, match="write response lost"):
        await engine.upload_file(principal, "input.jsonl", chunks())
    runtime.store.apply = original_apply
    runtime.store.get = original_get
    files = [file async for file in runtime.store.scan(kind="file")]
    assert len(files) == 1
    assert await runtime.blobs.exists(files[0]["blob_name"])


async def test_lost_batch_commit_response_with_stale_read_keeps_pin(rig):
    runtime, engine, principal = rig
    file, _ = await submit(engine, principal, [row("first")])
    original_apply = runtime.store.apply
    original_get = runtime.store.get
    committed_id = None

    async def lost_response(pk, mutations):
        nonlocal committed_id
        await original_apply(pk, mutations)
        if any(m.document and m.document.get("kind") == "batch" for m in mutations):
            committed_id = pk
            raise RuntimeError("batch commit response lost")

    async def stale_get(pk, id):
        if id == committed_id:
            return None
        return await original_get(pk, id)

    runtime.store.apply = lost_response
    runtime.store.get = stale_get
    with pytest.raises(RuntimeError, match="batch commit response lost"):
        await engine.create_batch(
            principal,
            {
                "input_file_id": file["id"],
                "endpoint": "/chat/completions",
                "completion_window": "24h",
            },
        )
    runtime.store.apply = original_apply
    runtime.store.get = original_get
    jobs = [job async for job in runtime.store.scan(kind="batch") if job["id"] == committed_id]
    assert len(jobs) == 1
    pins = [pin async for pin in runtime.store.scan(pk="file:" + file["id"], kind="pin")]
    assert any(pin["batch_id"] == committed_id for pin in pins)


async def test_stale_reservation_cannot_abort_committed_batch(rig):
    runtime, engine, principal = rig
    file, _ = await submit(engine, principal, [row("first")])
    original_apply = runtime.store.apply
    original_get = runtime.store.get
    committed_id = None
    stale_reservation = None

    async def lost_response(pk, mutations):
        nonlocal committed_id, stale_reservation
        if any(m.document and m.document.get("kind") == "batch" for m in mutations):
            stale_reservation = await original_get(pk, pk)
            await original_apply(pk, mutations)
            committed_id = pk
            raise RuntimeError("batch commit response lost")
        await original_apply(pk, mutations)

    async def stale_get(pk, id):
        if id == committed_id:
            return stale_reservation
        return await original_get(pk, id)

    runtime.store.apply = lost_response
    runtime.store.get = stale_get
    with pytest.raises(RuntimeError, match="batch commit response lost"):
        await engine.create_batch(
            principal,
            {
                "input_file_id": file["id"],
                "endpoint": "/chat/completions",
                "completion_window": "24h",
            },
        )
    runtime.store.apply = original_apply
    runtime.store.get = original_get
    committed = await runtime.store.get(committed_id, committed_id)
    assert committed["kind"] == "batch" and committed["status"] == "validating"
    pins = [pin async for pin in runtime.store.scan(pk="file:" + file["id"], kind="pin")]
    assert any(pin["batch_id"] == committed_id for pin in pins)


async def test_expired_reservation_is_aborted_before_orphan_pin_is_released(rig):
    runtime, engine, principal = rig
    file, _ = await submit(engine, principal, [row("first")])
    batch_id = "batch_orphan"
    reservation = {
        "id": batch_id,
        "pk": batch_id,
        "kind": "batch_reservation",
        "state": "pending",
        "input_file_id": file["id"],
        "created_at": runtime.now(),
        "expires_at": runtime.now() + runtime.settings.control_lease_seconds,
    }
    await runtime.store.apply(batch_id, [Mutation("create", batch_id, reservation)])
    await engine._pin(principal, file["id"], batch_id)
    assert (await engine.maintenance_status())["pending_reservations"] == 1
    runtime.clock.value += runtime.settings.control_lease_seconds + 1
    await engine.recover()
    stored = await runtime.store.get(batch_id, batch_id)
    assert stored["kind"] == "batch_reservation" and stored["state"] == "aborted"
    assert (await engine.maintenance_status())["pending_reservations"] == 0
    pins = [pin async for pin in runtime.store.scan(pk="file:" + file["id"], kind="pin")]
    assert all(pin["batch_id"] != batch_id for pin in pins)


async def test_recovery_retains_pin_when_batch_proof_is_missing(rig):
    runtime, engine, principal = rig
    file, _ = await submit(engine, principal, [row("first")])
    batch_id = "batch_uncertain"
    await engine._pin(principal, file["id"], batch_id)
    runtime.clock.value += runtime.settings.control_lease_seconds + 1
    await engine.recover()
    pins = [pin async for pin in runtime.store.scan(pk="file:" + file["id"], kind="pin")]
    assert any(pin["batch_id"] == batch_id for pin in pins)


async def test_pending_reservation_keeps_maintenance_unsafe_after_submission_lease_ends(rig):
    runtime, engine, _ = rig
    await engine.set_admission(paused=True)
    batch_id = "batch_pending_without_lease"
    reservation = {
        "id": batch_id,
        "pk": batch_id,
        "kind": "batch_reservation",
        "state": "pending",
        "input_file_id": "file_missing",
        "created_at": runtime.now(),
        "expires_at": runtime.now() + runtime.settings.control_lease_seconds,
    }
    await runtime.store.apply(batch_id, [Mutation("create", batch_id, reservation)])
    status = await engine.maintenance_status()
    assert status["active_submissions"] == 0
    assert status["pending_reservations"] == 1
    assert status["safe"] is False
    runtime.clock.value += runtime.settings.control_lease_seconds + 1
    await engine.recover()
    status = await engine.maintenance_status()
    assert status["pending_reservations"] == 0
    assert status["safe"] is True
