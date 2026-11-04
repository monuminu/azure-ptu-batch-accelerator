"""Async Azure infrastructure implementations of the ports used by the batch engine."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
import zlib
from collections.abc import AsyncIterable, AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import (
    CosmosBatchOperationError,
    CosmosHttpResponseError,
    CosmosResourceNotFoundError,
)
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusMessage, ServiceBusReceiveMode, ServiceBusSubQueue
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.amqp import AmqpMessageBodyType
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import BlobServiceClient

from ptu_batch.ports import BlobInfo, Conflict, Document, InferenceResult, Mutation

_SYSTEM_PROPERTIES = {"_etag", "_rid", "_self", "_attachments", "_ts"}
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESOURCE_ID = re.compile(
    r"^/subscriptions/([^/?#]+)/resourceGroups/([^/?#]+)/providers/"
    r"Microsoft\.CognitiveServices/accounts/([^/?#]+)/deployments/([^/?#]+)$",
    re.IGNORECASE,
)
_AZURE_RESOURCE_HOST = re.compile(
    r"^[a-z0-9][a-z0-9-]*\.(?:openai\.azure\.com|cognitiveservices\.azure\.com|services\.ai\.azure\.com)$",
    re.IGNORECASE,
)
_SUPPORTED_APIS = frozenset({"chat/completions", "responses"})
_BLOCK_SIZE = 4 * 1024 * 1024
_MAX_BLOCKS = 50_000
_MAX_BATCH_BYTES = 2 * 1024 * 1024
_MAX_NOTIFICATION_BYTES = 4096
_MAX_QUARANTINE_RAW_BYTES = 750 * 1024
_MAX_QUARANTINE_WRAPPER_BYTES = 1024 * 1024
_log = logging.getLogger(__name__)


def validated_deployment(deployment: Mapping[str, Any]) -> tuple[str, str, str, frozenset[str]]:
    """Return trusted endpoint, resource ID, deployment name and allowed APIs."""
    resource_id = deployment.get("resource_id")
    name = deployment.get("deployment_name")
    endpoint = deployment.get("resource_endpoint")
    if not isinstance(resource_id, str) or not (match := _RESOURCE_ID.fullmatch(resource_id)):
        raise ValueError("Invalid Azure deployment resource_id")
    if not isinstance(name, str) or not name or name.casefold() != match.group(4).casefold():
        raise ValueError("Deployment name does not match resource_id")
    if not isinstance(endpoint, str):
        raise ValueError("Invalid Azure resource_endpoint")
    url = urlsplit(endpoint)
    if (
        url.scheme != "https"
        or url.username
        or url.password
        or url.port is not None
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or not url.hostname
        or not _AZURE_RESOURCE_HOST.fullmatch(url.hostname)
        or url.hostname.split(".", 1)[0].casefold() != match.group(3).casefold()
    ):
        raise ValueError("Resource endpoint must be the matching public Azure resource hostname")
    apis = deployment.get("allowed_apis")
    if not isinstance(apis, (list, tuple, set, frozenset)) or not apis:
        raise ValueError("allowed_apis must name supported inference APIs")
    if not all(isinstance(api, str) for api in apis):
        raise ValueError("allowed_apis must contain API names")
    allowed = frozenset(api.removeprefix("/") for api in apis)
    if not allowed <= _SUPPORTED_APIS:
        raise ValueError("allowed_apis includes an unsupported API")
    return f"https://{url.hostname}", resource_id, name, allowed


class AzureDocumentStore:
    """Cosmos DB document store using one transactional batch per partition mutation."""

    def __init__(self, endpoint: str, database: str, container: str, credential=None):
        self._own_credential = credential is None
        self._credential = credential or DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=self._credential)
        self._container = self._client.get_database_client(database).get_container_client(container)

    async def get(self, pk: str, id: str) -> Document | None:
        try:
            return await self._container.read_item(item=id, partition_key=pk)
        except CosmosResourceNotFoundError:
            return None

    async def scan(
        self,
        *,
        kind: str | None = None,
        pk: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[Document]:
        conditions: list[str] = []
        parameters: list[dict[str, Any]] = []
        values: dict[str, Any] = dict(filters or {})
        if kind is not None:
            values["kind"] = kind
        if pk is not None:
            values["pk"] = pk
        for index, (field, value) in enumerate(values.items()):
            if not isinstance(field, str) or not _IDENTIFIER.fullmatch(field):
                raise ValueError("Invalid document filter field")
            parameter = f"@p{index}"
            conditions.append(f"c.{field} = {parameter}")
            parameters.append({"name": parameter, "value": value})
        query = "SELECT * FROM c"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        kwargs: dict[str, Any] = {"query": query, "parameters": parameters}
        if pk is not None:
            kwargs["partition_key"] = pk
        # The async SDK enables cross-partition queries when no pk is given.
        # The synchronous SDK's flag is not an async transport option.
        async for document in self._container.query_items(**kwargs):
            yield document

    async def apply(self, pk: str, mutations: Sequence[Mutation]) -> None:
        if not mutations:
            return
        if len(mutations) > 100:
            raise ValueError("Cosmos transaction exceeds 100 operations")
        operations: list[tuple[Any, ...]] = []
        total_bytes = 0
        for mutation in mutations:
            if mutation.op in ("create", "replace"):
                if mutation.document is None:
                    raise ValueError("Create and replace require a document")
                document = {
                    key: value
                    for key, value in mutation.document.items()
                    if key not in _SYSTEM_PROPERTIES
                }
                if (
                    document.get("id") != mutation.id
                    or document.get("pk") != pk
                    or not isinstance(document.get("kind"), str)
                    or not document["kind"]
                ):
                    raise ValueError("Document id, pk and kind must match transaction")
                total_bytes += len(
                    json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode()
                )
                if mutation.op == "create":
                    operations.append(("create", (document,)))
                else:
                    if not mutation.etag:
                        raise ValueError("Replace requires an observed ETag")
                    operations.append(
                        ("replace", (mutation.id, document), {"if_match_etag": mutation.etag})
                    )
            elif mutation.op == "delete":
                if not mutation.etag:
                    raise ValueError("Delete requires an observed ETag")
                operations.append(("delete", (mutation.id,), {"if_match_etag": mutation.etag}))
            else:
                raise ValueError("Unsupported document mutation")
            total_bytes += (
                len(mutation.id.encode())
                + (len(mutation.etag.encode()) if mutation.etag else 0)
                + 128
            )
        if total_bytes > _MAX_BATCH_BYTES:
            raise ValueError("Cosmos transaction exceeds 2 MB")
        try:
            await self._container.execute_item_batch(batch_operations=operations, partition_key=pk)
        except CosmosBatchOperationError as exc:
            statuses = {getattr(exc, "status_code", None)}
            statuses.update(
                response.get("statusCode") for response in (exc.operation_responses or [])
            )
            if statuses & {409, 412}:
                raise Conflict("Document already exists or ETag changed") from exc
            raise
        except CosmosHttpResponseError as exc:
            if exc.status_code in (409, 412):
                raise Conflict("Document already exists or ETag changed") from exc
            raise

    async def close(self) -> None:
        await self._client.close()
        if self._own_credential:
            await self._credential.close()


class AzureBlobStore:
    """Immutable block blob store that stages bounded chunks before commit."""

    def __init__(self, account_url: str, container: str, credential=None):
        self._own_credential = credential is None
        self._credential = credential or DefaultAzureCredential()
        self._client = BlobServiceClient(account_url=account_url, credential=self._credential)
        self._container = self._client.get_container_client(container)

    async def put(
        self,
        name: str,
        chunks: AsyncIterable[bytes],
        *,
        content_type: str = "application/jsonl",
    ) -> BlobInfo:
        blob = self._container.get_blob_client(name)
        digest = hashlib.sha256()
        size = 0
        block_ids: list[str] = []
        prefix = secrets.token_bytes(12)
        buffer = bytearray()

        async def stage(data: bytes) -> None:
            if len(block_ids) >= _MAX_BLOCKS:
                raise ValueError("Blob exceeds 50,000 blocks")
            block_id = base64.b64encode(prefix + len(block_ids).to_bytes(4, "big")).decode()
            await blob.stage_block(block_id=block_id, data=data, length=len(data))
            block_ids.append(block_id)

        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("Blob stream must yield bytes")
                digest.update(chunk)
                size += len(chunk)
                view = memoryview(chunk)
                cursor = 0
                while cursor < len(view):
                    take = min(_BLOCK_SIZE - len(buffer), len(view) - cursor)
                    buffer.extend(view[cursor : cursor + take])
                    cursor += take
                    if len(buffer) == _BLOCK_SIZE:
                        await stage(bytes(buffer))
                        buffer.clear()
            if buffer:
                await stage(bytes(buffer))
            await blob.commit_block_list(
                block_ids,
                content_settings=ContentSettings(content_type=content_type),
                match_condition=MatchConditions.IfMissing,
            )
        except ResourceExistsError as exc:
            raise Conflict("Blob name already exists") from exc
        except Exception as exc:
            # Staged blocks are uncommitted and never visible as the blob. They expire in Azure.
            if getattr(exc, "status_code", None) in (409, 412):
                raise Conflict("Blob name already exists") from exc
            raise
        return BlobInfo(name=name, size=size, sha256=digest.hexdigest())

    async def read(
        self,
        name: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> AsyncIterator[bytes]:
        if offset < 0 or (length is not None and length < 0):
            raise ValueError("Blob range must be nonnegative")
        downloader = await self._container.get_blob_client(name).download_blob(
            offset=offset, length=length
        )
        async for chunk in downloader.chunks():
            yield chunk

    async def delete(self, name: str) -> None:
        try:
            await self._container.get_blob_client(name).delete_blob()
        except ResourceNotFoundError:
            return

    async def exists(self, name: str) -> bool:
        return await self._container.get_blob_client(name).exists()

    async def close(self) -> None:
        await self._client.close()
        if self._own_credential:
            await self._credential.close()


class ServiceBusBroker:
    """Send compact JSON work notifications with stable caller-supplied IDs."""

    def __init__(self, fqdn: str, credential=None):
        self._own_credential = credential is None
        self._credential = credential or DefaultAzureCredential()
        self._client = ServiceBusClient(fully_qualified_namespace=fqdn, credential=self._credential)
        self._senders: dict[str, Any] = {}
        self._sender_lock = asyncio.Lock()

    async def publish(
        self,
        queue: str,
        payload: Document,
        *,
        message_id: str,
        not_before: int | None = None,
    ) -> None:
        if not message_id:
            raise ValueError("message_id is required")
        if queue not in self._senders:
            async with self._sender_lock:
                if queue not in self._senders:
                    sender = self._client.get_queue_sender(queue_name=queue)
                    self._senders[queue] = await sender.__aenter__()
        message = ServiceBusMessage(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            message_id=message_id,
            content_type="application/json",
        )
        sender = self._senders[queue]
        if not_before is not None and not_before > time.time():
            await sender.schedule_messages(message, datetime.fromtimestamp(not_before, UTC))
        else:
            await sender.send_messages(message)

    @staticmethod
    def _deadletter_envelope(message, queue) -> Document | None:
        if message.body_type is AmqpMessageBodyType.DATA:
            body = message.body
            chunks = (body,) if isinstance(body, (bytes, bytearray, memoryview)) else body
            try:
                iterator = iter(chunks)
            except TypeError:
                return None
            data = bytearray()
            for chunk in iterator:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    return None
                if len(data) + len(chunk) > _MAX_QUARANTINE_RAW_BYTES:
                    return None
                data.extend(chunk)
            raw = bytes(data)
            reason = None
            raw_encoding = "data"
        else:
            value = message.body
            body_kind = "value" if message.body_type is AmqpMessageBodyType.VALUE else "sequence"
            if isinstance(value, str):
                raw = value.encode("utf-8", errors="surrogatepass")
                raw_encoding = f"{body_kind}-utf8"
            elif isinstance(value, (bytes, bytearray, memoryview)):
                raw = bytes(value)
                raw_encoding = f"{body_kind}-bytes"
            else:
                raw = json.dumps(value, default=str, separators=(",", ":")).encode()
                raw_encoding = f"{body_kind}-json"
                if len(raw) > _MAX_QUARANTINE_RAW_BYTES:
                    raw = zlib.compress(raw, level=9)
                    raw_encoding += "-zlib"
            reason = "unsupported_body_type"
        if len(raw) > _MAX_QUARANTINE_RAW_BYTES:
            return None
        payload = None
        if reason is None:
            if len(raw) > _MAX_NOTIFICATION_BYTES:
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
            "message_id": str(message.message_id),
            "payload": payload,
            "raw_body_b64": base64.b64encode(raw).decode("ascii"),
            "raw_encoding": raw_encoding,
            "reason": reason,
        }
        if (
            len(json.dumps(envelope, separators=(",", ":")).encode())
            > _MAX_QUARANTINE_WRAPPER_BYTES
        ):
            return None
        return envelope

    async def redrive_deadletters(self, queue, handler, *, max_messages=100) -> int:
        limit = max(0, min(max_messages, 100))
        if not limit:
            return 0
        settled = 0
        async with self._client.get_queue_receiver(
            queue_name=queue,
            sub_queue=ServiceBusSubQueue.DEAD_LETTER,
            receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
            prefetch_count=0,
            max_wait_time=1,
        ) as receiver:
            messages = await receiver.receive_messages(max_message_count=limit, max_wait_time=1)
            for message in messages:
                envelope = self._deadletter_envelope(message, queue)
                if envelope is None:
                    _log.warning(
                        "Dead letter notification exceeds quarantine bounds",
                        extra={"message_id": message.message_id},
                    )
                    continue
                if await handler(envelope):
                    await receiver.complete_message(message)
                    settled += 1
        return settled

    async def close(self) -> None:
        for sender in self._senders.values():
            await sender.close()
        self._senders.clear()
        await self._client.close()
        if self._own_credential:
            await self._credential.close()


class AzureInferenceClient:
    """One HTTP POST per inference request, with no adapter-level retries."""

    def __init__(self, credential=None, timeout_seconds: int = 300, http_client=None):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._own_credential = credential is None
        self._credential = credential or DefaultAzureCredential()
        self._own_http = http_client is None
        self._http = http_client or httpx.AsyncClient()
        self._timeout_seconds = timeout_seconds

    async def invoke(
        self,
        deployment: Mapping[str, Any],
        endpoint: str,
        body: Document,
    ) -> InferenceResult:
        base_url, _, name, allowed = validated_deployment(deployment)
        api = endpoint.lstrip("/")
        if endpoint not in ("/chat/completions", "/responses") or api not in allowed:
            raise ValueError("Inference API is not allowed for this deployment")
        scope = deployment.get("token_scope") or "https://cognitiveservices.azure.com/.default"
        token = await self._credential.get_token(scope)
        request_body = {**body, "model": name}
        response = await self._http.post(
            f"{base_url}/openai/v1/{api}",
            json=request_body,
            headers={"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"},
            timeout=self._timeout_seconds,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text}
        if not isinstance(data, dict):
            data = {"data": data}
        headers = dict(response.headers)
        return InferenceResult(
            status_code=response.status_code,
            body=data,
            request_id=(
                headers.get("x-ms-request-id")
                or headers.get("apim-request-id")
                or headers.get("x-request-id")
            ),
            headers=headers,
        )

    async def close(self) -> None:
        if self._own_http:
            await self._http.aclose()
        if self._own_credential:
            await self._credential.close()
