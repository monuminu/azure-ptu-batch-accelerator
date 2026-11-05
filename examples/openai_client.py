"""OpenAI SDK workflow. Set PTU_BATCH_BASE_URL and PTU_BATCH_APP_ID for Azure."""

from __future__ import annotations

import os
import time
from pathlib import Path

from openai import OpenAI


def client_from_environment() -> OpenAI:
    base_url = os.getenv("PTU_BATCH_BASE_URL", "http://127.0.0.1:8000/openai/v1/")
    if base_url.startswith("http://127.0.0.1"):
        token = "local-dev-token"
    else:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        app_id = os.environ["PTU_BATCH_APP_ID"]
        token = get_bearer_token_provider(DefaultAzureCredential(), f"api://{app_id}/.default")
    return OpenAI(base_url=base_url, api_key=token)


def main() -> None:
    client = client_from_environment()
    sample_name = (
        "local-chat.jsonl" if str(client.base_url).startswith("http://127.0.0.1") else "chat.jsonl"
    )
    sample = Path(__file__).with_name(sample_name)
    with sample.open("rb") as handle:
        uploaded = client.files.create(file=handle, purpose="batch")
    batch = client.batches.create(
        input_file_id=uploaded.id,
        endpoint="/chat/completions",
        completion_window="24h",
    )
    print(f"Created {batch.id}; input file {uploaded.id}")
    listed = client.batches.list(limit=10)
    print(f"Visible batches: {len(listed.data)}")

    for _ in range(60):
        batch = client.batches.retrieve(batch.id)
        print(f"{batch.id}: {batch.status}")
        if batch.status in {"completed", "failed", "cancelled", "expired"}:
            break
        time.sleep(2)

    if batch.output_file_id:
        content = client.files.content(batch.output_file_id)
        print(content.text)
    elif batch.status not in {"completed", "failed", "cancelled", "expired"}:
        client.batches.cancel(batch.id)
        print("Cancelled the unfinished example batch.")


if __name__ == "__main__":
    main()
