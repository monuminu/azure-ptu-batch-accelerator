"""Official synchronous OpenAI SDK against a real loopback Uvicorn server."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from openai import OpenAI


@pytest.fixture
def local_server():
    repo = Path(__file__).resolve().parents[1]
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    env = os.environ.copy()
    env["PTU_BATCH_MODE"] = "local"
    env["PTU_BATCH_LOCAL_AUTORUN"] = "true"
    env["PYTHONPATH"] = str(repo / "src")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "ptu_batch.api:app",
            "--fd",
            str(listener.fileno()),
            "--log-level",
            "warning",
        ],
        cwd=repo,
        env=env,
        pass_fds=(listener.fileno(),),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    listener.close()
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"Local server exited: {process.stderr.read().decode()}")
            try:
                response = httpx.get(base + "/healthz", timeout=0.5)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError("Local server did not become ready in 15 seconds")
        yield base + "/openai/v1/"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()


def sample(custom_id, endpoint):
    body = (
        {"model": "demo", "messages": [{"role": "user", "content": "hello"}]}
        if endpoint == "/chat/completions"
        else {"model": "demo", "input": "hello"}
    )
    return (
        json.dumps(
            {"custom_id": custom_id, "method": "POST", "url": "/v1" + endpoint, "body": body}
        )
        + "\n"
    ).encode()


def wait_for_completion(client, batch_id):
    deadline = time.monotonic() + 15
    last = None
    while time.monotonic() < deadline:
        batch = client.batches.retrieve(batch_id)
        last = batch
        if batch.status in {"completed", "failed", "cancelled", "expired"}:
            return batch
        time.sleep(0.1)
    raise AssertionError(f"Batch {batch_id} did not finish in 15 seconds; last={last}")


def test_official_sync_sdk_chat_and_responses_over_loopback_tcp(local_server):
    with OpenAI(
        base_url=local_server, api_key="local-dev-token", max_retries=0, timeout=5
    ) as client:
        uploaded_ids = []
        batch_ids = []
        for endpoint, expected_object in [
            ("/chat/completions", "chat.completion"),
            ("/responses", "response"),
        ]:
            custom_id = "chat" if endpoint == "/chat/completions" else "responses"
            uploaded = client.files.create(
                file=(custom_id + ".jsonl", sample(custom_id, endpoint)), purpose="batch"
            )
            assert uploaded.status == "processed"
            assert uploaded.bytes == len(sample(custom_id, endpoint))
            uploaded_ids.append(uploaded.id)
            assert client.files.retrieve(uploaded.id).id == uploaded.id

            created = client.batches.create(
                input_file_id=uploaded.id, endpoint=endpoint, completion_window="24h"
            )
            batch_ids.append(created.id)
            finished = wait_for_completion(client, created.id)
            assert finished.status == "completed", finished.errors
            assert finished.request_counts.total == 1
            assert finished.request_counts.completed == 1
            assert finished.output_file_id
            output = json.loads(client.files.content(finished.output_file_id).text)
            assert output["custom_id"] == custom_id
            assert output["response"]["status_code"] == 200
            assert output["response"]["body"]["object"] == expected_object

        listed_files = {file.id for file in client.files.list().data}
        assert set(uploaded_ids) <= listed_files
        listed_batches = {batch.id for batch in client.batches.list().data}
        assert set(batch_ids) <= listed_batches
        for file_id in uploaded_ids:
            assert client.files.delete(file_id).deleted is True


def test_azure_smoke_logic_waits_for_and_verifies_real_sdk_results(local_server):
    import importlib.util

    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "ptu_smoke_script", repo / "scripts" / "smoke_test.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with OpenAI(
        base_url=local_server, api_key="local-dev-token", max_retries=0, timeout=5
    ) as client:
        for kind in ("chat", "responses"):
            result = module.run_smoke(
                client, repo / "examples" / f"{kind}.jsonl", timeout_seconds=15, poll_interval=0.1
            )
            assert result["status"] == "completed"
            assert result["completed"] >= 1 and result["failed"] == 0
            assert result["output_file_id"]
