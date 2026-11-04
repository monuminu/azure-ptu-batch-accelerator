"""Adapter contracts exercised with network boundaries replaced by narrow fakes."""

import asyncio
import hashlib
import json

import httpx
import pytest
from azure.core import MatchConditions
from azure.core.pipeline.transport import AsyncHttpResponse, AsyncHttpTransport
from azure.cosmos import _base
from azure.cosmos.exceptions import CosmosBatchOperationError
from azure.storage.blob._blob_client_helpers import _commit_block_list_options
from azure.storage.blob.aio import BlobServiceClient

from ptu_batch.adapters.azure import (
    AzureBlobStore,
    AzureDocumentStore,
    AzureInferenceClient,
    ServiceBusBroker,
)
from ptu_batch.ports import Conflict, Mutation


class TokenCredential:
    def __init__(self):
        self.scopes = []

    async def get_token(self, scope):
        self.scopes.append(scope)
        return type("Token", (), {"token": "test-token"})()


class BatchContainer:
    def __init__(self, fail_status=None):
        self.calls = []
        self.fail_status = fail_status

    async def execute_item_batch(self, batch_operations, partition_key):
        self.calls.append((batch_operations, partition_key))
        if self.fail_status:
            raise CosmosBatchOperationError(
                error_index=0,
                headers={},
                status_code=self.fail_status,
                operation_responses=[{"statusCode": self.fail_status}],
            )


def document_store(container):
    store = AzureDocumentStore.__new__(AzureDocumentStore)
    store._container = container
    return store


@pytest.mark.asyncio
async def test_cosmos_batch_sends_each_etag_and_sanitizes_documents():
    container = BatchContainer()
    store = document_store(container)
    await store.apply(
        "batch-1",
        [
            Mutation("create", "new", {"id": "new", "pk": "batch-1", "kind": "item", "_rid": "x"}),
            Mutation(
                "replace",
                "old",
                {"id": "old", "pk": "batch-1", "kind": "item", "_etag": "stale"},
                '"e1"',
            ),
            Mutation("delete", "gone", etag='"e2"'),
        ],
    )
    operations, pk = container.calls[0]
    assert pk == "batch-1"
    assert operations == [
        ("create", ({"id": "new", "pk": "batch-1", "kind": "item"},)),
        (
            "replace",
            ("old", {"id": "old", "pk": "batch-1", "kind": "item"}),
            {"if_match_etag": '"e1"'},
        ),
        ("delete", ("gone",), {"if_match_etag": '"e2"'}),
    ]
    serialized = _base._format_batch_operations(operations)
    assert serialized[1]["ifMatch"] == '"e1"'
    assert serialized[2]["ifMatch"] == '"e2"'


@pytest.mark.asyncio
async def test_cosmos_conflict_does_not_retry_partial_batch():
    container = BatchContainer(fail_status=412)
    store = document_store(container)
    with pytest.raises(Conflict):
        await store.apply("batch-1", [Mutation("delete", "a", etag='"old"')])
    assert len(container.calls) == 1


@pytest.mark.asyncio
async def test_cosmos_rejects_unconditional_replace_and_oversized_transaction():
    container = BatchContainer()
    store = document_store(container)
    with pytest.raises(ValueError):
        await store.apply("p", [Mutation("replace", "x", {"id": "x", "pk": "p", "kind": "x"})])
    with pytest.raises(ValueError):
        await store.apply("p", [Mutation("delete", str(i), etag='"e"') for i in range(101)])
    assert container.calls == []


class FakeBlob:
    def __init__(self, data=b"", exists=False):
        self.data = data
        self.present = exists
        self.staged = {}
        self.commits = []

    async def stage_block(self, block_id, data, **kwargs):
        self.staged[block_id] = data

    async def commit_block_list(self, block_list, **kwargs):
        self.commits.append((block_list, kwargs))
        if self.present:
            from azure.core.exceptions import ResourceExistsError

            raise ResourceExistsError("exists")
        self.data = b"".join(self.staged[block_id] for block_id in block_list)
        self.present = True

    async def download_blob(self, offset=0, length=None):
        data = self.data[offset:] if length is None else self.data[offset : offset + length]
        return type("Download", (), {"chunks": lambda _: _chunks(data)})()

    async def exists(self):
        return self.present

    async def delete_blob(self):
        self.present = False


async def _chunks(data):
    for i in range(0, len(data), 2):
        yield data[i : i + 2]


class FakeBlobContainer:
    def __init__(self, blob):
        self.blob = blob

    def get_blob_client(self, name):
        return self.blob


def blob_store(blob):
    store = AzureBlobStore.__new__(AzureBlobStore)
    store._container = FakeBlobContainer(blob)
    return store


@pytest.mark.asyncio
async def test_blob_streaming_hash_offsets_and_immutability():
    blob = FakeBlob()
    store = blob_store(blob)

    async def source():
        yield b"abcd"
        yield b"ef"

    info = await store.put("file", source())
    assert (info.name, info.size, info.sha256) == ("file", 6, hashlib.sha256(b"abcdef").hexdigest())
    assert blob.commits[0][1]["match_condition"] is MatchConditions.IfMissing
    assert "etag" not in blob.commits[0][1]
    commit_options = _commit_block_list_options(
        block_list=blob.commits[0][0],
        **blob.commits[0][1],
    )
    assert commit_options["modified_access_conditions"].if_none_match == "*"
    assert [part async for part in store.read("file", offset=1, length=3)] == [b"bc", b"d"]
    with pytest.raises(Conflict):
        await store.put("file", source())
    assert blob.data == b"abcdef"


class RecordingBlobResponse(AsyncHttpResponse):
    def __init__(self, request):
        super().__init__(request, None)
        self.status_code = 201
        self.headers = {
            "ETag": '"committed"',
            "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
        }

    async def __aenter__(self):
        return self

    async def load_body(self):
        return None

    def body(self):
        return b""


class RecordingBlobTransport(AsyncHttpTransport):
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def open(self):
        return None

    async def close(self):
        return None

    async def send(self, request, **kwargs):
        if "etag" in kwargs:
            raise TypeError("unexpected keyword argument 'etag'")
        self.requests.append(request)
        return RecordingBlobResponse(request)


@pytest.mark.asyncio
async def test_blob_put_commits_immutable_block_list_through_real_sdk_pipeline():
    transport = RecordingBlobTransport()
    service = BlobServiceClient(
        account_url="https://example.blob.core.windows.net",
        credential=None,
        transport=transport,
    )
    store = AzureBlobStore.__new__(AzureBlobStore)
    store._container = service.get_container_client("files")

    async def source():
        yield b"one block"

    try:
        info = await store.put("file", source())
    finally:
        await service.close()

    assert info.size == len(b"one block")
    commits = [request for request in transport.requests if "comp=blocklist" in request.url]
    assert len(commits) == 1
    assert commits[0].headers["If-None-Match"] == "*"


@pytest.mark.asyncio
async def test_blob_failed_stream_does_not_commit():
    blob = FakeBlob()
    store = blob_store(blob)

    async def broken():
        yield b"first"
        raise RuntimeError("stream stopped")

    with pytest.raises(RuntimeError, match="stream stopped"):
        await store.put("file", broken())
    assert blob.commits == []


class FakeSender:
    def __init__(self):
        self.sent = []
        self.scheduled = []

    async def __aenter__(self):
        return self

    async def send_messages(self, message):
        self.sent.append(message)

    async def schedule_messages(self, message, when):
        self.scheduled.append((message, when))

    async def close(self):
        pass


class FakeBus:
    def __init__(self):
        self.sender = FakeSender()
        self.calls = []

    def get_queue_sender(self, queue_name):
        self.calls.append(queue_name)
        return self.sender

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_service_bus_publishes_json_with_stable_message_id_and_schedules_future():
    broker = ServiceBusBroker.__new__(ServiceBusBroker)
    broker._client = FakeBus()
    broker._senders = {}
    broker._sender_lock = asyncio.Lock()
    await broker.publish("jobs", {"batch_id": "b1"}, message_id="m1")
    await broker.publish("jobs", {"batch_id": "b2"}, message_id="m2", not_before=4102444800)
    assert broker._client.calls == ["jobs"]
    assert broker._client.sender.sent[0].message_id == "m1"
    assert json.loads(str(broker._client.sender.sent[0])) == {"batch_id": "b1"}
    assert broker._client.sender.scheduled[0][0].message_id == "m2"


@pytest.mark.asyncio
async def test_service_bus_concurrent_publish_reuses_one_open_sender():
    class SlowSender(FakeSender):
        async def __aenter__(self):
            await asyncio.sleep(0)
            return self

    broker = ServiceBusBroker.__new__(ServiceBusBroker)
    broker._client = FakeBus()
    broker._client.sender = SlowSender()
    broker._senders = {}
    broker._sender_lock = asyncio.Lock()
    await asyncio.gather(
        broker.publish("jobs", {"batch_id": "a"}, message_id="a"),
        broker.publish("jobs", {"batch_id": "b"}, message_id="b"),
    )
    assert broker._client.calls == ["jobs"]


@pytest.mark.asyncio
async def test_inference_returns_429_without_retry_and_preserves_headers():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            429,
            json={"error": {"code": "rate_limit"}},
            headers={
                "x-ms-request-id": "r1",
                "retry-after": "3",
                "x-ms-spillover-deployment": "unused",
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    credential = TokenCredential()
    client = AzureInferenceClient(credential=credential, http_client=http)
    result = await client.invoke(
        {
            "resource_endpoint": "https://account.openai.azure.com",
            "deployment_name": "ptu-deployment",
            "resource_id": "/subscriptions/s/resourceGroups/r/providers/Microsoft.CognitiveServices/accounts/account/deployments/ptu-deployment",
            "allowed_apis": ["chat/completions"],
        },
        "/chat/completions",
        {"model": "user-supplied", "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert result.status_code == 429
    assert result.body == {"error": {"code": "rate_limit"}}
    assert result.request_id == "r1"
    assert result.headers["retry-after"] == "3"
    assert len(requests) == 1
    assert requests[0].url.path == "/openai/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer test-token"
    assert json.loads(requests[0].content)["model"] == "ptu-deployment"
    await http.aclose()


@pytest.mark.asyncio
async def test_inference_rejects_untrusted_endpoint_before_token_fetch():
    credential = TokenCredential()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    client = AzureInferenceClient(credential=credential, http_client=http)
    deployment = {
        "resource_endpoint": "https://evil.example.com",
        "deployment_name": "d",
        "resource_id": "/subscriptions/s/resourceGroups/r/providers/Microsoft.CognitiveServices/accounts/a/deployments/d",
        "allowed_apis": ["responses"],
    }
    with pytest.raises(ValueError):
        await client.invoke(deployment, "/responses", {"input": "hi"})
    assert credential.scopes == []
    await http.aclose()


@pytest.mark.asyncio
async def test_inference_accepts_configured_leading_slash_allowed_api():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"output": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = AzureInferenceClient(credential=TokenCredential(), http_client=http)
    deployment = {
        "resource_endpoint": "https://account.openai.azure.com/",
        "resource_id": "/subscriptions/s/resourceGroups/r/providers/Microsoft.CognitiveServices/accounts/account/deployments/ptu",
        "deployment_name": "ptu",
        "allowed_apis": ["/chat/completions", "/responses"],
    }
    result = await client.invoke(deployment, "/responses", {"input": "hello"})
    assert result.status_code == 200
    assert requests[0].url.path == "/openai/v1/responses"
    await http.aclose()


@pytest.mark.asyncio
async def test_cosmos_scan_parameterizes_values_and_rejects_unsafe_fields():
    class QueryContainer:
        def query_items(self, **kwargs):
            self.kwargs = kwargs
            return self

        def __aiter__(self):
            async def pages():
                yield {"id": "1", "pk": "p", "kind": "item", "_etag": '"e"'}

            return pages()

    container = QueryContainer()
    store = document_store(container)
    rows = [row async for row in store.scan(kind="item", pk="p", filters={"owner": "x' OR 1=1"})]
    assert rows[0]["_etag"] == '"e"'
    assert "x' OR 1=1" not in container.kwargs["query"]
    assert {p["value"] for p in container.kwargs["parameters"]} == {"item", "p", "x' OR 1=1"}
    assert container.kwargs["partition_key"] == "p"
    with pytest.raises(ValueError):
        [row async for row in store.scan(filters={"owner; DROP": "x"})]


async def test_cosmos_async_query_uses_sdk_cross_partition_option_not_transport_kwarg():
    from azure.cosmos.aio import ContainerProxy

    class Connection:
        def QueryItems(self, **kwargs):
            if "enable_cross_partition_query" in kwargs:
                raise TypeError("unsupported option leaked to async transport")
            assert kwargs["options"]["enableCrossPartitionQuery"] is True
            return _empty()

    async def _empty():
        if False:
            yield {}

    container = ContainerProxy.__new__(ContainerProxy)
    container.client_connection = Connection()
    container.container_link = "/dbs/test/colls/ledger"
    store = document_store(container)
    assert [doc async for doc in store.scan(kind="batch")] == []
