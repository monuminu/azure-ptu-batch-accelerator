"""Opt-in full Azure Batch SDK smoke; dry-run never requests credentials or creates jobs."""

from __future__ import annotations

import argparse
import io
import json
import time
from pathlib import Path
from urllib.parse import urlparse


def run_smoke(
    client, sample: Path, *, timeout_seconds=900, poll_interval=10, deployment_alias=None
):
    rows = [json.loads(line) for line in sample.read_text().splitlines() if line.strip()]
    if not rows:
        raise ValueError("Smoke input is empty")
    if deployment_alias:
        for row in rows:
            row["body"]["model"] = deployment_alias
    expected = {row["custom_id"] for row in rows}
    data = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
    uploaded = client.files.create(
        file=(sample.name, io.BytesIO(data)),
        purpose="batch",
        extra_body={"expires_after": {"seconds": 1209600, "anchor": "created_at"}},
    )
    batch = client.batches.create(
        input_file_id=uploaded.id,
        endpoint="/chat/completions",
        completion_window="24h",
        metadata={"source": "accelerator-smoke"},
        extra_body={"output_expires_after": {"seconds": 1209600, "anchor": "created_at"}},
    )
    deadline = time.monotonic() + timeout_seconds
    while batch.status not in {"completed", "failed", "cancelled", "expired"}:
        if time.monotonic() >= deadline:
            client.batches.cancel(batch.id)
            raise TimeoutError(
                f"Smoke batch {batch.id} exceeded {timeout_seconds} seconds; cancellation requested"
            )
        time.sleep(poll_interval)
        batch = client.batches.retrieve(batch.id)
    if batch.status != "completed" or batch.request_counts.failed or not batch.output_file_id:
        raise RuntimeError(
            f"Smoke batch {batch.id} ended with {batch.status}; failed={batch.request_counts.failed}"
        )
    output = [
        json.loads(line) for line in client.files.content(batch.output_file_id).text.splitlines()
    ]
    if len(output) != len(rows) or {row["custom_id"] for row in output} != expected:
        raise RuntimeError("Smoke output did not match input custom IDs")
    if any(
        row.get("error") or not 200 <= row.get("response", {}).get("status_code", 0) < 300
        for row in output
    ):
        raise RuntimeError("Smoke output contains failed requests")
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "completed": batch.request_counts.completed,
        "failed": batch.request_counts.failed,
        "output_file_id": batch.output_file_id,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", required=True, help="Accelerator HTTPS URL ending in /openai/v1/"
    )
    parser.add_argument("--app-id", required=True, help="Accelerator Entra app/client ID")
    parser.add_argument("--kind", choices=("chat", "responses"), default="chat")
    parser.add_argument("--deployment-alias", default="ptu")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--create-job", action="store_true", help="Makes billable Azure requests")
    args = parser.parse_args()
    parsed = urlparse(args.base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.path.rstrip("/").endswith("/openai/v1")
    ):
        parser.error("--base-url must be an HTTPS accelerator URL ending in /openai/v1/")
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be positive")
    sample = Path(__file__).resolve().parents[1] / "examples" / f"{args.kind}.jsonl"
    print(f"Prepared {sample.name} for deployment alias {args.deployment_alias}")
    if not args.create_job:
        print(
            "Dry run only. Add --create-job to upload, submit, poll, and verify a billable batch."
        )
        return
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import OpenAI

    with DefaultAzureCredential() as credential:
        token_provider = get_bearer_token_provider(credential, f"api://{args.app_id}/.default")
        with OpenAI(base_url=args.base_url, api_key=token_provider, max_retries=0) as client:
            print(
                json.dumps(
                    run_smoke(
                        client,
                        sample,
                        timeout_seconds=args.timeout_seconds,
                        deployment_alias=args.deployment_alias,
                    ),
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
