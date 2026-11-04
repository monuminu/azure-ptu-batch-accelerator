import asyncio
import json
from dataclasses import dataclass

import pytest

from ptu_batch.engine import Engine
from ptu_batch.errors import BatchError
from ptu_batch.memory import (
    EchoInference,
    LocalAuthenticator,
    LocalBlobStore,
    MemoryBroker,
    MemoryDocumentStore,
)
from ptu_batch.ports import InferenceResult, Principal
from ptu_batch.runtime import Runtime
from ptu_batch.settings import Settings


@dataclass
class Clock:
    value: int = 1800000000

    def __call__(self):
        return self.value


class RecordingInference(EchoInference):
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    async def invoke(self, deployment, endpoint, body):
        self.calls.append((deployment, endpoint, body))
        if self.results:
            value = self.results.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return await super().invoke(deployment, endpoint, body)


@pytest.fixture
async def rig(tmp_path):
    settings = Settings(
        mode="local",
        local_autorun=False,
        deployments={
            "original-batch": {
                "resource_endpoint": "https://example.openai.azure.com/",
                "deployment_name": "existing-ptu",
                "resource_id": "/subscriptions/local/deployments/existing-ptu",
            }
        },
    )
    inference = RecordingInference()
    runtime = Runtime(
        settings,
        MemoryDocumentStore(),
        LocalBlobStore(tmp_path),
        MemoryBroker(),
        inference,
        LocalAuthenticator(),
        clock=Clock(),
    )
    yield runtime, Engine(runtime), Principal("tenant", "alice", frozenset({"Batch.ReadWrite"}))
    await runtime.close()


def row(custom="a", url="/v1/chat/completions", **extra):
    return {
        "custom_id": custom,
        "method": "POST",
        "url": url,
        "body": {
            "model": "original-batch",
            "messages": [{"role": "user", "content": "hello"}],
            **extra,
        },
    }


async def upload(engine, principal, rows):
    data = b"".join(json.dumps(r).encode() + b"\n" for r in rows)

    async def chunks():
        for i in range(0, len(data), 17):
            yield data[i : i + 17]

    return await engine.upload_file(principal, "input.jsonl", chunks())


async def submit(engine, principal, rows):
    file = await upload(engine, principal, rows)
    batch = await engine.create_batch(
        principal,
        {"input_file_id": file["id"], "endpoint": "/chat/completions", "completion_window": "24h"},
    )
    return file, batch


async def drain(runtime, engine):
    for _ in range(1000):
        await engine.publish_outbox()
        message = runtime.broker.pop_ready(runtime.now())
        if not message:
            return
        if message["queue"] == runtime.settings.inference_queue:
            await engine.handle_inference(message["payload"])
        else:
            await engine.handle_control(message["payload"])
    raise AssertionError("pipeline did not settle")


async def test_whole_batch_round_trip_preserves_correlation_and_upstream_body(rig):
    rt, e, p = rig
    _, batch = await submit(e, p, [row("one"), row("two")])
    await drain(rt, e)
    status = await e.get_batch(p, batch["id"])
    assert status["status"] == "completed"
    assert status["request_counts"] == {"total": 2, "completed": 2, "failed": 0}
    result = await e.get_file(p, status["output_file_id"])
    data = b"".join([c async for c in rt.blobs.read(result["blob_name"])])
    output = [json.loads(line) for line in data.splitlines()]
    assert {r["custom_id"] for r in output} == {"one", "two"}
    assert output[0]["response"]["body"]["object"] == "chat.completion"
    assert rt.inference.calls[0][0]["deployment_name"] == "existing-ptu"
    assert rt.inference.calls[0][2]["model"] == "original-batch"


async def test_responses_rows_override_advisory_create_endpoint(rig):
    rt, e, p = rig
    r = row(url="/v1/responses")
    r["body"] = {
        "model": "original-batch",
        "input": "hello",
        "text": {"format": {"type": "json_object"}},
    }
    _, b = await submit(e, p, [r])
    await drain(rt, e)
    assert (await e.get_batch(p, b["id"]))["status"] == "completed"
    assert rt.inference.calls[0][1] == "/responses"
    assert rt.inference.calls[0][2]["text"] == {"format": {"type": "json_object"}}


@pytest.mark.parametrize(
    "bad",
    [
        [row("duplicate"), row("duplicate")],
        [row(), row("b", url="https://evil.example/execute")],
        [row(), row("b", stream=True)],
        [row(), row("b", background=True)],
        [row(), row("b", previous_response_id="another-resource")],
        [row(), row("b", tools=[{"type": "web_search"}])],
        [row(), {**row("b"), "method": "GET"}],
    ],
)
async def test_entire_file_validates_before_any_inference(rig, bad):
    rt, e, p = rig
    _, b = await submit(e, p, bad)
    await drain(rt, e)
    state = await e.get_batch(p, b["id"])
    assert state["status"] == "failed"
    assert state["errors"]["data"]
    assert rt.inference.calls == []


async def test_duplicate_delivery_publishes_and_counts_only_one_outcome(rig):
    rt, e, p = rig
    _, b = await submit(e, p, [row()])
    await e.handle_control({"kind": "validate", "batch_id": b["id"]})
    request = next([x async for x in rt.store.scan(pk=b["id"], kind="request")].__iter__())
    message = {"batch_id": b["id"], "request_id": request["id"], "generation": 0}
    await asyncio.gather(e.handle_inference(message), e.handle_inference(message))
    await drain(rt, e)
    assert len(rt.inference.calls) == 1
    assert (await e.get_batch(p, b["id"]))["request_counts"] == {
        "total": 1,
        "completed": 1,
        "failed": 0,
    }


async def test_429_is_durably_rescheduled_with_new_notification_identity(rig):
    rt, e, p = rig
    rt.inference.results = [
        InferenceResult(429, {"error": {"message": "busy"}}, headers={"retry-after-ms": "5000"})
    ]
    _, b = await submit(e, p, [row()])
    await drain(rt, e)
    state = await e.get_batch(p, b["id"])
    assert state["status"] == "in_progress"
    assert state["request_counts"]["failed"] == 0
    assert len(rt.inference.calls) == 1
    rt.clock.value += 6
    await drain(rt, e)
    assert len(rt.inference.calls) == 2
    assert (await e.get_batch(p, b["id"]))["status"] == "completed"


async def test_unknown_network_outcome_is_not_automatically_replayed(rig):
    rt, e, p = rig
    rt.inference.results = [TimeoutError("response lost")]
    _, b = await submit(e, p, [row()])
    await drain(rt, e)
    state = await e.get_batch(p, b["id"])
    assert state["status"] == "completed"
    assert state["request_counts"]["failed"] == 1
    assert len(rt.inference.calls) == 1
    file = await e.get_file(p, state["error_file_id"])
    errors = b"".join([c async for c in rt.blobs.read(file["blob_name"])])
    assert json.loads(errors)["error"]["code"] == "request_outcome_unknown"
    await e.recover()
    await drain(rt, e)
    assert len(rt.inference.calls) == 1


async def test_cancel_before_validation_does_not_execute_and_releases_input(rig):
    rt, e, p = rig
    f, b = await submit(e, p, [row()])
    with pytest.raises(BatchError) as err:
        await e.delete_file(p, f["id"])
    assert err.value.status == 409
    await e.cancel_batch(p, b["id"])
    await drain(rt, e)
    assert (await e.get_batch(p, b["id"]))["status"] == "cancelled"
    assert rt.inference.calls == []
    assert (await e.delete_file(p, f["id"]))["deleted"] is True


async def test_job_continues_after_24_hours_and_cannot_be_read_by_another_owner(rig):
    rt, e, p = rig
    _, b = await submit(e, p, [row()])
    rt.clock.value += 90000
    await drain(rt, e)
    assert (await e.get_batch(p, b["id"]))["status"] == "completed"
    with pytest.raises(BatchError) as err:
        await e.get_batch(Principal("tenant", "bob"), b["id"])
    assert err.value.status == 404


async def test_failed_validation_after_staged_rows_never_dispatches(rig):
    rt, e, p = rig
    rt.settings.max_requests = 1
    _, b = await submit(e, p, [row(), row("two")])
    await drain(rt, e)
    assert (await e.get_batch(p, b["id"]))["status"] == "failed"
    assert not rt.inference.calls
