"""Small async infrastructure contracts; documents use id/pk/kind and optional _etag.

Transactions are atomic within a single partition. replace/delete require the
observed ETag. Iterators must be lazy: payloads and 100k-line ledgers do not fit
in one process-wide list. Azure and local implementations share these contracts.
"""

from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Document = dict[str, Any]


class Conflict(Exception):
    """An item already exists or its ETag no longer matches."""


@dataclass(frozen=True)
class Mutation:
    op: Literal["create", "replace", "delete"]
    id: str
    document: Document | None = None
    etag: str | None = None


class DocumentStore(Protocol):
    async def get(self, pk: str, id: str) -> Document | None: ...

    def scan(
        self,
        *,
        kind: str | None = None,
        pk: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[Document]: ...

    async def apply(self, pk: str, mutations: Sequence[Mutation]) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class BlobInfo:
    name: str
    size: int
    sha256: str


class BlobStore(Protocol):
    async def put(
        self,
        name: str,
        chunks: AsyncIterable[bytes],
        *,
        content_type: str = "application/jsonl",
    ) -> BlobInfo: ...

    def read(
        self,
        name: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> AsyncIterator[bytes]: ...

    async def delete(self, name: str) -> None: ...

    async def exists(self, name: str) -> bool: ...

    async def close(self) -> None: ...


class Broker(Protocol):
    async def publish(
        self,
        queue: str,
        payload: Document,
        *,
        message_id: str,
        not_before: int | None = None,
    ) -> None: ...

    async def redrive_deadletters(
        self,
        queue: str,
        handler: Callable[[Document], Awaitable[bool]],
        *,
        max_messages: int = 100,
    ) -> int: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject_id: str
    roles: frozenset[str] = field(default_factory=frozenset)

    @property
    def owner(self) -> str:
        return f"{self.tenant_id}:{self.subject_id}"


class Authenticator(Protocol):
    async def authenticate(self, bearer: str) -> Principal: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class InferenceResult:
    status_code: int
    body: Document
    request_id: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


class InferenceClient(Protocol):
    async def invoke(
        self,
        deployment: Mapping[str, Any],
        endpoint: str,
        body: Document,
    ) -> InferenceResult: ...

    async def close(self) -> None: ...


class Preflight(Protocol):
    async def check(self, deployment: Mapping[str, Any], force: bool = False) -> None: ...

    async def close(self) -> None: ...
