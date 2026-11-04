"""Small, loop-local adapter between Azure Functions triggers and the engine."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

MAX_SERVICE_BUS_BODY_BYTES = 1_048_576
_instances: dict[asyncio.AbstractEventLoop, asyncio.Task[tuple[Any, Any]]] = {}


def parse_service_bus_json(message: Any) -> dict[str, Any]:
    """Decode one bounded JSON object without exposing its body in errors."""
    body = message.get_body()
    if (
        not isinstance(body, (bytes, bytearray, memoryview))
        or len(body) > MAX_SERVICE_BUS_BODY_BYTES
    ):
        raise ValueError("Invalid Service Bus JSON message")
    try:
        value = json.loads(bytes(body))
    except (UnicodeError, ValueError):
        raise ValueError("Invalid Service Bus JSON message") from None
    if not isinstance(value, dict):
        raise ValueError("Invalid Service Bus JSON message")
    return value


def change_feed_documents(documents: Sequence[Any]) -> list[dict[str, Any]]:
    """Convert Azure Functions DocumentList items for Engine.publish_outbox."""
    result: list[dict[str, Any]] = []
    for document in documents:
        if isinstance(document, Mapping):
            value = dict(document)
        else:
            try:
                value = json.loads(document.to_json())
            except (AttributeError, UnicodeError, ValueError):
                raise ValueError("Invalid Cosmos change feed document") from None
        if not isinstance(value, dict):
            raise ValueError("Invalid Cosmos change feed document")
        result.append(value)
    return result


async def _create_engine() -> tuple[Any, Any]:
    # Imports and settings validation happen inside a trigger invocation.
    from ptu_batch.bootstrap import build_runtime
    from ptu_batch.engine import Engine

    runtime = await build_runtime()
    try:
        return Engine(runtime), runtime
    except BaseException:
        await runtime.close()
        raise


async def get_engine() -> Any:
    """Cache a single runtime and Engine per running event loop."""
    loop = asyncio.get_running_loop()
    task = _instances.get(loop)
    if task is None:
        task = loop.create_task(_create_engine())
        _instances[loop] = task
    try:
        engine, _ = await asyncio.shield(task)
    except asyncio.CancelledError:
        # A cancelled invocation must not cancel another invocation's startup.
        if task.cancelled() and _instances.get(loop) is task:
            _instances.pop(loop)
        raise
    except BaseException:
        if _instances.get(loop) is task:
            _instances.pop(loop)
        raise
    return engine


async def close_runtime() -> None:
    """Close this event loop's cached runtime during controlled shutdown or tests."""
    task = _instances.pop(asyncio.get_running_loop(), None)
    if task is None:
        return
    _, runtime = await task
    await runtime.close()
