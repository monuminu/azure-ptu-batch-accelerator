"""Explicit local/demo infrastructure. The document ledger is volatile.

No local adapter is selected by the Azure runtime. File writes use an atomic
no-clobber publication, so failed uploads never become readable inputs.
"""

import asyncio
import base64
import copy
import hashlib
import json
import os
import tempfile
import time
from collections.abc import AsyncIterable, Mapping, Sequence
from pathlib import Path

from .errors import AuthError
from .ports import BlobInfo, Conflict, InferenceResult, Mutation, Principal


class MemoryDocumentStore:
    def __init__(self):
        self._items = {}
        self._lock = asyncio.Lock()
        self._version = 0

    async def get(self, pk, id):
        async with self._lock:
            return copy.deepcopy(self._items.get((pk, id)))

    async def scan(self, *, kind=None, pk=None, filters=None):
        async with self._lock:
            snapshot = [
                copy.deepcopy(v)
                for (p, _), v in self._items.items()
                if (pk is None or pk == p)
                and (kind is None or v.get("kind") == kind)
                and all(v.get(k) == val for k, val in (filters or {}).items())
            ]
        for document in snapshot:
            yield document

    async def apply(self, pk, mutations: Sequence[Mutation]):
        if len(mutations) > 100:
            raise ValueError("Transactions support at most 100 mutations")
        async with self._lock:
            staged = dict(self._items)
            for m in mutations:
                current = staged.get((pk, m.id))
                if m.op == "create":
                    if current is not None:
                        raise Conflict(m.id)
                elif current is None or not m.etag or current["_etag"] != m.etag:
                    raise Conflict(m.id)
                if m.op == "delete":
                    del staged[(pk, m.id)]
                else:
                    if m.document is None or m.document["pk"] != pk or m.document["id"] != m.id:
                        raise ValueError("Document identity must match its transaction")
                    self._version += 1
                    staged[(pk, m.id)] = {**copy.deepcopy(m.document), "_etag": str(self._version)}
            self._items = staged

    async def close(self):
        pass


class LocalBlobStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name):
        result = (self.root / name).resolve()
        if not result.is_relative_to(self.root) or result == self.root:
            raise ValueError("Invalid blob name")
        return result

    async def put(self, name, chunks: AsyncIterable[bytes], *, content_type="application/jsonl"):
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, scratch = tempfile.mkstemp(dir=path.parent, prefix=".upload-")
        size, digest = 0, hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as stream:
                async for chunk in chunks:
                    stream.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            try:
                os.link(scratch, path)
            except FileExistsError as exc:
                raise Conflict(name) from exc
        finally:
            Path(scratch).unlink(missing_ok=True)
        return BlobInfo(name, size, digest.hexdigest())

    async def read(self, name, *, offset=0, length=None):
        if offset < 0 or (length is not None and length < 0):
            raise ValueError("Invalid blob range")
        with self._path(name).open("rb") as stream:
            stream.seek(offset)
            remaining = length
            while remaining is None or remaining:
                data = stream.read(65536 if remaining is None else min(65536, remaining))
                if not data:
                    break
                if remaining is not None:
                    remaining -= len(data)
                yield data

    async def delete(self, name):
        self._path(name).unlink(missing_ok=True)

    async def exists(self, name):
        return self._path(name).is_file()

    async def close(self):
        pass


class MemoryBroker:
    def __init__(self):
        self.messages = []
        self.dead_letters = []
        self._ids = set()

    async def publish(self, queue, payload, *, message_id, not_before=None):
        if message_id not in self._ids:
            self._ids.add(message_id)
            self.messages.append(
                {
                    "queue": queue,
                    "payload": copy.deepcopy(payload),
                    "message_id": message_id,
                    "not_before": not_before or 0,
                }
            )

    async def redrive_deadletters(self, queue, handler, *, max_messages=100):
        settled = 0
        selected = [item for item in self.dead_letters if item["queue"] == queue][
            : max(0, min(max_messages, 100))
        ]
        for item in selected:
            raw = item.get("raw_body")
            if raw is None:
                raw = json.dumps(item.get("payload"), separators=(",", ":")).encode()
            if len(raw) > 750 * 1024:
                continue
            reason = None
            payload = None
            if len(raw) > 4096:
                reason = "oversized_notification"
            else:
                try:
                    parsed = json.loads(raw)
                except (UnicodeError, ValueError):
                    reason = "invalid_json"
                else:
                    if isinstance(parsed, dict):
                        payload = parsed
                    else:
                        reason = "invalid_schema"
            envelope = {
                "source_queue": queue,
                "message_id": str(item["message_id"]),
                "payload": payload,
                "raw_body_b64": base64.b64encode(raw).decode("ascii"),
                "raw_encoding": "data",
                "reason": reason,
            }
            if await handler(envelope):
                self.dead_letters.remove(item)
                settled += 1
        return settled

    def pop_ready(self, now=None):
        now = int(time.time()) if now is None else now
        for i, message in enumerate(self.messages):
            if message["not_before"] <= now:
                return self.messages.pop(i)
        return None

    async def close(self):
        pass


class LocalAuthenticator:
    async def authenticate(self, bearer):
        if bearer != "local-dev-token":
            raise AuthError()
        return Principal("local", "developer", frozenset({"Batch.ReadWrite"}))

    async def close(self):
        pass


class EchoInference:
    """Synthetic local results, explicitly not a model or cost estimate."""

    async def invoke(self, deployment: Mapping, endpoint, body):
        text = "LOCAL DEMO: " + json.dumps(body.get("messages", body.get("input", "")))[:500]
        common = {
            "model": deployment["deployment_name"],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
        if endpoint == "/responses":
            result = {
                "id": "resp_local",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_local",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                ],
                **common,
            }
        else:
            result = {
                "id": "chatcmpl-local",
                "object": "chat.completion",
                "created": int(time.time()),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
                **common,
            }
        return InferenceResult(200, result, "local-request")

    async def close(self):
        pass
