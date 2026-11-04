"""Regression cases at the public JSONL and upload boundaries."""

import asyncio

import httpx
import pytest
from test_auth import AUDIENCE, TENANT, keypair, token
from test_engine import row, submit

from ptu_batch.adapters.auth import EntraAuthenticator
from ptu_batch.api import create_app
from ptu_batch.errors import AuthError

pytest_plugins = ("test_engine",)


async def test_malformed_tools_value_fails_validation_without_sticking_batch(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row(tools=123)])

    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})

    state = await engine.get_batch(principal, batch["id"])
    assert state["status"] == "failed"
    assert state["errors"]["data"]
    assert runtime.inference.calls == []


async def test_non_json_numeric_constant_is_rejected_before_dispatch(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row(temperature=float("nan"))])

    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})

    state = await engine.get_batch(principal, batch["id"])
    assert state["status"] == "failed"
    assert runtime.inference.calls == []


async def test_chunked_oversize_upload_returns_standard_413(rig):
    runtime, _, _ = rig
    runtime.settings.max_file_bytes = 1
    app = create_app(runtime)
    boundary = "edge-boundary"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="purpose"\r\n\r\n'
        "batch\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="input.jsonl"\r\n'
        "Content-Type: application/jsonl\r\n\r\n"
    ).encode()
    payload = prefix + b"x" * (1024 * 1024 + 16) + f"\r\n--{boundary}--\r\n".encode()

    async def stream():
        for start in range(0, len(payload), 65536):
            yield payload[start : start + 65536]

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer local-dev-token"},
        ) as client:
            response = await client.post(
                "/openai/v1/files",
                content=stream(),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


async def test_cancelled_batch_recovery_does_not_publish_empty_result_files(rig):
    runtime, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    await engine.cancel_batch(principal, batch["id"])

    original_delete = runtime.blobs.delete
    failed = False

    async def crash_before_removing_empty_output(name):
        nonlocal failed
        if not failed and name.endswith("/output.jsonl"):
            failed = True
            raise RuntimeError("crash after empty output commit")
        await original_delete(name)

    runtime.blobs.delete = crash_before_removing_empty_output
    with pytest.raises(RuntimeError, match="crash after empty output commit"):
        await engine.handle_control({"kind": "finalize", "batch_id": batch["id"]})

    runtime.blobs.delete = original_delete
    runtime.clock.value += runtime.settings.control_lease_seconds + 1
    await engine.recover()
    state = await engine.get_batch(principal, batch["id"])
    assert state["status"] == "cancelled"
    assert state["output_file_id"] is None
    assert state["error_file_id"] is None


async def test_unknown_signing_kids_do_not_force_unbounded_jwks_fetches():
    private, jwk = keypair("trusted")
    calls = 0

    def handle(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"keys": [jwk]}, headers={"cache-control": "max-age=300"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        auth = EntraAuthenticator(TENANT, AUDIENCE, http_client=client)
        await auth.authenticate(token(private, "trusted"))
        for index in range(20):
            with pytest.raises(AuthError):
                await auth.authenticate(token(private, f"unknown-{index}"))

    assert calls <= 3


async def test_simultaneous_unknown_signing_kids_share_one_forced_refresh():
    private, jwk = keypair("trusted")
    calls = 0

    async def handle(_request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"keys": [jwk]}, headers={"cache-control": "max-age=300"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        auth = EntraAuthenticator(TENANT, AUDIENCE, http_client=client)
        await auth.authenticate(token(private, "trusted"))
        attempts = await asyncio.gather(
            *(auth.authenticate(token(private, f"unknown-{index}")) for index in range(20)),
            return_exceptions=True,
        )

    assert all(isinstance(result, AuthError) for result in attempts)
    assert calls <= 3
