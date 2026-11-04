"""Durable queue notification recovery without invalidating live backlogs."""

import asyncio
import base64
import json
import zlib

import pytest
from azure.servicebus import ServiceBusReceiveMode, ServiceBusSubQueue
from azure.servicebus.amqp import AmqpMessageBodyType
from test_engine import drain, row, submit
from test_engine import rig as rig

from ptu_batch.adapters.azure import ServiceBusBroker
from ptu_batch.ports import Mutation


async def pending_request(runtime, batch_id):
    values = [item async for item in runtime.store.scan(pk=batch_id, kind="request")]
    assert len(values) == 1
    return values[0]


async def current_infer_event(runtime, batch_id, request):
    event_id = f"outbox:{batch_id}:infer:{request['id']}:{request['generation']}"
    return await runtime.store.get(batch_id, event_id)


async def quarantine_records(runtime, state=None):
    values = [
        item
        async for item in runtime.store.scan(pk="system:deadletters", kind="dead_letter_quarantine")
    ]
    return [item for item in values if state is None or item["state"] == state]


def deadletter_ready(runtime, queue):
    index = next(i for i, item in enumerate(runtime.broker.messages) if item["queue"] == queue)
    item = runtime.broker.messages.pop(index)
    runtime.broker.dead_letters.append(item)
    return item


async def test_five_minute_recovery_preserves_live_pending_generation_and_outbox(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    await engine.publish_outbox()
    request = await pending_request(runtime, batch["id"])
    event = await current_infer_event(runtime, batch["id"], request)
    assert event["state"] == "published"
    queued_before = [
        item
        for item in runtime.broker.messages
        if item["queue"] == runtime.settings.inference_queue
    ]
    assert len(queued_before) == 1

    runtime.clock.value += 300
    await engine.recover()
    current = await pending_request(runtime, batch["id"])
    assert current["generation"] == request["generation"]
    assert await current_infer_event(runtime, batch["id"], current) is not None
    queued_after = [
        item
        for item in runtime.broker.messages
        if item["queue"] == runtime.settings.inference_queue
    ]
    assert queued_after == queued_before


async def test_large_backlog_maintenance_avoids_per_request_outbox_point_reads(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row(str(index)) for index in range(1000)])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    await engine.publish_outbox()
    original_get = runtime.store.get
    original_scan = runtime.store.scan
    outbox_gets = 0
    outbox_scans = 0

    async def count_get(pk, id):
        nonlocal outbox_gets
        if id.startswith("outbox:"):
            outbox_gets += 1
        return await original_get(pk, id)

    def count_scan(*, kind=None, pk=None, filters=None):
        nonlocal outbox_scans
        if kind == "outbox" and pk == batch["id"]:
            outbox_scans += 1
        return original_scan(kind=kind, pk=pk, filters=filters)

    runtime.store.get = count_get
    runtime.store.scan = count_scan
    runtime.clock.value += 300
    await engine.recover()
    runtime.store.get = original_get
    runtime.store.scan = original_scan
    assert outbox_gets <= 3
    assert outbox_scans == 1
    current = [item async for item in runtime.store.scan(pk=batch["id"], kind="request")]
    assert len(current) == 1000
    assert all(item["state"] == "pending" and item["generation"] == 0 for item in current)


async def test_missing_current_outbox_is_recreated_without_generation_change(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    request = await pending_request(runtime, batch["id"])
    event = await current_infer_event(runtime, batch["id"], request)
    await runtime.store.apply(batch["id"], [Mutation("delete", event["id"], etag=event["_etag"])])

    await engine.recover()
    current = await pending_request(runtime, batch["id"])
    assert current["generation"] == request["generation"]
    recreated = await current_infer_event(runtime, batch["id"], current)
    assert recreated["state"] == "published"
    assert recreated["published_at"] == runtime.now()


async def test_stale_negative_outbox_scan_does_not_duplicate_current_generation(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    await engine.publish_outbox()
    original_scan = runtime.store.scan

    async def stale_outbox_scan(*, kind=None, pk=None, filters=None):
        if kind == "outbox" and pk == batch["id"]:
            return
        async for item in original_scan(kind=kind, pk=pk, filters=filters):
            yield item

    runtime.store.scan = stale_outbox_scan
    runtime.clock.value += 300
    await engine.recover()
    runtime.store.scan = original_scan
    current = await pending_request(runtime, batch["id"])
    assert current["generation"] == 0
    events = [
        item
        async for item in runtime.store.scan(pk=batch["id"], kind="outbox")
        if item["payload"]["kind"] == "infer"
    ]
    assert len(events) == 1


async def test_pending_outbox_relay_does_not_change_request_generation(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    request = await pending_request(runtime, batch["id"])
    assert (await current_infer_event(runtime, batch["id"], request))["state"] == "pending"

    runtime.clock.value += 300
    await engine.recover()
    current = await pending_request(runtime, batch["id"])
    assert current["generation"] == request["generation"]
    event = await current_infer_event(runtime, batch["id"], current)
    assert event["state"] == "published"
    assert event["published_at"] == runtime.now()


async def test_expired_published_notification_gets_new_generation_after_fourteen_days(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    await engine.publish_outbox()
    request = await pending_request(runtime, batch["id"])
    event = await current_infer_event(runtime, batch["id"], request)
    assert event["published_at"] == runtime.now()

    runtime.clock.value += 14 * 86400 + 1
    await engine.recover()
    current = await pending_request(runtime, batch["id"])
    assert current["generation"] == request["generation"] + 1
    replacement = await current_infer_event(runtime, batch["id"], current)
    assert replacement["state"] == "published"
    assert replacement["id"] != event["id"]


async def test_expired_scan_does_not_bump_a_concurrently_refreshed_generation(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    await engine.publish_outbox()
    original_refresh = engine._refresh_expired_notification

    async def refresh_after_concurrent_reschedule(job, request, event, now):
        await engine._reschedule(job, request, now)
        await original_refresh(job, request, event, now)

    engine._refresh_expired_notification = refresh_after_concurrent_reschedule
    runtime.clock.value += 14 * 86400 + 1
    await engine.recover()
    assert (await pending_request(runtime, batch["id"]))["generation"] == 1


async def test_expired_intent_with_mismatched_batch_identity_is_not_refreshed(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    await engine.publish_outbox()
    request = await pending_request(runtime, batch["id"])
    event = await current_infer_event(runtime, batch["id"], request)
    event["payload"]["batch_id"] = "batch_other"
    await runtime.store.apply(
        batch["id"], [Mutation("replace", event["id"], event, event["_etag"])]
    )

    runtime.clock.value += 14 * 86400 + 1
    await engine.recover()
    assert (await pending_request(runtime, batch["id"]))["generation"] == 0


async def test_scheduled_notification_ttl_starts_at_not_before(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    request = await pending_request(runtime, batch["id"])
    event = await current_infer_event(runtime, batch["id"], request)
    scheduled_for = runtime.now() + 86400
    request["not_before"] = scheduled_for
    event["not_before"] = scheduled_for
    await runtime.store.apply(
        batch["id"],
        [
            Mutation("replace", request["id"], request, request["_etag"]),
            Mutation("replace", event["id"], event, event["_etag"]),
        ],
    )
    await engine.publish_outbox()

    runtime.clock.value += 14 * 86400 + 1
    await engine.recover()
    current = await pending_request(runtime, batch["id"])
    assert current["generation"] == request["generation"]
    runtime.clock.value += 86400 + 1
    await engine.recover()
    current = await pending_request(runtime, batch["id"])
    assert current["generation"] == request["generation"] + 1


async def test_inference_dead_letter_is_durably_redriven_then_completed(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    await engine.publish_outbox()
    deadletter_ready(runtime, runtime.settings.inference_queue)
    assert len(runtime.broker.dead_letters) == 1

    await engine.recover()
    assert runtime.broker.dead_letters == []
    request = await pending_request(runtime, batch["id"])
    assert request["generation"] == 1
    assert (await current_infer_event(runtime, batch["id"], request))["state"] == "published"
    await drain(runtime, engine)
    assert (await engine.get_batch(principal, batch["id"]))["status"] == "completed"
    assert len(runtime.inference.calls) == 1


async def test_dead_letter_stays_unsettled_when_requeue_transaction_fails(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    await engine.publish_outbox()
    deadletter_ready(runtime, runtime.settings.inference_queue)
    original_apply = runtime.store.apply

    async def fail_requeue(pk, mutations):
        if any(
            item.op == "create"
            and item.document.get("kind") == "outbox"
            and item.document["payload"]["kind"] == "infer"
            for item in mutations
        ):
            raise RuntimeError("ledger unavailable")
        await original_apply(pk, mutations)

    runtime.store.apply = fail_requeue
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await engine.recover()
    assert len(runtime.broker.dead_letters) == 1
    assert (await pending_request(runtime, batch["id"]))["generation"] == 0


async def test_dispatched_dead_letter_is_acknowledged_without_replay(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    await engine.publish_outbox()
    dead = deadletter_ready(runtime, runtime.settings.inference_queue)
    request = await pending_request(runtime, batch["id"])
    entered, release = asyncio.Event(), asyncio.Event()
    original_invoke = runtime.inference.invoke

    async def blocked_invoke(*args):
        entered.set()
        await release.wait()
        return await original_invoke(*args)

    runtime.inference.invoke = blocked_invoke
    task = asyncio.create_task(engine.handle_inference(dead["payload"]))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await pending_request(runtime, batch["id"]))["state"] == "dispatched"

    await engine.recover()
    assert runtime.broker.dead_letters == []
    assert (await pending_request(runtime, batch["id"]))["generation"] == request["generation"]
    runtime.clock.value += runtime.settings.request_lease_seconds + 1
    await engine.recover()
    await drain(runtime, engine)
    assert (await engine.get_batch(principal, batch["id"]))["request_counts"]["failed"] == 1
    assert runtime.inference.calls == []


async def test_unknown_batch_dead_letter_is_quarantined_before_ack(rig):
    runtime, engine, _ = rig
    item = {
        "queue": runtime.settings.inference_queue,
        "payload": {
            "kind": "infer",
            "batch_id": "batch_missing",
            "request_id": "request:1",
            "generation": 0,
        },
        "message_id": "missing-job-message",
        "not_before": 0,
    }
    runtime.broker.dead_letters.append(item)
    await engine.recover()
    assert runtime.broker.dead_letters == []
    records = await quarantine_records(runtime, "pending")
    assert len(records) == 1
    assert records[0]["source_queue"] == runtime.settings.inference_queue
    assert records[0]["message_id"] == "missing-job-message"
    assert records[0]["payload"] == item["payload"]


async def test_repeated_delivery_reuses_same_content_addressed_quarantine(rig):
    runtime, engine, _ = rig
    item = {
        "queue": runtime.settings.control_queue,
        "payload": {"kind": "validate", "batch_id": "batch_missing", "generation": 0},
        "message_id": "duplicate-deadletter",
        "not_before": 0,
    }
    runtime.broker.dead_letters.append(item)
    await engine.recover()
    runtime.broker.dead_letters.append(item.copy())
    await engine.recover()
    assert runtime.broker.dead_letters == []
    records = await quarantine_records(runtime, "pending")
    assert len(records) == 1
    assert records[0]["id"].startswith("deadletter:")


async def test_hundred_bad_front_dead_letters_do_not_starve_valid_work(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    await engine.publish_outbox()
    valid = deadletter_ready(runtime, runtime.settings.inference_queue)
    runtime.broker.dead_letters.pop()
    prefix = []
    for index in range(50):
        prefix.append(
            {
                "queue": runtime.settings.inference_queue,
                "raw_body": b'{"prompt":"private"',
                "message_id": f"invalid-{index}",
                "not_before": 0,
            }
        )
    for index in range(50):
        prefix.append(
            {
                "queue": runtime.settings.inference_queue,
                "payload": {
                    "kind": "infer",
                    "batch_id": f"batch_missing_{index}",
                    "request_id": "request:000000001",
                    "generation": 0,
                },
                "message_id": f"unresolved-{index}",
                "not_before": 0,
            }
        )
    runtime.broker.dead_letters.extend(prefix + [valid])

    await engine.recover()
    assert runtime.broker.dead_letters == [valid]
    assert len(await quarantine_records(runtime, "manual")) == 50
    assert len(await quarantine_records(runtime, "pending")) == 50
    await engine.recover()
    assert runtime.broker.dead_letters == []
    assert (await pending_request(runtime, batch["id"]))["generation"] == 1
    await drain(runtime, engine)
    assert (await engine.get_batch(principal, batch["id"]))["status"] == "completed"


async def test_quarantine_write_failure_leaves_original_dead_letter(rig):
    runtime, engine, _ = rig
    item = {
        "queue": runtime.settings.inference_queue,
        "payload": {
            "kind": "infer",
            "batch_id": "batch_missing",
            "request_id": "request:000000001",
            "generation": 0,
        },
        "message_id": "unresolved-write-failure",
        "not_before": 0,
    }
    runtime.broker.dead_letters.append(item)
    original_apply = runtime.store.apply

    async def fail_quarantine(pk, mutations):
        if any(
            mutation.op == "create" and mutation.document.get("kind") == "dead_letter_quarantine"
            for mutation in mutations
        ):
            raise RuntimeError("Cosmos unavailable")
        await original_apply(pk, mutations)

    runtime.store.apply = fail_quarantine
    with pytest.raises(RuntimeError, match="Cosmos unavailable"):
        await engine.recover()
    assert runtime.broker.dead_letters == [item]
    assert await quarantine_records(runtime) == []


async def test_pending_quarantine_recovers_when_request_becomes_proven(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    delayed_request_id = "request:999999999"
    item = {
        "queue": runtime.settings.inference_queue,
        "payload": {
            "kind": "infer",
            "batch_id": batch["id"],
            "request_id": delayed_request_id,
            "generation": 0,
        },
        "message_id": "late-request",
        "not_before": 0,
    }
    runtime.broker.dead_letters.append(item)
    await engine.recover()
    records = await quarantine_records(runtime, "pending")
    assert len(records) == 1
    await runtime.store.apply(
        batch["id"],
        [
            Mutation(
                "create",
                delayed_request_id,
                {
                    "id": delayed_request_id,
                    "pk": batch["id"],
                    "kind": "request",
                    "state": "pending",
                    "generation": 0,
                    "not_before": 0,
                    "lease_until": 0,
                    "lease_token": None,
                },
            )
        ],
    )
    runtime.clock.value += 1
    await engine.recover()
    records = await quarantine_records(runtime, "resolved")
    assert len(records) == 1
    request = await runtime.store.get(batch["id"], delayed_request_id)
    assert request["generation"] == 1
    assert (await current_infer_event(runtime, batch["id"], request))["state"] == "published"


async def test_malformed_body_is_retained_as_base64_without_logging_content(rig, caplog):
    runtime, engine, _ = rig
    secret = b'{"prompt":"private-token"'
    item = {
        "queue": runtime.settings.control_queue,
        "raw_body": secret,
        "message_id": "malformed-control",
        "not_before": 0,
    }
    runtime.broker.dead_letters.append(item)
    await engine.recover()
    assert runtime.broker.dead_letters == []
    records = await quarantine_records(runtime, "manual")
    assert len(records) == 1
    assert base64.b64decode(records[0]["raw_body_b64"]) == secret
    assert records[0]["reason"] == "invalid_json"
    assert "private-token" not in caplog.text


async def test_invalid_schema_keeps_prompt_only_in_protected_raw_body(rig):
    runtime, engine, _ = rig
    item = {
        "queue": runtime.settings.inference_queue,
        "payload": {
            "kind": "infer",
            "batch_id": "batch_missing",
            "request_id": "request:1",
            "generation": 0,
            "prompt": "private-content",
        },
        "message_id": "bad-schema",
        "not_before": 0,
    }
    runtime.broker.dead_letters.append(item)
    await engine.recover()
    records = await quarantine_records(runtime, "manual")
    assert len(records) == 1
    assert records[0]["payload"] is None
    assert records[0]["reason"] == "invalid_schema"
    assert b"private-content" in base64.b64decode(records[0]["raw_body_b64"])


async def test_pending_quarantine_reconciliation_rotates_past_old_unresolved(rig):
    runtime, engine, _ = rig
    documents = [
        {
            "id": f"deadletter:{index:03d}",
            "pk": "system:deadletters",
            "kind": "dead_letter_quarantine",
            "source_queue": runtime.settings.inference_queue,
            "message_id": f"missing-{index}",
            "payload": {
                "kind": "infer",
                "batch_id": f"batch_missing_{index}",
                "request_id": "request:000000001",
                "generation": 0,
            },
            "state": "pending",
            "created_at": 0,
            "last_checked_at": 0,
        }
        for index in range(101)
    ]
    await runtime.store.apply(
        "system:deadletters", [Mutation("create", d["id"], d) for d in documents[:100]]
    )
    await runtime.store.apply(
        "system:deadletters", [Mutation("create", documents[100]["id"], documents[100])]
    )

    await engine.recover()
    first = await quarantine_records(runtime, "pending")
    assert sum(item["last_checked_at"] == 0 for item in first) == 1
    runtime.clock.value += 1
    await engine.recover()
    second = await quarantine_records(runtime, "pending")
    assert all(item["last_checked_at"] > 0 for item in second)


async def test_control_dead_letter_creates_new_durable_outbox_before_ack(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.publish_outbox()
    old = deadletter_ready(runtime, runtime.settings.control_queue)
    await engine.recover()
    assert runtime.broker.dead_letters == []
    new_events = [
        event
        async for event in runtime.store.scan(pk=batch["id"], kind="outbox")
        if event["payload"]["kind"] == "validate" and event["id"] != old["message_id"]
    ]
    assert len(new_events) == 1
    assert new_events[0]["state"] == "published"


async def test_memory_dead_letter_handler_exception_leaves_message_for_retry(rig):
    runtime, _, _ = rig
    item = {
        "queue": runtime.settings.control_queue,
        "payload": {"kind": "validate", "batch_id": "batch_x", "generation": 0},
        "message_id": "x",
        "not_before": 0,
    }
    runtime.broker.dead_letters.append(item)

    async def fail(_):
        raise RuntimeError("ledger unavailable")

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await runtime.broker.redrive_deadletters(runtime.settings.control_queue, fail)
    assert runtime.broker.dead_letters == [item]


class DeadLetterMessage:
    def __init__(self, message_id, body, body_type=AmqpMessageBodyType.DATA):
        self.message_id = message_id
        self.body = [body] if body_type is AmqpMessageBodyType.DATA else body
        self.body_type = body_type
        self.content_type = "application/json"


class FakeDeadLetterReceiver:
    def __init__(self, messages):
        self.messages = messages
        self.completed = []
        self.receive_arguments = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def receive_messages(self, *, max_message_count, max_wait_time):
        self.receive_arguments = (max_message_count, max_wait_time)
        return self.messages[:max_message_count]

    async def complete_message(self, message):
        self.completed.append(message)


class DrainingDeadLetterReceiver(FakeDeadLetterReceiver):
    async def complete_message(self, message):
        await super().complete_message(message)
        self.messages.remove(message)


class FakeDeadLetterClient:
    def __init__(self, receiver):
        self.receiver = receiver
        self.queue_arguments = None

    def get_queue_receiver(self, **kwargs):
        self.queue_arguments = kwargs
        return self.receiver


def broker_with_deadletters(messages):
    receiver = FakeDeadLetterReceiver(messages)
    client = FakeDeadLetterClient(receiver)
    broker = ServiceBusBroker.__new__(ServiceBusBroker)
    broker._client = client
    return broker, client, receiver


async def test_azure_dead_letter_receiver_settles_only_valid_durably_handled_messages(caplog):
    valid = {"kind": "infer", "batch_id": "batch_1", "request_id": "request:1", "generation": 0}
    settled = DeadLetterMessage("valid-id", json.dumps(valid).encode())
    unresolved = DeadLetterMessage(
        "unresolved-id", json.dumps({**valid, "batch_id": "missing"}).encode()
    )
    injected = DeadLetterMessage("invalid-id", json.dumps({**valid, "prompt": "secret"}).encode())
    oversized = DeadLetterMessage("oversized-id", b"x" * 4097)
    broker, client, receiver = broker_with_deadletters([settled, unresolved, injected, oversized])
    seen = []

    async def handle(envelope):
        seen.append(envelope)
        return envelope["message_id"] != "unresolved-id"

    count = await broker.redrive_deadletters("batch-inference", handle, max_messages=500)
    assert count == 3
    assert receiver.completed == [settled, injected, oversized]
    assert [entry["message_id"] for entry in seen] == [
        "valid-id",
        "unresolved-id",
        "invalid-id",
        "oversized-id",
    ]
    assert seen[0]["payload"] == valid
    assert seen[1]["payload"] == {**valid, "batch_id": "missing"}
    assert seen[2]["payload"]["prompt"] == "secret"
    assert seen[3]["payload"] is None
    assert seen[3]["reason"] == "oversized_notification"
    assert base64.b64decode(seen[3]["raw_body_b64"]) == b"x" * 4097
    assert client.queue_arguments["queue_name"] == "batch-inference"
    assert client.queue_arguments["sub_queue"] is ServiceBusSubQueue.DEAD_LETTER
    assert client.queue_arguments["receive_mode"] is ServiceBusReceiveMode.PEEK_LOCK
    assert client.queue_arguments["prefetch_count"] == 0
    assert receiver.receive_arguments == (100, 1)
    assert "secret" not in caplog.text


async def test_azure_dead_letter_handler_exception_leaves_message_unsettled():
    payload = {"kind": "validate", "batch_id": "batch_1", "generation": 0}
    item = DeadLetterMessage("control-id", json.dumps(payload).encode())
    broker, _, receiver = broker_with_deadletters([item])

    async def fail(_):
        raise RuntimeError("ledger unavailable")

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await broker.redrive_deadletters("batch-control", fail)
    assert receiver.completed == []


async def test_value_nul_poison_prefix_is_fully_quarantined_then_valid_message_recovers(
    rig, caplog
):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    request = await pending_request(runtime, batch["id"])
    payload = {
        "kind": "infer",
        "batch_id": batch["id"],
        "request_id": request["id"],
        "generation": request["generation"],
    }
    poison = [
        DeadLetterMessage(f"value-nul-{index}", "\0" * 200_000, AmqpMessageBodyType.VALUE)
        for index in range(100)
    ]
    valid = DeadLetterMessage("valid-after-prefix", json.dumps(payload).encode())
    receiver = DrainingDeadLetterReceiver(poison + [valid])
    broker = ServiceBusBroker.__new__(ServiceBusBroker)
    broker._client = FakeDeadLetterClient(receiver)

    async def durable_handoff(envelope):
        return await engine._redrive_deadletter(envelope, control=False)

    first = await broker.redrive_deadletters(runtime.settings.inference_queue, durable_handoff)
    assert first == 100
    assert receiver.messages == [valid]
    records = await quarantine_records(runtime, "manual")
    assert len(records) == 100
    assert all(record["raw_encoding"] == "value-utf8" for record in records)
    assert all(base64.b64decode(record["raw_body_b64"]) == b"\0" * 200_000 for record in records)
    second = await broker.redrive_deadletters(runtime.settings.inference_queue, durable_handoff)
    assert second == 1
    assert receiver.messages == []
    assert (await pending_request(runtime, batch["id"]))["generation"] == 1
    assert "\0" not in caplog.text


async def test_value_bytes_and_structured_value_use_bounded_reversible_encoding():
    raw_bytes = b"\0" * 200_000
    structured = {"content": "\0" * 200_000}
    messages = [
        DeadLetterMessage("value-bytes", raw_bytes, AmqpMessageBodyType.VALUE),
        DeadLetterMessage("value-structured", structured, AmqpMessageBodyType.VALUE),
    ]
    broker, _, receiver = broker_with_deadletters(messages)
    seen = []

    async def accept(envelope):
        seen.append(envelope)
        return True

    assert await broker.redrive_deadletters("batch-inference", accept) == 2
    assert receiver.completed == messages
    assert seen[0]["raw_encoding"] == "value-bytes"
    assert base64.b64decode(seen[0]["raw_body_b64"]) == raw_bytes
    assert seen[1]["raw_encoding"] == "value-json-zlib"
    decoded = zlib.decompress(base64.b64decode(seen[1]["raw_body_b64"]))
    assert json.loads(decoded) == structured
    assert all(len(json.dumps(envelope).encode()) <= 1024 * 1024 for envelope in seen)
