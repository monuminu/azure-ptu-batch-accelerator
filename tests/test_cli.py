import argparse
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from ptu_batch.bootstrap import build_runtime
from ptu_batch.cli import _preflight, deployment_configuration, main, usage_report
from ptu_batch.settings import Settings

ACCOUNT_ID = (
    "/subscriptions/11111111-2222-4333-8444-555555555555/resourceGroups/rg-model/"
    "providers/Microsoft.CognitiveServices/accounts/customer"
)
ACCOUNT = {
    "resource_endpoint": "https://customer.openai.azure.com/",
    "resource_id": ACCOUNT_ID,
    "allowed_apis": ["/chat/completions", "/responses"],
    "allowed_subjects": [],
}


class PreflightRecorder:
    def __init__(self):
        self.deployments = []
        self.accounts = []

    async def check(self, target, force=False):
        self.deployments.append((target, force))

    async def check_account(self, account, force=False):
        self.accounts.append((account, force))

    async def close(self):
        pass


def preflight_fakes(monkeypatch, *, managed_identity_client_id=""):
    from azure.identity import aio as identity_aio

    from ptu_batch.adapters import preflight as preflight_module

    recorder = PreflightRecorder()
    credential_kwargs = []

    class FakeCredential:
        def __init__(self, **kwargs):
            credential_kwargs.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

    monkeypatch.setattr(identity_aio, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr(preflight_module, "AzurePreflight", lambda credential: recorder)
    if managed_identity_client_id:
        monkeypatch.setenv("PTU_BATCH_MANAGED_IDENTITY_CLIENT_ID", managed_identity_client_id)
    else:
        monkeypatch.delenv("PTU_BATCH_MANAGED_IDENTITY_CLIENT_ID", raising=False)
    return recorder, credential_kwargs


def preflight_args(**overrides):
    values = {"resource_id": None, "endpoint": None, "deployment": None, "alias": "ptu"}
    values.update(overrides)
    return argparse.Namespace(**values)


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


def test_account_only_preflight_reports_scope_without_claiming_ptu_eligibility(monkeypatch, capsys):
    monkeypatch.setenv("PTU_BATCH_DEPLOYMENT_ACCOUNT", json.dumps(ACCOUNT))
    monkeypatch.delenv("PTU_BATCH_DEPLOYMENTS", raising=False)
    recorder, _ = preflight_fakes(monkeypatch)
    asyncio.run(_preflight(preflight_args()))
    assert len(recorder.accounts) == 1
    assert recorder.deployments == []
    assert recorder.accounts[0][0].resource_id == ACCOUNT_ID
    report = json.loads(capsys.readouterr().out)
    assert report["scope"] == "account"
    assert report["ptu_eligibility_checked"] is False
    assert "ptu_only" not in report


def test_named_account_preflight_resolves_literal_name_and_checks_strict_ptu(monkeypatch, capsys):
    monkeypatch.setenv("PTU_BATCH_DEPLOYMENT_ACCOUNT", json.dumps(ACCOUNT))
    monkeypatch.setenv(
        "PTU_BATCH_DEPLOYMENTS",
        json.dumps(
            {
                "old-alias": {
                    **ACCOUNT,
                    "resource_id": ACCOUNT_ID + "/deployments/other",
                    "deployment_name": "other",
                }
            }
        ),
    )
    recorder, _ = preflight_fakes(monkeypatch)
    asyncio.run(_preflight(preflight_args(deployment="actual-model-name")))
    assert recorder.accounts == []
    assert len(recorder.deployments) == 1
    target, force = recorder.deployments[0]
    assert target["deployment_name"] == "actual-model-name"
    assert target["resource_id"] == ACCOUNT_ID + "/deployments/actual-model-name"
    assert force is True
    report = json.loads(capsys.readouterr().out)
    assert report["deployment_name"] == "actual-model-name"
    assert report["ptu_eligibility_checked"] is True


def test_preflight_can_build_account_from_azd_values_without_model_name(monkeypatch, capsys):
    from ptu_batch import cli

    for key in (
        "PTU_BATCH_DEPLOYMENT_ACCOUNT",
        "PTU_BATCH_DEPLOYMENTS",
        "PTU_BATCH_PTU_DEPLOYMENT_RESOURCE_ID",
        "PTU_BATCH_PTU_ACCOUNT_RESOURCE_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    recorder, _ = preflight_fakes(monkeypatch)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps(
                {
                    "PTU_BATCH_PTU_ACCOUNT_RESOURCE_ID": ACCOUNT_ID,
                    "PTU_BATCH_PTU_RESOURCE_ENDPOINT": ACCOUNT["resource_endpoint"],
                }
            ),
            "",
        ),
    )
    asyncio.run(_preflight(preflight_args()))
    assert len(recorder.accounts) == 1
    assert recorder.deployments == []
    assert json.loads(capsys.readouterr().out)["ptu_eligibility_checked"] is False


@pytest.mark.parametrize("name", ["../escape", "a/b", "?model=other", "", " name "])
def test_account_preflight_rejects_nonliteral_deployment_names(monkeypatch, name):
    monkeypatch.setenv("PTU_BATCH_DEPLOYMENT_ACCOUNT", json.dumps(ACCOUNT))
    recorder, _ = preflight_fakes(monkeypatch)
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(_preflight(preflight_args(deployment=name)))
    assert recorder.deployments == []


def test_explicit_deployment_override_wins_over_existing_map_and_account(monkeypatch):
    monkeypatch.setenv("PTU_BATCH_DEPLOYMENT_ACCOUNT", json.dumps(ACCOUNT))
    monkeypatch.setenv("PTU_BATCH_PTU_ACCOUNT_RESOURCE_ID", ACCOUNT_ID)
    monkeypatch.setenv(
        "PTU_BATCH_DEPLOYMENTS",
        json.dumps(
            {
                "old-alias": {
                    **ACCOUNT,
                    "resource_id": ACCOUNT_ID + "/deployments/old",
                    "deployment_name": "old",
                }
            }
        ),
    )
    recorder, _ = preflight_fakes(monkeypatch)
    explicit_id = ACCOUNT_ID + "/deployments/explicit"
    asyncio.run(
        _preflight(
            preflight_args(
                resource_id=explicit_id,
                endpoint=ACCOUNT["resource_endpoint"],
                deployment="explicit",
                alias="explicit-alias",
            )
        )
    )
    assert len(recorder.deployments) == 1
    assert recorder.deployments[0][0]["resource_id"] == explicit_id
    assert recorder.accounts == []


def test_explicit_override_ignores_stale_invalid_env_configuration(monkeypatch):
    monkeypatch.setenv("PTU_BATCH_DEPLOYMENT_ACCOUNT", "stale-invalid-json")
    monkeypatch.setenv("PTU_BATCH_DEPLOYMENTS", "stale-invalid-json")
    recorder, _ = preflight_fakes(monkeypatch)
    explicit_id = ACCOUNT_ID + "/deployments/explicit"
    asyncio.run(
        _preflight(
            preflight_args(
                resource_id=explicit_id,
                endpoint=ACCOUNT["resource_endpoint"],
                deployment="explicit",
            )
        )
    )
    assert recorder.deployments[0][0]["resource_id"] == explicit_id


def test_explicit_account_override_supports_named_deployment(monkeypatch):
    monkeypatch.setenv("PTU_BATCH_DEPLOYMENTS", "stale-invalid-json")
    recorder, _ = preflight_fakes(monkeypatch)
    asyncio.run(
        _preflight(
            preflight_args(
                resource_id=ACCOUNT_ID,
                endpoint=ACCOUNT["resource_endpoint"],
                deployment="requested-model",
            )
        )
    )
    assert recorder.deployments[0][0]["resource_id"] == ACCOUNT_ID + "/deployments/requested-model"


def test_preflight_selects_configured_managed_identity(monkeypatch):
    monkeypatch.setenv("PTU_BATCH_DEPLOYMENT_ACCOUNT", json.dumps(ACCOUNT))
    _, credential_kwargs = preflight_fakes(
        monkeypatch, managed_identity_client_id="00000000-1111-4222-8333-444444444444"
    )
    asyncio.run(_preflight(preflight_args()))
    assert credential_kwargs == [
        {"managed_identity_client_id": "00000000-1111-4222-8333-444444444444"}
    ]


def test_azd_hook_uses_account_without_requiring_deployment_name(monkeypatch):
    from scripts import preflight_from_azd

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["azd", "env", "get-values"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "PTU_BATCH_PTU_ACCOUNT_RESOURCE_ID": ACCOUNT_ID,
                        "PTU_BATCH_PTU_RESOURCE_ENDPOINT": ACCOUNT["resource_endpoint"],
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(preflight_from_azd.subprocess, "run", fake_run)
    monkeypatch.setenv("PTU_BATCH_DEPLOYMENTS", "stale-alias-map")
    preflight_from_azd.main()
    command, kwargs = calls[-1]
    assert command[-2:] == ["ptu-batch", "preflight"]
    assert "PTU_BATCH_DEPLOYMENTS" not in kwargs["env"]
    account = json.loads(kwargs["env"]["PTU_BATCH_DEPLOYMENT_ACCOUNT"])
    assert account["resource_id"] == ACCOUNT_ID
    assert account["resource_endpoint"] == ACCOUNT["resource_endpoint"]


def test_azd_hook_preserves_legacy_deployment_configuration(monkeypatch):
    from scripts import preflight_from_azd

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["azd", "env", "get-values"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "PTU_BATCH_PTU_RESOURCE_ENDPOINT": ACCOUNT["resource_endpoint"],
                        "PTU_BATCH_PTU_DEPLOYMENT_RESOURCE_ID": ACCOUNT_ID + "/deployments/old",
                        "PTU_BATCH_PTU_DEPLOYMENT_NAME": "old",
                        "PTU_BATCH_PTU_ALIAS": "old-alias",
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(preflight_from_azd.subprocess, "run", fake_run)
    monkeypatch.setenv("PTU_BATCH_DEPLOYMENT_ACCOUNT", "stale-account")
    preflight_from_azd.main()
    env = calls[-1][1]["env"]
    assert "PTU_BATCH_DEPLOYMENT_ACCOUNT" not in env
    assert json.loads(env["PTU_BATCH_DEPLOYMENTS"])["old-alias"]["deployment_name"] == "old"


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
        for path in samples.glob("local-*.jsonl"):
            for line in path.read_text().splitlines():
                assert json.loads(line)["body"]["model"] in runtime.settings.deployments
    finally:
        await runtime.close()
