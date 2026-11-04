import json

import httpx
import httpx2
import pytest
from openai import AsyncOpenAI, AuthenticationError, BadRequestError
from test_engine import drain, row
from test_engine import rig as rig

from ptu_batch.api import create_app


async def test_official_sdk_upload_submit_poll_download_and_delete(rig):
    runtime, engine, _ = rig
    app = create_app(runtime)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as transport:
            async with AsyncOpenAI(
                base_url="http://test/openai/v1/",
                api_key="local-dev-token",
                http_client=transport,
                max_retries=0,
            ) as client:
                uploaded = await client.files.create(
                    file=("batch.jsonl", json.dumps(row()).encode()),
                    purpose="batch",
                    extra_body={"expires_after": {"seconds": 1209600, "anchor": "created_at"}},
                )
                assert uploaded.status == "processed"
                assert uploaded.expires_at == runtime.now() + 1209600
                batch = await client.batches.create(
                    input_file_id=uploaded.id,
                    endpoint="/chat/completions",
                    completion_window="24h",
                    metadata={"application": "sdk-test"},
                )
                assert batch.status == "validating"
                await drain(runtime, engine)
                final = await client.batches.retrieve(batch.id)
                assert final.status == "completed"
                assert final.metadata == {"application": "sdk-test"}
                output = await client.files.content(final.output_file_id)
                parsed = json.loads(output.text)
                assert parsed["custom_id"] == "a"
                assert parsed["response"]["body"]["object"] == "chat.completion"
                files = await client.files.list()
                assert uploaded.id in [file.id for file in files.data]
                batches = await client.batches.list(limit=1)
                assert batches.data[0].id == batch.id
                deleted = await client.files.delete(uploaded.id)
                assert deleted.deleted is True


async def test_sdk_receives_standard_errors_for_bad_auth_and_parameters(rig):
    rt, _, _ = rig
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as transport:
            client = AsyncOpenAI(
                base_url="http://test/openai/v1/",
                api_key="wrong",
                http_client=transport,
                max_retries=0,
            )
            with pytest.raises(AuthenticationError):
                await client.files.list()
            client.api_key = "local-dev-token"
            with pytest.raises(BadRequestError) as err:
                await client.batches.create(
                    input_file_id="file-missing",
                    endpoint="/chat/completions",
                    completion_window="12h",
                )
            assert err.value.body["param"] == "completion_window"
            await client.close()


async def test_api_does_not_expose_internal_job_state_or_blob_names(rig):
    rt, engine, _ = rig
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer local-dev-token"},
        ) as client:
            file = (
                await client.post(
                    "/openai/v1/files",
                    files={"file": ("x.jsonl", json.dumps(row()))},
                    data={"purpose": "batch"},
                )
            ).json()
            assert "owner" not in file and "blob_name" not in file
            batch = (
                await client.post(
                    "/openai/v1/batches",
                    json={
                        "input_file_id": file["id"],
                        "endpoint": "/chat/completions",
                        "completion_window": "24h",
                    },
                )
            ).json()
            assert "deployment_snapshot" not in batch and "principal" not in batch
            wrong = await client.post("/openai/v1/batches", content="not-json")
            assert wrong.status_code == 400 and "error" in wrong.json()


async def test_sdk_pagination_can_iterate_all_batches(rig):
    rt, e, _ = rig
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as transport:
            client = AsyncOpenAI(
                base_url="http://test/openai/v1/",
                api_key="local-dev-token",
                http_client=transport,
                max_retries=0,
            )
            f = await client.files.create(
                file=("x.jsonl", json.dumps(row()).encode()), purpose="batch"
            )
            ids = []
            for _ in range(3):
                b = await client.batches.create(
                    input_file_id=f.id, endpoint="/chat/completions", completion_window="24h"
                )
                ids.append(b.id)
            page = await client.batches.list(limit=1)
            actual = [b.id async for b in page]
            assert set(actual) == set(ids) and len(actual) == 3
            await client.close()


async def test_batch_json_is_bounded_and_backend_errors_use_sdk_envelope(rig):
    rt, e, _ = rig
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
            headers={"Authorization": "Bearer local-dev-token"},
        ) as client:
            oversized = await client.post("/openai/v1/batches", content=b"x" * 65537)
            assert oversized.status_code == 413 and "error" in oversized.json()
            original = rt.store.scan

            async def unavailable(**kwargs):
                raise RuntimeError("private database connection detail")
                yield

            rt.store.scan = unavailable
            response = await client.get("/openai/v1/batches")
            rt.store.scan = original
            assert response.status_code == 500
            assert response.json()["error"]["code"] == "internal_error"
            assert "private database" not in response.text


async def test_files_pagination_accepts_documented_large_page_sizes(rig):
    rt, _, _ = rig
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer local-dev-token"},
        ) as client:
            result = await client.get("/openai/v1/files?limit=1000")
            assert result.status_code == 200
            assert result.json()["data"] == []
