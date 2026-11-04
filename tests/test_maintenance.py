import asyncio

import pytest
from test_engine import drain, row, submit, upload
from test_engine import rig as rig

from ptu_batch.errors import BatchError


async def test_paused_admission_blocks_new_jobs_but_existing_work_can_finish(rig):
    rt, e, p = rig
    file, batch = await submit(e, p, [row()])
    await e.set_admission(paused=True)
    assert (await e.maintenance_status())["safe"] is False
    with pytest.raises(BatchError) as err:
        await e.create_batch(
            p,
            {
                "input_file_id": file["id"],
                "endpoint": "/chat/completions",
                "completion_window": "24h",
            },
        )
    assert err.value.status == 503
    await drain(rt, e)
    status = await e.maintenance_status()
    assert status["safe"] is True
    assert status["active_submissions"] == 0 and status["active_batches"] == 0
    await e.set_admission(paused=False)
    again = await e.create_batch(
        p,
        {"input_file_id": file["id"], "endpoint": "/chat/completions", "completion_window": "24h"},
    )
    assert again["status"] == "validating"


async def test_azure_admission_starts_paused_until_explicit_resume(rig):
    rt, e, p = rig
    rt.settings.mode = "azure"
    file = await upload(e, p, [row()])
    with pytest.raises(BatchError) as err:
        await e.create_batch(
            p,
            {
                "input_file_id": file["id"],
                "endpoint": "/chat/completions",
                "completion_window": "24h",
            },
        )
    assert err.value.status == 503
    assert (await e.maintenance_status())["paused"] is True
    await e.set_admission(paused=False)
    batch = await e.create_batch(
        p,
        {"input_file_id": file["id"], "endpoint": "/chat/completions", "completion_window": "24h"},
    )
    assert batch["status"] == "validating"


async def test_submission_in_flight_is_counted_during_maintenance_fence(rig):
    rt, e, p = rig
    file = await upload(e, p, [row()])
    entered, release = asyncio.Event(), asyncio.Event()
    original = rt.store.apply

    async def hold_pin(pk, mutations):
        if any(m.op == "create" and m.document.get("kind") == "pin" for m in mutations):
            entered.set()
            await release.wait()
        await original(pk, mutations)

    rt.store.apply = hold_pin
    task = asyncio.create_task(
        e.create_batch(
            p,
            {
                "input_file_id": file["id"],
                "endpoint": "/chat/completions",
                "completion_window": "24h",
            },
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    await e.set_admission(paused=True)
    status = await e.maintenance_status()
    assert status["active_submissions"] == 1 and status["safe"] is False
    release.set()
    await task
    rt.store.apply = original
    await drain(rt, e)
    assert (await e.maintenance_status())["safe"] is True
