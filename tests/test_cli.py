import json
from pathlib import Path

import pytest

from ptu_batch.bootstrap import build_runtime
from ptu_batch.cli import deployment_configuration, main, usage_report
from ptu_batch.settings import Settings


def test_preflight_can_build_mapping_from_azd_values_without_cosmos_credentials():
    settings = Settings(mode="azure", deployments={})
    mapping = deployment_configuration(
        settings,
        {
            "PTU_BATCH_PTU_ALIAS": "night-work",
            "PTU_BATCH_PTU_RESOURCE_ENDPOINT": "https://customer.openai.azure.com/",
            "PTU_BATCH_PTU_DEPLOYMENT_NAME": "existing",
            "PTU_BATCH_PTU_DEPLOYMENT_RESOURCE_ID": "/subscriptions/s/resourceGroups/r/providers/Microsoft.CognitiveServices/accounts/customer/deployments/existing",
        },
    )
    assert mapping["night-work"]["deployment_name"] == "existing"
    assert mapping["night-work"]["allowed_apis"] == ["/chat/completions", "/responses"]


def test_usage_report_separates_measured_tokens_from_supplied_infrastructure_cost():
    report = usage_report(
        [
            {
                "status": "completed",
                "request_counts": {"completed": 2, "failed": 1},
                "usage": {
                    "input_tokens": 1000000,
                    "output_tokens": 2000000,
                    "total_tokens": 3000000,
                },
            }
        ],
        input_rate=1.5,
        output_rate=2.0,
        infrastructure_cost=2.0,
    )
    assert report["known_usage"]["total_tokens"] == 3000000
    assert report["estimated_avoided_batch_cost"] == 5.5
    assert report["supplied_infrastructure_cost"] == 2.0
    assert report["estimated_net_savings"] == 3.5


def test_openapi_export_is_offline_and_describes_multipart_and_batch_json(tmp_path, monkeypatch):
    path = tmp_path / "openapi.json"
    monkeypatch.setattr("sys.argv", ["ptu-batch", "export-openapi", "--output", str(path)])
    main()
    schema = json.loads(path.read_text())
    assert schema["paths"]["/openai/v1/files"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]
    assert schema["paths"]["/openai/v1/batches"]["post"]["requestBody"]["content"][
        "application/json"
    ]
    for key in ("FileObject", "BatchObject", "CreateBatchRequest"):
        assert key in schema["components"]["schemas"]


async def test_missing_azure_configuration_fails_closed():
    with pytest.raises(ValueError, match="Azure configuration is incomplete"):
        await build_runtime(
            Settings(
                mode="azure",
                tenant_id="",
                audience="",
                deployments={},
                cosmos_endpoint="",
                servicebus_fqdn="",
                storage_account_url="",
            )
        )


async def test_local_demo_accepts_the_shipped_sdk_sample_aliases():
    runtime = await build_runtime(Settings(mode="local", deployments={}, local_autorun=False))
    try:
        samples = Path(__file__).resolve().parents[1] / "examples"
        for path in samples.glob("*.jsonl"):
            for line in path.read_text().splitlines():
                assert json.loads(line)["body"]["model"] in runtime.settings.deployments
    finally:
        await runtime.close()
