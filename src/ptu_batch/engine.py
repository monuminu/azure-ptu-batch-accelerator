"""Durable Files/Batch business logic, independent of HTTP and Functions.

External inference cannot participate in a database transaction. Dispatch is
recorded before the network boundary; expired dispatched attempts are errors,
never silently replayed. Notifications are hints; the ledger is authoritative.
"""

import asyncio
import copy
import hashlib
import heapq
import json
import logging
import uuid
from collections.abc import AsyncIterable
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from .errors import BatchError, DeploymentPreflightError
from .ports import BlobInfo, Conflict, Mutation
from .settings import DeploymentAccount

log = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled", "expired"}
FILE_FIELDS = (
    "id",
    "object",
    "bytes",
    "created_at",
    "expires_at",
    "filename",
    "purpose",
    "status",
    "status_details",
)
BATCH_FIELDS = (
    "id",
    "object",
    "endpoint",
    "input_file_id",
    "completion_window",
    "created_at",
    "status",
    "cancelled_at",
    "cancelling_at",
    "completed_at",
    "error_file_id",
    "errors",
    "expired_at",
    "expires_at",
    "failed_at",
    "finalizing_at",
    "in_progress_at",
    "metadata",
    "output_file_id",
    "request_counts",
)


def file_view(file):
    return {k: file[k] for k in FILE_FIELDS if k in file}


def batch_view(batch):
    return {k: batch.get(k) for k in BATCH_FIELDS}


def canonical_endpoint(value):
    if value in ("/chat/completions", "/v1/chat/completions"):
        return "/chat/completions"
    if value in ("/responses", "/v1/responses"):
        return "/responses"
    raise BatchError(400, "Unsupported request endpoint", "unsupported_endpoint", "url")


def expiration(value, now):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"anchor", "seconds"}:
        raise BatchError(400, "Expiration requires anchor and seconds", param="expires_after")
    seconds = value.get("seconds")
    if (
        value.get("anchor") != "created_at"
        or type(seconds) is not int
        or not 1209600 <= seconds <= 2592000
    ):
        raise BatchError(
            400, "Expiration must be 14–30 days anchored at created_at", param="expires_after"
        )
    return now + seconds


def json_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        + b"\n"
    )


async def bytes_stream(data):
    yield data


class Engine:
    def __init__(self, runtime):
        self.rt = runtime
        self._semaphore = asyncio.Semaphore(runtime.settings.inference_concurrency)

    def _event(self, batch_id, kind, identity="job", generation=0, not_before=0):
        # Service Bus deduplicates message IDs across the entire queue, not pk.
        event_id = f"outbox:{batch_id}:{kind}:{identity}:{generation}"
        payload = {"kind": kind, "batch_id": batch_id, "generation": generation}
        if kind == "infer":
            payload["request_id"] = identity
        return {
            "id": event_id,
            "pk": batch_id,
            "kind": "outbox",
            "state": "pending",
            "queue": self.rt.settings.inference_queue
            if kind == "infer"
            else self.rt.settings.control_queue,
            "payload": payload,
            "not_before": not_before,
            "created_at": self.rt.now(),
        }

    async def _job(self, batch_id):
        job = await self.rt.store.get(batch_id, batch_id)
        if job is None or job.get("kind") != "batch":
            raise BatchError(404, "Batch not found", "not_found")
        return job

    async def _change_job(self, batch_id, change):
        for _ in range(50):
            old = await self._job(batch_id)
            new = copy.deepcopy(old)
            extra = change(new)
            if extra is None:
                return old
            try:
                await self.rt.store.apply(
                    batch_id, [Mutation("replace", batch_id, new, old["_etag"]), *extra]
                )
                return await self._job(batch_id)
            except Conflict:
                await asyncio.sleep(0)
        raise RuntimeError("Job contention exceeded retry budget")

    async def upload_file(
        self,
        principal,
        filename,
        chunks: AsyncIterable[bytes],
        *,
        purpose="batch",
        expires_after=None,
    ):
        if purpose != "batch":
            raise BatchError(400, "Only purpose=batch is supported", param="purpose")
        now = self.rt.now()
        expires = expiration(expires_after, now)
        id = "file-" + uuid.uuid4().hex
        blob_name = f"inputs/{id}.jsonl"

        async def limited():
            size = 0
            async for chunk in chunks:
                size += len(chunk)
                if size > self.rt.settings.max_file_bytes:
                    raise BatchError(
                        413,
                        "Input file exceeds the configured file limit",
                        "file_too_large",
                        "file",
                    )
                yield chunk
            if size == 0:
                raise BatchError(400, "Input file is empty", "empty_file", "file")

        info = await self.rt.blobs.put(blob_name, limited())
        document = {
            "id": id,
            "pk": "file:" + id,
            "kind": "file",
            "object": "file",
            "owner": principal.owner,
            "filename": filename,
            "bytes": info.size,
            "sha256": info.sha256,
            "purpose": purpose,
            "status": "processed",
            "created_at": now,
            "expires_at": expires,
            "blob_name": blob_name,
            "deleted": False,
        }
        try:
            await self.rt.store.apply(document["pk"], [Mutation("create", id, document)])
        except BaseException:
            # A lost write response also loses its Cosmos session token. A
            # follow-up missing read may be stale, so retain the payload.
            log.warning("File metadata commit uncertain; preserving payload", extra={"file_id": id})
            raise
        return document

    async def get_file(self, principal, id):
        doc = await self.rt.store.get("file:" + id, id)
        if (
            not doc
            or doc.get("deleted")
            or doc.get("owner") != principal.owner
            or (doc.get("expires_at") is not None and doc["expires_at"] <= self.rt.now())
        ):
            raise BatchError(404, "File not found", "not_found")
        return doc

    async def delete_file(self, principal, id):
        for _ in range(30):
            doc = await self.get_file(principal, id)
            pins = [p async for p in self.rt.store.scan(pk=doc["pk"], kind="pin")]
            if pins:
                raise BatchError(409, "File is referenced by an active batch", "file_in_use")
            doc["deleted"] = True
            try:
                await self.rt.store.apply(doc["pk"], [Mutation("replace", id, doc, doc["_etag"])])
                await self.rt.blobs.delete(doc["blob_name"])
                return {"id": id, "object": "file", "deleted": True}
            except Conflict:
                continue
        raise RuntimeError("File deletion contention exceeded retry budget")

    async def _pin(self, principal, file_id, batch_id):
        for _ in range(30):
            doc = await self.get_file(principal, file_id)
            if doc["purpose"] != "batch":
                raise BatchError(400, "Batch input must have purpose=batch", param="input_file_id")
            pin = {
                "id": "pin:" + batch_id,
                "pk": doc["pk"],
                "kind": "pin",
                "batch_id": batch_id,
                "created_at": self.rt.now(),
            }
            try:
                await self.rt.store.apply(
                    doc["pk"],
                    [
                        Mutation("replace", file_id, doc, doc["_etag"]),
                        Mutation("create", pin["id"], pin),
                    ],
                )
                return doc
            except Conflict:
                continue
        raise RuntimeError("Input pin contention exceeded retry budget")

    async def _unpin(self, job):
        pk = "file:" + job["input_file_id"]
        pin = await self.rt.store.get(pk, "pin:" + job["id"])
        if pin:
            with suppress(Conflict):
                await self.rt.store.apply(pk, [Mutation("delete", pin["id"], etag=pin["_etag"])])

    async def _abort_reservation(self, batch_id):
        """Return positive abort proof, fenced against a committed Batch."""
        for _ in range(30):
            current = await self.rt.store.get(batch_id, batch_id)
            if not current or current.get("kind") != "batch_reservation":
                return None
            if current.get("state") == "aborted":
                return current
            if current.get("state") != "pending":
                return None
            aborted = {**current, "state": "aborted", "aborted_at": self.rt.now()}
            try:
                await self.rt.store.apply(
                    batch_id,
                    [Mutation("replace", batch_id, aborted, current["_etag"])],
                )
                return aborted
            except Conflict:
                await asyncio.sleep(0)
        return None

    async def set_admission(self, *, paused):
        pk = "system:admission"
        for _ in range(50):
            old = await self.rt.store.get(pk, "admission")
            new = {
                "id": "admission",
                "pk": pk,
                "kind": "admission",
                "paused": bool(paused),
                "updated_at": self.rt.now(),
            }
            mutation = (
                Mutation("replace", "admission", new, old["_etag"])
                if old
                else Mutation("create", "admission", new)
            )
            try:
                await self.rt.store.apply(pk, [mutation])
                return
            except Conflict:
                await asyncio.sleep(0)
        raise RuntimeError("Admission gate contention exceeded retry budget")

    async def _admit_submission(self):
        pk = "system:admission"
        for _ in range(50):
            gate = await self.rt.store.get(pk, "admission")
            if gate is None:
                gate = {
                    "id": "admission",
                    "pk": pk,
                    "kind": "admission",
                    "paused": self.rt.settings.mode == "azure",
                }
                mutation = Mutation("create", "admission", gate)
            else:
                mutation = Mutation("replace", "admission", gate, gate["_etag"])
            if gate["paused"]:
                raise BatchError(
                    503,
                    "Batch submission is paused; an operator must resume admission",
                    "admission_paused",
                    error_type="service_unavailable",
                )
            lease = {
                "id": "submission:" + uuid.uuid4().hex,
                "pk": pk,
                "kind": "submission",
                "expires_at": self.rt.now() + self.rt.settings.control_lease_seconds,
            }
            try:
                await self.rt.store.apply(pk, [mutation, Mutation("create", lease["id"], lease)])
                return lease
            except Conflict:
                await asyncio.sleep(0)
        raise RuntimeError("Admission contention exceeded retry budget")

    async def maintenance_status(self):
        gate = await self.rt.store.get("system:admission", "admission")
        paused = gate["paused"] if gate else self.rt.settings.mode == "azure"
        submissions = sum([1 async for _ in self.rt.store.scan(kind="submission")])
        reservations = sum(
            [
                1
                async for reservation in self.rt.store.scan(kind="batch_reservation")
                if reservation.get("state") == "pending"
            ]
        )
        active = sum(
            [1 async for b in self.rt.store.scan(kind="batch") if b["status"] not in TERMINAL]
        )
        pending = sum(
            [1 async for _ in self.rt.store.scan(kind="outbox", filters={"state": "pending"})]
        )
        return {
            "paused": paused,
            "active_submissions": submissions,
            "pending_reservations": reservations,
            "active_batches": active,
            "pending_outbox": pending,
            "safe": paused and not (submissions or reservations or active or pending),
        }

    async def create_batch(self, principal, body):
        lease = await self._admit_submission()
        try:
            async with asyncio.timeout(self.rt.settings.submission_timeout_seconds):
                return await self._create_batch(principal, body)
        finally:
            current = await self.rt.store.get(lease["pk"], lease["id"])
            if current:
                with suppress(Conflict):
                    await self.rt.store.apply(
                        lease["pk"], [Mutation("delete", lease["id"], etag=current["_etag"])]
                    )

    async def _create_batch(self, principal, body):
        allowed = {
            "input_file_id",
            "endpoint",
            "completion_window",
            "metadata",
            "output_expires_after",
        }
        unknown = set(body) - allowed
        if unknown:
            raise BatchError(400, "Unsupported batch parameter", param=sorted(unknown)[0])
        if body.get("completion_window") != "24h":
            raise BatchError(400, "completion_window must be 24h", param="completion_window")
        canonical_endpoint(body.get("endpoint"))
        if not isinstance(body.get("input_file_id"), str):
            raise BatchError(400, "input_file_id is required", param="input_file_id")
        metadata = body.get("metadata")
        if metadata is not None and (
            not isinstance(metadata, dict)
            or len(metadata) > 16
            or any(
                not isinstance(k, str) or len(k) > 64 or not isinstance(v, str) or len(v) > 512
                for k, v in metadata.items()
            )
        ):
            raise BatchError(
                400,
                "metadata supports up to 16 string values (keys 64, values 512 characters)",
                param="metadata",
            )
        expiration(body.get("output_expires_after"), self.rt.now())
        id = "batch_" + uuid.uuid4().hex
        now = self.rt.now()
        reservation = {
            "id": id,
            "pk": id,
            "kind": "batch_reservation",
            "state": "pending",
            "input_file_id": body["input_file_id"],
            "created_at": now,
            "expires_at": now
            + self.rt.settings.submission_timeout_seconds
            + self.rt.settings.control_lease_seconds,
        }
        # A durable reservation precedes the cross-partition input pin.
        # An ambiguous create cannot leave a pin because pinning has not begun.
        await self.rt.store.apply(id, [Mutation("create", id, reservation)])
        try:
            reservation = await self.rt.store.get(id, id)
            if (
                not reservation
                or reservation.get("kind") != "batch_reservation"
                or reservation.get("state") != "pending"
            ):
                raise RuntimeError("Batch reservation is no longer pending")
            file = await self._pin(principal, body["input_file_id"], id)
            now = self.rt.now()
            job = {k: None for k in BATCH_FIELDS}
            job.update(
                {
                    "id": id,
                    "pk": id,
                    "kind": "batch",
                    "object": "batch",
                    "owner": principal.owner,
                    "principal": {
                        "tenant_id": principal.tenant_id,
                        "subject_id": principal.subject_id,
                    },
                    "input_file_id": file["id"],
                    "input_blob": file["blob_name"],
                    "input_sha256": file["sha256"],
                    "endpoint": body["endpoint"],
                    "completion_window": "24h",
                    "metadata": metadata,
                    "created_at": now,
                    "expires_at": now + 86400,
                    "status": "validating",
                    "request_counts": {"total": 0, "completed": 0, "failed": 0},
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    "deployment_snapshot": {
                        k: v.model_dump() for k, v in self.rt.settings.deployments.items()
                    },
                    "deployment_account_snapshot": (
                        self.rt.settings.deployment_account.model_dump()
                        if self.rt.settings.deployment_account is not None
                        else None
                    ),
                    "validation_complete": False,
                    "dispatch_complete": False,
                    "output_expires_after": body.get("output_expires_after"),
                    "control_token": None,
                    "control_lease_until": 0,
                }
            )
            event = self._event(id, "validate")
            await self.rt.store.apply(
                id,
                [
                    Mutation("replace", id, job, reservation["_etag"]),
                    Mutation("create", event["id"], event),
                ],
            )
        except BaseException:
            # Only a successful CAS to an aborted marker proves no Batch can
            # have committed. Missing reads are never sufficient to unpin.
            with suppress(Exception):
                aborted = await self._abort_reservation(id)
                if aborted:
                    await self._unpin(aborted)
            raise
        return batch_view(job)

    async def get_batch(self, principal, id):
        job = await self._job(id)
        if job["owner"] != principal.owner:
            raise BatchError(404, "Batch not found", "not_found")
        return batch_view(job)

    async def list_objects(
        self, principal, kind, *, limit=20, after=None, order="desc", purpose=None
    ):
        maximum = 10000 if kind == "file" else 100
        if not 1 <= limit <= maximum or order not in ("asc", "desc"):
            raise BatchError(400, "Invalid pagination parameters")
        # Partition-independent lists are indexed metadata queries, never payload reads.
        values = []
        async for doc in self.rt.store.scan(kind=kind, filters={"owner": principal.owner}):
            if kind == "file" and (
                doc.get("deleted")
                or (doc.get("expires_at") is not None and doc["expires_at"] <= self.rt.now())
                or (purpose and doc["purpose"] != purpose)
            ):
                continue
            values.append(doc)
        values.sort(key=lambda d: (d["created_at"], d["id"]), reverse=order == "desc")
        if after:
            positions = [i for i, d in enumerate(values) if d["id"] == after]
            if not positions:
                raise BatchError(400, "Pagination cursor not found", param="after")
            values = values[positions[0] + 1 :]
        more = len(values) > limit
        page = values[:limit]
        view = file_view if kind == "file" else batch_view
        return {
            "object": "list",
            "data": [view(d) for d in page],
            "first_id": page[0]["id"] if page else None,
            "last_id": page[-1]["id"] if page else None,
            "has_more": more,
        }

    async def cancel_batch(self, principal, id):
        await self.get_batch(principal, id)

        def cancel(job):
            if job["status"] in TERMINAL or job["status"] == "cancelling":
                return None
            job["status"] = "cancelling"
            job["cancelling_at"] = self.rt.now()
            event = self._event(id, "finalize", generation=self.rt.now())
            return [Mutation("create", event["id"], event)]

        return batch_view(await self._change_job(id, cancel))

    async def publish_outbox(self, documents=None):
        if documents is None:
            source = self.rt.store.scan(kind="outbox", filters={"state": "pending"})
        else:

            async def selected():
                for doc in documents:
                    if doc.get("kind") == "outbox" and doc.get("state") == "pending":
                        current = await self.rt.store.get(doc["pk"], doc["id"])
                        if current and current.get("state") == "pending":
                            yield current

            source = selected()
        async for event in source:
            await self.rt.broker.publish(
                event["queue"],
                event["payload"],
                message_id=event["id"],
                not_before=event.get("not_before") or None,
            )
            event["state"] = "published"
            event["published_at"] = self.rt.now()
            with suppress(Conflict):
                await self.rt.store.apply(
                    event["pk"], [Mutation("replace", event["id"], event, event["_etag"])]
                )

    async def _latest_inference_intents(self, batch_id):
        # A paged scan keeps one metadata record per request, regardless of
        # how many older notification generations remain in the ledger.
        latest = {}
        async for event in self.rt.store.scan(pk=batch_id, kind="outbox"):
            payload = event.get("payload")
            if (
                not isinstance(payload, dict)
                or payload.get("kind") != "infer"
                or payload.get("batch_id") != batch_id
            ):
                continue
            request_id = payload.get("request_id")
            generation = payload.get("generation")
            if not isinstance(request_id, str) or type(generation) is not int or generation < 0:
                continue
            prior = latest.get(request_id)
            if prior is None or generation > prior["payload"]["generation"]:
                latest[request_id] = event
        return latest

    async def _refresh_expired_notification(self, job, request, event, now):
        # An old scan result is not proof that the request or notification is
        # still current. Only the rare TTL path needs fresh point reads.
        current = await self.rt.store.get(job["id"], request["id"])
        if (
            not current
            or current["state"] != "pending"
            or current["generation"] != request["generation"]
        ):
            return
        fresh_event = await self.rt.store.get(job["id"], event["id"])
        fresh_payload = fresh_event.get("payload") if fresh_event else None
        if (
            not fresh_event
            or fresh_event.get("state") != "published"
            or not isinstance(fresh_payload, dict)
            or fresh_payload.get("kind") != "infer"
            or fresh_payload.get("batch_id") != job["id"]
            or fresh_payload.get("request_id") != current["id"]
            or fresh_payload.get("generation") != current["generation"]
        ):
            return
        published_at = fresh_event.get("published_at", fresh_event.get("created_at", now))
        delivered_from = max(fresh_event.get("not_before") or 0, published_at)
        if now <= delivered_from + 14 * 86400:
            return
        replacement = copy.deepcopy(current)
        when = max(now, replacement.get("not_before", 0))
        replacement.update(
            generation=replacement["generation"] + 1,
            not_before=when,
            lease_until=0,
            lease_token=None,
        )
        successor = self._event(
            job["id"], "infer", replacement["id"], replacement["generation"], when
        )
        with suppress(Conflict):
            await self.rt.store.apply(
                job["id"],
                [
                    Mutation("replace", current["id"], replacement, current["_etag"]),
                    Mutation("create", successor["id"], successor),
                ],
            )

    async def _ensure_pending_notification(self, job, request, intent, now):
        if intent is None or intent["payload"]["generation"] != request["generation"]:
            # Fence a concurrent request transition while reconstructing its missing notification.
            # A stale scan may miss an existing event; create conflict is safe.
            event = self._event(
                job["id"],
                "infer",
                request["id"],
                request["generation"],
                request.get("not_before", 0),
            )
            with suppress(Conflict):
                await self.rt.store.apply(
                    job["id"],
                    [
                        Mutation("replace", request["id"], request, request["_etag"]),
                        Mutation("create", event["id"], event),
                    ],
                )
        elif intent.get("state") == "published":
            published_at = intent.get("published_at", intent.get("created_at", now))
            delivered_from = max(intent.get("not_before") or 0, published_at)
            if now > delivered_from + 14 * 86400:
                await self._refresh_expired_notification(job, request, intent, now)

    @staticmethod
    def _valid_dead_letter_payload(message, *, control):
        if not isinstance(message, dict):
            return False
        kind = message.get("kind")
        expected = {"validate", "finalize"} if control else {"infer"}
        required = {"kind", "batch_id", "generation"}
        if not control:
            required.add("request_id")
        if (
            kind not in expected
            or set(message) != required
            or not isinstance(message.get("batch_id"), str)
            or not message["batch_id"]
            or type(message.get("generation")) is not int
            or message["generation"] < 0
            or (
                not control
                and (not isinstance(message.get("request_id"), str) or not message["request_id"])
            )
        ):
            return False
        return True

    async def _quarantine_dead_letter(self, envelope, *, state, reason):
        source_queue = envelope["source_queue"]
        message_id = envelope["message_id"]
        raw_body_b64 = envelope["raw_body_b64"]
        raw_encoding = envelope["raw_encoding"]
        identity = json.dumps(
            [source_queue, message_id, raw_encoding, raw_body_b64],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        id = "deadletter:" + hashlib.sha256(identity).hexdigest()
        now = self.rt.now()
        document = {
            "id": id,
            "pk": "system:deadletters",
            "kind": "dead_letter_quarantine",
            "source_queue": source_queue,
            "message_id": message_id,
            "payload": copy.deepcopy(envelope["payload"]) if state == "pending" else None,
            "raw_body_b64": raw_body_b64 if state == "manual" else None,
            "raw_encoding": raw_encoding if state == "manual" else None,
            "reason": reason,
            "state": state,
            "created_at": now,
            "last_checked_at": now,
        }
        try:
            await self.rt.store.apply("system:deadletters", [Mutation("create", id, document)])
        except Conflict:
            # The content-addressed record is positive proof of a prior durable handoff.
            pass

    async def _redrive_deadletter(self, envelope, *, control):
        expected_queue = (
            self.rt.settings.control_queue if control else self.rt.settings.inference_queue
        )
        if (
            not isinstance(envelope, dict)
            or envelope.get("source_queue") != expected_queue
            or not isinstance(envelope.get("message_id"), str)
            or not isinstance(envelope.get("raw_body_b64"), str)
            or not isinstance(envelope.get("raw_encoding"), str)
            or len(json.dumps(envelope, separators=(",", ":")).encode()) > 1024 * 1024
        ):
            return False
        message = envelope.get("payload")
        reason = envelope.get("reason")
        if reason is not None or not self._valid_dead_letter_payload(message, control=control):
            await self._quarantine_dead_letter(
                envelope, state="manual", reason=reason or "invalid_schema"
            )
            return True
        if await self._resolve_dead_letter_payload(message, control=control):
            return True
        await self._quarantine_dead_letter(envelope, state="pending", reason="unresolved")
        return True

    async def _resolve_dead_letter_payload(self, message, *, control):
        if not self._valid_dead_letter_payload(message, control=control):
            return False
        kind = message["kind"]
        batch_id = message["batch_id"]
        job = await self.rt.store.get(batch_id, batch_id)
        if not job or job.get("kind") != "batch":
            return False
        if job["status"] in TERMINAL:
            return True
        if control:
            for offset in range(100):
                current = await self.rt.store.get(batch_id, batch_id)
                if not current or current.get("kind") != "batch":
                    return False
                if current["status"] in TERMINAL:
                    return True
                event = self._event(batch_id, kind, generation=self.rt.now() + offset)
                try:
                    await self.rt.store.apply(
                        batch_id,
                        [
                            Mutation("replace", batch_id, current, current["_etag"]),
                            Mutation("create", event["id"], event),
                        ],
                    )
                    return True
                except Conflict:
                    continue
            raise RuntimeError("Control dead letter redrive contention exceeded retry budget")
        request_id = message.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return False
        for _ in range(50):
            current = await self.rt.store.get(batch_id, request_id)
            if not current or current.get("kind") != "request":
                return False
            if current["state"] in ("succeeded", "failed", "dispatched"):
                return True
            if current["state"] != "pending":
                return False
            if current["generation"] > message["generation"]:
                return True
            if current["generation"] < message["generation"]:
                return False
            replacement = copy.deepcopy(current)
            when = max(self.rt.now(), replacement.get("not_before", 0))
            replacement.update(
                generation=replacement["generation"] + 1,
                not_before=when,
                lease_until=0,
                lease_token=None,
            )
            event = self._event(batch_id, "infer", request_id, replacement["generation"], when)
            try:
                await self.rt.store.apply(
                    batch_id,
                    [
                        Mutation("replace", request_id, replacement, current["_etag"]),
                        Mutation("create", event["id"], event),
                    ],
                )
                return True
            except Conflict:
                continue
        raise RuntimeError("Inference dead letter redrive contention exceeded retry budget")

    async def _reconcile_pending_quarantine(self):
        # A bounded max-heap selects the 100 least recently checked records
        # without retaining an unbounded quarantine partition in memory.
        selected = []
        sequence = 0
        async for record in self.rt.store.scan(
            pk="system:deadletters", kind="dead_letter_quarantine", filters={"state": "pending"}
        ):
            sequence += 1
            checked = record.get("last_checked_at") or 0
            created = record.get("created_at") or 0
            entry = (-checked, -created, -sequence, record)
            if len(selected) < 100:
                heapq.heappush(selected, entry)
            elif entry > selected[0]:
                heapq.heapreplace(selected, entry)
        for _, _, _, record in sorted(selected, key=lambda item: (-item[0], -item[1], -item[2])):
            queue = record.get("source_queue")
            if queue == self.rt.settings.control_queue:
                control = True
            elif queue == self.rt.settings.inference_queue:
                control = False
            else:
                control = None
            resolved = (
                await self._resolve_dead_letter_payload(record.get("payload"), control=control)
                if control is not None
                else False
            )
            updated = copy.deepcopy(record)
            updated["state"] = "resolved" if resolved else "pending"
            updated["last_checked_at"] = self.rt.now()
            with suppress(Conflict):
                await self.rt.store.apply(
                    "system:deadletters",
                    [Mutation("replace", updated["id"], updated, record["_etag"])],
                )

    async def handle_control(self, message):
        id = message.get("batch_id")
        if not isinstance(id, str):
            raise ValueError("Control message requires batch_id")
        if message.get("kind") == "validate":
            await self._validate(id)
        elif message.get("kind") == "finalize":
            await self._finalize(id)
        else:
            raise ValueError("Unknown control message kind")

    async def _claim_control(self, id, operation):
        token = uuid.uuid4().hex

        def claim(job):
            if job["status"] in TERMINAL or job.get("control_lease_until", 0) > self.rt.now():
                return None
            job.update(
                control_token=token,
                control_operation=operation,
                control_lease_until=self.rt.now() + self.rt.settings.control_lease_seconds,
            )
            return []

        job = await self._change_job(id, claim)
        return (job, token) if job.get("control_token") == token else (None, None)

    async def _control_heartbeat(self, id, token):
        def beat(job):
            if job.get("control_token") != token:
                raise Conflict("Control lease was fenced")
            job["control_lease_until"] = self.rt.now() + self.rt.settings.control_lease_seconds
            return []

        return await self._change_job(id, beat)

    async def _lines(self, blob):
        buffer = bytearray()
        offset = 0
        number = 0
        async for chunk in self.rt.blobs.read(blob):
            buffer.extend(chunk)
            while (end := buffer.find(b"\n")) >= 0:
                data = bytes(buffer[: end + 1])
                del buffer[: end + 1]
                number += 1
                yield number, offset, len(data), data
                offset += len(data)
        if buffer:
            yield number + 1, offset, len(buffer), bytes(buffer)

    def _validate_row(self, data, job, seen, expected):
        try:

            def nonfinite(value):
                raise ValueError("Non-finite JSON numbers are invalid")

            row = json.loads(data.decode("utf-8"), parse_constant=nonfinite)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise BatchError(
                400, "Invalid UTF-8 JSONL request", "invalid_json", param="file"
            ) from exc
        if not isinstance(row, dict) or row.get("method") != "POST":
            raise BatchError(400, "Each request must be an object with method POST", param="method")
        custom = row.get("custom_id")
        if not isinstance(custom, str) or not custom or len(custom) > 512 or custom in seen:
            raise BatchError(
                400,
                "custom_id must be unique, nonempty, and at most 512 characters",
                param="custom_id",
            )
        seen.add(custom)
        endpoint = canonical_endpoint(row.get("url"))
        body = row.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("model"), str):
            raise BatchError(400, "Request body requires a deployment name", param="body.model")
        alias = body["model"]
        account_snapshot = job.get("deployment_account_snapshot")
        if account_snapshot is not None:
            try:
                target = (
                    DeploymentAccount.model_validate(account_snapshot).resolve(alias).model_dump()
                )
            except ValueError as exc:
                raise BatchError(
                    400, "Invalid deployment name or account configuration", param="body.model"
                ) from exc
        else:
            target = job["deployment_snapshot"].get(alias)
            if not target:
                raise BatchError(
                    400, "Deployment alias is not configured", "model_not_found", "body.model"
                )
        if (
            target.get("allowed_subjects")
            and job["principal"]["subject_id"] not in target["allowed_subjects"]
        ):
            raise BatchError(
                403, "Caller cannot use this deployment", "permission_denied", "body.model"
            )
        if endpoint not in target["allowed_apis"]:
            raise BatchError(400, "Deployment does not allow this API", param="url")
        if account_snapshot is not None:
            target["allowed_apis"] = [endpoint]
        if expected and expected != (endpoint, alias):
            raise BatchError(
                400, "All lines must use one endpoint and deployment", param="body.model"
            )
        if body.get("stream") or body.get("background"):
            raise BatchError(
                400, "Streaming and background Responses are unsupported", param="body"
            )
        if body.get("store") is True or any(
            k in body for k in ("previous_response_id", "conversation", "assistant_id", "thread_id")
        ):
            raise BatchError(400, "Stateful requests are unsupported", param="body")
        tools = body.get("tools", [])
        if not isinstance(tools, list) or any(
            not isinstance(t, dict) or t.get("type") != "function" for t in tools
        ):
            raise BatchError(400, "Hosted tools are unsupported", param="body.tools")

        def has_file_id(value):
            if isinstance(value, dict):
                return "file_id" in value or any(has_file_id(v) for v in value.values())
            return isinstance(value, list) and any(has_file_id(v) for v in value)

        if has_file_id(body):
            raise BatchError(400, "Requests cannot reference external file IDs", param="body")
        return row, endpoint, alias, target

    async def _validate(self, id):
        initial = await self._job(id)
        if initial["status"] == "cancelling":
            await self._finalize(id)
            return
        if initial["status"] != "validating":
            if initial["status"] == "in_progress" and not initial.get("dispatch_complete"):
                await self._dispatch_staged(id)
            return
        job, token = await self._claim_control(id, "validate")
        if not job:
            return
        number = 0
        pending = []
        seen = set()
        expected = None
        target = None
        digest = hashlib.sha256()
        try:
            async for number, offset, length, data in self._lines(job["input_blob"]):
                digest.update(data)
                if number > self.rt.settings.max_requests:
                    raise BatchError(
                        400, "Input exceeds the configured request count", "too_many_requests"
                    )
                row, endpoint, alias, target = self._validate_row(data, job, seen, expected)
                expected = (endpoint, alias)
                request = {
                    "id": f"request:{number:09d}",
                    "pk": id,
                    "kind": "request",
                    "state": "staged",
                    "line": number,
                    "custom_id": row["custom_id"],
                    "offset": offset,
                    "length": length,
                    "generation": 0,
                    "attempt": 0,
                    "lease_until": 0,
                    "not_before": 0,
                }
                pending.append(request)
                if len(pending) >= 40:
                    await self._stage(id, pending)
                    pending = []
                    current = await self._control_heartbeat(id, token)
                    if current["status"] == "cancelling":
                        await self._release_control(id, token)
                        await self._finalize(id)
                        return
            if not number:
                raise BatchError(400, "Input has no requests", "empty_file")
            await self._stage(id, pending)
            if digest.hexdigest() != job["input_sha256"]:
                raise BatchError(400, "Input content changed", "input_file_modified")
            if self.rt.preflight:
                await self.rt.preflight.check(target)

            def ready(current):
                if current.get("control_token") != token:
                    raise Conflict("Validation owner was fenced")
                if current["status"] == "cancelling":
                    return None
                current.update(
                    status="in_progress",
                    in_progress_at=self.rt.now(),
                    validation_complete=True,
                    target=target,
                    alias=expected[1],
                    actual_endpoint=expected[0],
                    control_token=None,
                    control_lease_until=0,
                )
                current["request_counts"]["total"] = number
                return []

            current = await self._change_job(id, ready)
            if current["status"] == "cancelling":
                await self._release_control(id, token)
                await self._finalize(id)
            else:
                await self._dispatch_staged(id)
        except DeploymentPreflightError as exc:
            await self._fail_validation(
                id, token, BatchError(400, str(exc), exc.code, "body.model"), number
            )
        except BatchError as exc:
            await self._fail_validation(id, token, exc, number)
        except Conflict:
            # A replacement owner/terminal transition is authoritative.
            return

    async def _stage(self, id, requests):
        mutations = [Mutation("create", request["id"], request) for request in requests]
        if mutations:
            try:
                await self.rt.store.apply(id, mutations)
            except Conflict:
                for m in mutations:
                    if await self.rt.store.get(id, m.id) is None:
                        with suppress(Conflict):
                            await self.rt.store.apply(id, [m])

    async def _release_control(self, id, token):
        def release(job):
            if job.get("control_token") != token:
                return None
            job.update(control_token=None, control_lease_until=0)
            return []

        await self._change_job(id, release)

    async def _fail_validation(self, id, token, error, line):
        def fail(job):
            if job.get("control_token") != token or job["status"] in TERMINAL:
                return None
            job.update(
                status="failed",
                failed_at=self.rt.now(),
                control_token=None,
                control_lease_until=0,
                errors={
                    "object": "list",
                    "data": [
                        {
                            "code": error.code,
                            "message": error.message,
                            "line": line,
                            "param": error.param,
                        }
                    ],
                },
            )
            return []

        job = await self._change_job(id, fail)
        if job["status"] in TERMINAL:
            await self._unpin(job)

    async def _dispatch_staged(self, id):
        chunk = []
        # Keep query membership stable while paging: dispatch changes state but
        # never changes kind. Cosmos continuations are not snapshot iterators.
        async for request in self.rt.store.scan(pk=id, kind="request"):
            if request["state"] != "staged":
                continue
            chunk.append(request)
            if len(chunk) >= 40:
                if not await self._dispatch_chunk(id, chunk):
                    return
                chunk = []
        if chunk and not await self._dispatch_chunk(id, chunk):
            return

        # A separate query after the writes must confirm that no staged row was
        # omitted at a page boundary before recovery stops revisiting this job.
        async for _ in self.rt.store.scan(pk=id, kind="request", filters={"state": "staged"}):
            return

        def complete(job):
            if job["status"] != "in_progress":
                return None
            job["dispatch_complete"] = True
            return []

        await self._change_job(id, complete)

    async def _dispatch_chunk(self, id, requests):
        for _ in range(50):
            job = await self._job(id)
            if job["status"] != "in_progress" or not job["validation_complete"]:
                return False
            mutations = [Mutation("replace", id, job, job["_etag"])]
            for request in requests:
                event = self._event(id, "infer", request["id"], request["generation"])
                mutations.extend(
                    [
                        Mutation(
                            "replace",
                            request["id"],
                            {**request, "state": "pending"},
                            request["_etag"],
                        ),
                        Mutation("create", event["id"], event),
                    ]
                )
            try:
                await self.rt.store.apply(id, mutations)
                return True
            except Conflict:
                # Another dispatcher may have committed the same chunk.
                refreshed = []
                for request in requests:
                    current = await self.rt.store.get(id, request["id"])
                    if current and current["state"] == "staged":
                        refreshed.append(current)
                requests = refreshed
                if not requests:
                    return True
                await asyncio.sleep(0)
        raise RuntimeError("Dispatch contention exceeded retry budget")

    async def _heartbeat(self, batch_id, request_id, token):
        interval = max(1, min(30, self.rt.settings.request_lease_seconds // 3))
        while True:
            await asyncio.sleep(interval)
            request = await self.rt.store.get(batch_id, request_id)
            if (
                not request
                or request.get("lease_token") != token
                or request["state"] != "dispatched"
            ):
                return
            request["lease_until"] = self.rt.now() + self.rt.settings.request_lease_seconds
            try:
                await self.rt.store.apply(
                    batch_id, [Mutation("replace", request_id, request, request["_etag"])]
                )
            except Conflict:
                return

    async def handle_inference(self, message):
        id = message.get("batch_id")
        request_id = message.get("request_id")
        if not isinstance(id, str) or not isinstance(request_id, str):
            raise ValueError("Inference message requires batch_id and request_id")
        async with self._semaphore:
            job = await self._job(id)
            request = await self.rt.store.get(id, request_id)
            if (
                not request
                or request["state"] in ("succeeded", "failed")
                or job["status"] in TERMINAL
            ):
                return
            if job["status"] == "cancelling":
                await self._finalize(id)
                return
            if not job.get("validation_complete") or request["state"] == "staged":
                return
            if (
                message.get("generation", 0) != request["generation"]
                or request.get("lease_until", 0) > self.rt.now()
            ):
                return
            if request["state"] == "dispatched":
                await self._unknown(job, request)
                return
            if request.get("not_before", 0) > self.rt.now():
                await self._reschedule(job, request, request["not_before"])
                return
            if self.rt.preflight:
                try:
                    await self.rt.preflight.check(job["target"])
                except RuntimeError:
                    await self._reschedule(job, request, self.rt.now() + 300)
                    log.error("PTU preflight blocked work", extra={"batch_id": id})
                    return
            blocked = await self.rt.store.get("deployment:" + job["alias"], job["alias"])
            if blocked:
                await self._reschedule(job, request, self.rt.now() + 300)
                return
            token = uuid.uuid4().hex
            request.update(
                state="dispatched",
                lease_token=token,
                lease_until=self.rt.now() + self.rt.settings.request_lease_seconds,
                attempt=request["attempt"] + 1,
            )
            attempt = {
                "id": f"attempt:{request_id}:{request['attempt']}",
                "pk": id,
                "kind": "attempt",
                "request_id": request_id,
                "lease_token": token,
                "state": "dispatched",
                "cleanup_done": False,
                "started_at": self.rt.now(),
            }
            try:
                # Guard against cancellation changing the job between check and dispatch.
                await self.rt.store.apply(
                    id,
                    [
                        Mutation("replace", id, job, job["_etag"]),
                        Mutation("replace", request_id, request, request["_etag"]),
                        Mutation("create", attempt["id"], attempt),
                    ],
                )
            except Conflict:
                # Another request may only have updated counters; safely re-notify.
                await self._retry_notification(id, request_id)
                return
            heartbeat = asyncio.create_task(self._heartbeat(id, request_id, token))
            try:
                data = b"".join(
                    [
                        c
                        async for c in self.rt.blobs.read(
                            job["input_blob"], offset=request["offset"], length=request["length"]
                        )
                    ]
                )
                body = json.loads(data)["body"]
                if job["actual_endpoint"] == "/responses":
                    body.setdefault("store", False)
                result = await asyncio.wait_for(
                    self.rt.inference.invoke(job["target"], job["actual_endpoint"], body),
                    timeout=self.rt.settings.inference_timeout_seconds,
                )
                headers = {k.lower(): v for k, v in result.headers.items()}
                if any(
                    k in headers for k in ("x-ms-spillover-from-deployment", "x-ms-spillover-error")
                ):
                    await self._block_deployment(job)
                    await self._finish_request(
                        job,
                        request,
                        token,
                        error={
                            "code": "upstream_spillover_detected",
                            "message": "Target unexpectedly used Standard spillover; further work is paused",
                        },
                    )
                elif result.status_code == 429:
                    await self._reschedule(
                        job, request, self.rt.now() + self._retry_delay(headers), token=token
                    )
                else:
                    response = {
                        "status_code": result.status_code,
                        "request_id": result.request_id,
                        "body": result.body,
                    }
                    await self._finish_request(
                        job,
                        request,
                        token,
                        response=response,
                        success=200 <= result.status_code < 300,
                    )
            except asyncio.CancelledError:
                # Leave durable dispatched state for lease-based UNKNOWN recovery.
                raise
            except (TimeoutError, OSError) as exc:
                await self._finish_request(
                    job,
                    request,
                    token,
                    error={
                        "code": "request_outcome_unknown",
                        "message": "Inference outcome is unknown; this request was not automatically replayed",
                    },
                )
                log.warning(
                    "Unknown inference outcome",
                    extra={"batch_id": id, "exception_type": type(exc).__name__},
                )
            except Exception as exc:
                # Network adapters may expose their own transport exception hierarchy.
                # Commit an UNKNOWN only if the result was not already committed.
                current = await self.rt.store.get(id, request_id)
                if (
                    current
                    and current["state"] == "dispatched"
                    and current.get("lease_token") == token
                ):
                    # Persistence failures must propagate, not masquerade as inference errors.
                    if exc.__class__.__module__.startswith(
                        ("httpx", "httpcore", "azure.core.exceptions")
                    ):
                        await self._finish_request(
                            job,
                            request,
                            token,
                            error={
                                "code": "request_outcome_unknown",
                                "message": "Inference response was lost; automatic replay is disabled",
                            },
                        )
                    else:
                        raise
                else:
                    raise
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

    @staticmethod
    def _retry_delay(headers):
        try:
            if "retry-after-ms" in headers:
                return max(1, min(86400, int(float(headers["retry-after-ms"]) / 1000 + 0.999)))
            value = headers.get("retry-after", "1")
            try:
                return max(1, min(86400, int(float(value) + 0.999)))
            except ValueError:
                return max(
                    1,
                    min(
                        86400,
                        int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()) + 1,
                    ),
                )
        except (ValueError, TypeError, OverflowError):
            return 1

    async def _retry_notification(self, id, request_id):
        request = await self.rt.store.get(id, request_id)
        if request and request["state"] == "pending":
            await self._reschedule(await self._job(id), request, self.rt.now())

    async def _reschedule(self, job, request, when, token=None):
        current = await self.rt.store.get(job["id"], request["id"])
        if not current or current["state"] in ("succeeded", "failed"):
            return
        if token and current.get("lease_token") != token:
            return
        if not token and current["state"] == "dispatched":
            return
        current.update(
            state="pending",
            not_before=when,
            generation=current["generation"] + 1,
            lease_until=0,
            lease_token=None,
        )
        event = self._event(job["id"], "infer", current["id"], current["generation"], when)
        try:
            await self.rt.store.apply(
                job["id"],
                [
                    Mutation("replace", current["id"], current, current["_etag"]),
                    Mutation("create", event["id"], event),
                ],
            )
        except Conflict:
            return

    async def _finish_request(
        self, job, request, token, *, response=None, error=None, success=False
    ):
        current = await self.rt.store.get(job["id"], request["id"])
        batch = await self._job(job["id"])
        if (
            not current
            or current["state"] in ("succeeded", "failed")
            or current.get("lease_token") != token
            or batch["status"] in TERMINAL
        ):
            return
        envelope = {
            "id": "batch_req_"
            + hashlib.sha256(f"{job['id']}:{request['id']}".encode()).hexdigest()[:32],
            "custom_id": request["custom_id"],
            "response": response,
            "error": error,
        }
        outcome = (
            "unknown"
            if error and error.get("code") == "request_outcome_unknown"
            else ("response" if response is not None else "error")
        )
        blob = f"results/{job['id']}/{request['id'].replace(':', '-')}/{token}-{outcome}.jsonl"
        if not await self.rt.blobs.exists(blob):
            try:
                await self.rt.blobs.put(blob, bytes_stream(json_bytes(envelope)))
            except Conflict:
                pass
        for _ in range(50):
            current = await self.rt.store.get(job["id"], request["id"])
            batch = await self._job(job["id"])
            if (
                not current
                or current["state"] in ("succeeded", "failed")
                or current.get("lease_token") != token
                or batch["status"] in TERMINAL
            ):
                return
            current.update(
                state="succeeded" if success else "failed",
                result_blob=blob,
                finished_at=self.rt.now(),
                lease_until=0,
            )
            batch["request_counts"]["completed" if success else "failed"] += 1
            usage = (response or {}).get("body", {}).get("usage") or {}
            for k, upstream in [
                ("input_tokens", "prompt_tokens"),
                ("output_tokens", "completion_tokens"),
                ("total_tokens", "total_tokens"),
            ]:
                value = usage.get(k, usage.get(upstream, 0)) or 0
                if isinstance(value, (int, float)):
                    batch["usage"][k] += int(value)
            mutations = [
                Mutation("replace", current["id"], current, current["_etag"]),
                Mutation("replace", batch["id"], batch, batch["_etag"]),
            ]
            attempt_id = f"attempt:{request['id']}:{request['attempt']}"
            attempt = await self.rt.store.get(batch["id"], attempt_id)
            if attempt:
                attempt.update(
                    state="succeeded" if success else "failed",
                    finished_at=self.rt.now(),
                    result_blob=blob,
                )
                mutations.append(Mutation("replace", attempt_id, attempt, attempt["_etag"]))
            done = batch["request_counts"]["completed"] + batch["request_counts"]["failed"]
            if done == batch["request_counts"]["total"] or batch["status"] == "cancelling":
                event = self._event(batch["id"], "finalize", generation=done)
                if await self.rt.store.get(batch["id"], event["id"]) is None:
                    mutations.append(Mutation("create", event["id"], event))
            try:
                await self.rt.store.apply(batch["id"], mutations)
                return
            except Conflict:
                await asyncio.sleep(0)
        raise RuntimeError("Outcome contention exceeded retry budget")

    async def _unknown(self, job, request):
        await self._finish_request(
            job,
            request,
            request["lease_token"],
            error={
                "code": "request_outcome_unknown",
                "message": "Worker lease expired after dispatch; automatic replay is disabled",
            },
        )

    async def _block_deployment(self, job):
        doc = {
            "id": job["alias"],
            "pk": "deployment:" + job["alias"],
            "kind": "deployment_block",
            "target": job["target"],
            "created_at": self.rt.now(),
        }
        with suppress(Conflict):
            await self.rt.store.apply(doc["pk"], [Mutation("create", doc["id"], doc)])
        log.error(
            "Standard spillover detected; deployment paused",
            extra={"deployment_alias": job["alias"]},
        )

    async def _finalize(self, id):
        initial = await self._job(id)
        if initial["status"] in TERMINAL:
            await self._unpin(initial)
            return
        done = sum(initial["request_counts"][k] for k in ("completed", "failed"))
        cancelling = initial["status"] == "cancelling"
        if not cancelling and (
            not initial.get("validation_complete") or done < initial["request_counts"]["total"]
        ):
            return
        active = [
            r
            async for r in self.rt.store.scan(
                pk=id, kind="request", filters={"state": "dispatched"}
            )
        ]
        if cancelling and active:
            if (
                self.rt.now()
                < initial["cancelling_at"] + self.rt.settings.cancellation_drain_seconds
            ):
                return
            for request in active:
                await self._unknown(initial, request)
        job, token = await self._claim_control(id, "finalize")
        if not job:
            return

        def mark(current):
            if current.get("control_token") != token:
                raise Conflict("Finalization owner was fenced")
            current["finalizing_at"] = current["finalizing_at"] or self.rt.now()
            if current["status"] != "cancelling":
                current["status"] = "finalizing"
            return []

        job = await self._change_job(id, mark)
        files = {}
        for state, purpose in [("succeeded", "output"), ("failed", "error")]:
            file_id = "file-" + hashlib.sha256(f"{id}:{purpose}".encode()).hexdigest()[:32]
            # Outcomes are frozen before finalizing. Stable names allow another
            # owner to reuse a completed upload after a crash without orphans.
            blob = f"outputs/{id}/{purpose}.jsonl"
            found = False

            async def results(selected_state=state):
                nonlocal found
                async for request in self.rt.store.scan(
                    pk=id, kind="request", filters={"state": selected_state}
                ):
                    if request.get("result_blob"):
                        found = True
                        async for chunk in self.rt.blobs.read(request["result_blob"]):
                            yield chunk

            if await self.rt.blobs.exists(blob):
                info = await self._blob_info(blob)
                found = info.size > 0
            else:
                try:
                    info = await self.rt.blobs.put(blob, results())
                except Conflict:
                    info = await self._blob_info(blob)
                    found = info.size > 0
            if not found:
                await self.rt.blobs.delete(blob)
                continue
            now = self.rt.now()
            file = {
                "id": file_id,
                "pk": "file:" + file_id,
                "kind": "file",
                "object": "file",
                "owner": job["owner"],
                "filename": f"{id}_{purpose}.jsonl",
                "bytes": info.size,
                "sha256": info.sha256,
                "purpose": "batch_output",
                "status": "processed",
                "created_at": now,
                "expires_at": expiration(job.get("output_expires_after"), now),
                "deleted": False,
                "blob_name": blob,
            }
            existing = await self.rt.store.get(file["pk"], file_id)
            if existing is None:
                try:
                    await self.rt.store.apply(file["pk"], [Mutation("create", file_id, file)])
                except Conflict:
                    pass
            files[purpose] = file_id
            await self._control_heartbeat(id, token)

        def finish(current):
            if current.get("control_token") != token or current["status"] in TERMINAL:
                raise Conflict("Finalization owner was fenced")
            cancelled = current["status"] == "cancelling"
            current.update(
                status="cancelled" if cancelled else "completed",
                output_file_id=files.get("output"),
                error_file_id=files.get("error"),
                control_token=None,
                control_lease_until=0,
            )
            current["cancelled_at" if cancelled else "completed_at"] = self.rt.now()
            return []

        job = await self._change_job(id, finish)
        await self._unpin(job)
        log.info(
            "Batch finalized",
            extra={"batch_id": id, "request_counts": job["request_counts"], "usage": job["usage"]},
        )

    async def _blob_info(self, name):
        digest = hashlib.sha256()
        size = 0
        async for chunk in self.rt.blobs.read(name):
            digest.update(chunk)
            size += len(chunk)
        return BlobInfo(name, size, digest.hexdigest())

    async def _cleanup_attempts(self, job):
        terminal_at = next(
            (
                job.get(k)
                for k in ("completed_at", "cancelled_at", "failed_at", "expired_at")
                if job.get(k) is not None
            ),
            self.rt.now(),
        )
        if self.rt.now() < terminal_at + self.rt.settings.control_lease_seconds:
            return
        count = 0
        async for attempt in self.rt.store.scan(
            pk=job["id"], kind="attempt", filters={"cleanup_done": False}
        ):
            prefix = f"results/{job['id']}/{attempt['request_id'].replace(':', '-')}/{attempt['lease_token']}"
            for suffix in ("response", "error", "unknown"):
                await self.rt.blobs.delete(f"{prefix}-{suffix}.jsonl")
            attempt["cleanup_done"] = True
            with suppress(Conflict):
                await self.rt.store.apply(
                    job["id"], [Mutation("replace", attempt["id"], attempt, attempt["_etag"])]
                )
            count += 1
            if count >= 1000:
                break

    async def recover(self):
        now = self.rt.now()
        async for submission in self.rt.store.scan(kind="submission"):
            if submission["expires_at"] <= now:
                with suppress(Conflict):
                    await self.rt.store.apply(
                        submission["pk"],
                        [Mutation("delete", submission["id"], etag=submission["_etag"])],
                    )
        async for reservation in self.rt.store.scan(kind="batch_reservation"):
            if reservation.get("state") == "pending" and reservation["expires_at"] <= now:
                aborted = await self._abort_reservation(reservation["id"])
                if aborted:
                    await self._unpin(aborted)
            elif reservation.get("state") == "aborted":
                await self._unpin(reservation)
        async for job in self.rt.store.scan(kind="batch"):
            if job["status"] in TERMINAL:
                await self._unpin(job)
                await self._cleanup_attempts(job)
                continue
            intents = None
            async for request in self.rt.store.scan(pk=job["id"], kind="request"):
                if request["state"] == "dispatched" and request.get("lease_until", 0) <= now:
                    await self._unknown(job, request)
                elif request["state"] == "pending":
                    if intents is None:
                        intents = await self._latest_inference_intents(job["id"])
                    await self._ensure_pending_notification(
                        job, request, intents.get(request["id"]), now
                    )
            job = await self._job(job["id"])
            if job["status"] == "validating" and job.get("control_lease_until", 0) <= now:
                await self._validate(job["id"])
            elif job["status"] == "in_progress" and not job.get("dispatch_complete"):
                await self._dispatch_staged(job["id"])
            await self._finalize(job["id"])
        async for pin in self.rt.store.scan(kind="pin"):
            owner = await self.rt.store.get(pin["batch_id"], pin["batch_id"])
            if owner and owner.get("kind") == "batch_reservation":
                if owner.get("state") == "pending" and owner["expires_at"] <= now:
                    owner = await self._abort_reservation(pin["batch_id"])
                if not owner or owner.get("state") != "aborted":
                    continue
            elif owner and owner.get("kind") == "batch":
                if owner.get("status") not in TERMINAL:
                    continue
            else:
                if pin["created_at"] + self.rt.settings.control_lease_seconds < now:
                    log.warning(
                        "Pin owner is uncertain; retaining input pin",
                        extra={"batch_id": pin["batch_id"]},
                    )
                continue
            with suppress(Conflict):
                await self.rt.store.apply(
                    pin["pk"], [Mutation("delete", pin["id"], etag=pin["_etag"])]
                )
        async for file in self.rt.store.scan(kind="file"):
            expired = file.get("expires_at") is not None and file["expires_at"] <= now
            if not file.get("deleted") and not expired:
                continue
            pins = [p async for p in self.rt.store.scan(pk=file["pk"], kind="pin")]
            if pins:
                continue
            if not file.get("deleted"):
                file["deleted"] = True
                try:
                    await self.rt.store.apply(
                        file["pk"], [Mutation("replace", file["id"], file, file["_etag"])]
                    )
                except Conflict:
                    continue
            await self.rt.blobs.delete(file["blob_name"])
        if self.rt.preflight:
            async for block in self.rt.store.scan(kind="deployment_block"):
                try:
                    await self.rt.preflight.check(block["target"], force=True)
                except RuntimeError:
                    continue
                with suppress(Conflict):
                    await self.rt.store.apply(
                        block["pk"], [Mutation("delete", block["id"], etag=block["_etag"])]
                    )

        await self._reconcile_pending_quarantine()

        async def redrive_control(message):
            return await self._redrive_deadletter(message, control=True)

        async def redrive_inference(message):
            return await self._redrive_deadletter(message, control=False)

        await self.rt.broker.redrive_deadletters(self.rt.settings.control_queue, redrive_control)
        await self.rt.broker.redrive_deadletters(
            self.rt.settings.inference_queue, redrive_inference
        )
        await self.publish_outbox()
