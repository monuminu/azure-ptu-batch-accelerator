"""Deployment driver contract with all external processes replaced by fakes."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from scripts import terraform_deploy

SUB = "11111111-2222-4333-8444-555555555555"
TENANT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
DEPLOYMENT_ID = (
    f"/subscriptions/{SUB}/resourceGroups/rg-model/providers/"
    "Microsoft.CognitiveServices/accounts/aoai-test/deployments/model1"
)
ACCOUNT_ID = DEPLOYMENT_ID.rsplit("/deployments/", 1)[0]
ACCOUNT_ENDPOINT = "https://aoai-test.cognitiveservices.azure.com/"


def variables(tmp_path: Path, *, validation_only=False) -> Path:
    path = tmp_path / "private-variables.json"
    path.write_text(
        json.dumps(
            {
                "subscription_id": SUB,
                "tenant_id": TENANT,
                "environment_name": "ptubatchtf",
                "ptu_deployment_resource_id": DEPLOYMENT_ID,
                "ptu_deployment_name": "model1",
                "ptu_resource_endpoint": "https://aoai-test.openai.azure.com/",
                "validation_only": validation_only,
            }
        )
    )
    return path


def account_variables(tmp_path: Path, *, validation_only=False) -> Path:
    path = tmp_path / "private-account-variables.json"
    path.write_text(
        json.dumps(
            {
                "subscription_id": SUB,
                "tenant_id": TENANT,
                "environment_name": "ptubatchtf",
                "openai_account_resource_id": ACCOUNT_ID,
                "openai_resource_endpoint": ACCOUNT_ENDPOINT,
                "validation_only": validation_only,
            }
        )
    )
    return path


class FakeRunner:
    def __init__(
        self,
        *,
        sku="ProvisionedManaged",
        actions=None,
        resource_type="azurerm_resource_group",
        fail_guard=False,
        remote_output_error=None,
        address=None,
        account_id=ACCOUNT_ID,
        account_name="aoai-test",
        account_state="Succeeded",
        account_endpoint=ACCOUNT_ENDPOINT,
    ):
        self.calls = []
        self.sku = sku
        self.actions = actions or ["create"]
        self.resource_type = resource_type
        self.fail_guard = fail_guard
        self.remote_output_error = remote_output_error
        self.address = address or ""
        self.account_id = account_id
        self.account_name = account_name
        self.account_state = account_state
        self.account_endpoint = account_endpoint

    def __call__(self, cmd, *, cwd=None, env=None, log_file=None):
        self.calls.append((list(cmd), cwd, dict(env or {}), log_file))
        if cmd[:2] == ["az", "rest"]:
            if "/deployments/" in " ".join(cmd):
                data = {
                    "sku": {"name": self.sku, "capacity": 10},
                    "properties": {
                        "provisioningState": "Succeeded",
                        "spilloverDeploymentName": None,
                    },
                }
            else:
                data = {
                    "id": self.account_id,
                    "name": self.account_name,
                    "properties": {
                        "provisioningState": self.account_state,
                        "endpoint": self.account_endpoint,
                    },
                }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")
        if cmd[:3] == ["terraform", "show", "-json"]:
            change = {"actions": self.actions}
            if self.resource_type == "azapi_resource":
                change["after"] = {
                    "type": "Microsoft.CognitiveServices/accounts/deployments@2025-06-01"
                }
            data = {
                "resource_changes": [
                    {"type": self.resource_type, "address": self.address, "change": change}
                ]
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")
        if self.fail_guard and "terraform_guard.py" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 1, "", "unsafe")
        if cmd[:2] == ["terraform", "plan"]:
            output = next(part.split("=", 1)[1] for part in cmd if part.startswith("-out="))
            Path(output).write_bytes(b"reviewable-fake-plan")
        if cmd[:4] == ["terraform", "output", "-json", "servicebus_namespace"]:
            if self.remote_output_error:
                return subprocess.CompletedProcess(cmd, 1, "", self.remote_output_error)
            return subprocess.CompletedProcess(cmd, 0, json.dumps("sb-remote"), "")
        if cmd[:3] == ["terraform", "output", "-json"]:
            data = {
                "api_endpoint_url": {"sensitive": False, "value": "https://api.example"},
                "servicebus_namespace": {"sensitive": False, "value": "sb-test"},
                "key": {"sensitive": True, "value": "secret-value"},
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_plan_only_prechecks_and_never_runs_guard_or_apply(tmp_path, capsys):
    runner = FakeRunner()
    state = tmp_path / "private" / "state"
    plan = terraform_deploy.deploy(variables(tmp_path), state, execute=runner)
    commands = [c[0] for c in runner.calls]
    assert plan == state / "accelerator.tfplan"
    assert commands[0][:2] == ["az", "rest"]
    assert "--subscription" in commands[0]
    assert f"https://management.azure.com{DEPLOYMENT_ID}?api-version=2025-06-01" in commands[0]
    assert [c[:2] for c in commands if c[0] == "terraform"] == [
        ["terraform", "init"],
        ["terraform", "fmt"],
        ["terraform", "validate"],
        ["terraform", "plan"],
        ["terraform", "show"],
    ]
    assert not any("terraform_guard.py" in " ".join(c) for c in commands)
    assert not any(c[:2] == ["terraform", "apply"] for c in commands)
    assert all("account set" not in " ".join(c) for c in commands)
    assert all(
        c[2].get("ARM_SUBSCRIPTION_ID") == SUB and c[2].get("ARM_TENANT_ID") == TENANT
        for c in runner.calls
    )
    assert "secret-value" not in capsys.readouterr().out


def test_account_only_plan_checks_account_without_deployment_name_or_model_call(tmp_path):
    runner = FakeRunner()
    plan = terraform_deploy.deploy(account_variables(tmp_path), tmp_path / "state", execute=runner)
    commands = [call[0] for call in runner.calls]
    assert plan.is_file()
    assert commands[0][:2] == ["az", "rest"]
    assert f"https://management.azure.com{ACCOUNT_ID}?api-version=2025-06-01" in commands[0]
    assert "--subscription" in commands[0]
    assert not any("/deployments/" in " ".join(command) for command in commands)
    assert not any(command[:2] == ["terraform", "apply"] for command in commands)


@pytest.mark.parametrize(
    "field,value",
    [
        ("openai_account_resource_id", "not-an-account-id"),
        ("openai_account_resource_id", DEPLOYMENT_ID),
        ("openai_account_resource_id", ACCOUNT_ID.replace(SUB, TENANT)),
        ("openai_resource_endpoint", "http://aoai-test.cognitiveservices.azure.com/"),
        ("openai_resource_endpoint", "https://aoai-test.cognitiveservices.azure.com/path"),
        ("openai_resource_endpoint", "https://user@aoai-test.cognitiveservices.azure.com/"),
        ("openai_resource_endpoint", "https://aoai-test.cognitiveservices.azure.com/?key=x"),
    ],
)
def test_account_only_rejects_malformed_scope_or_origin_before_commands(tmp_path, field, value):
    path = account_variables(tmp_path)
    values = json.loads(path.read_text())
    values[field] = value
    path.write_text(json.dumps(values))
    runner = FakeRunner()
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(path, tmp_path / "state", execute=runner)
    assert runner.calls == []


@pytest.mark.parametrize(
    "extra", ["ptu_deployment_resource_id", "ptu_deployment_name", "ptu_resource_endpoint"]
)
def test_account_only_rejects_legacy_deployment_fields(tmp_path, extra):
    path = account_variables(tmp_path)
    values = json.loads(path.read_text())
    values[extra] = {
        "ptu_deployment_resource_id": DEPLOYMENT_ID,
        "ptu_deployment_name": "model1",
        "ptu_resource_endpoint": "https://aoai-test.openai.azure.com/",
    }[extra]
    path.write_text(json.dumps(values))
    runner = FakeRunner()
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(path, tmp_path / "state", execute=runner)
    assert runner.calls == []


@pytest.mark.parametrize(
    "account_kwargs",
    [
        {"account_id": ACCOUNT_ID.replace("aoai-test", "other-account")},
        {"account_name": "different"},
        {"account_state": "Failed"},
        {"account_endpoint": "https://other.cognitiveservices.azure.com/"},
    ],
)
def test_account_only_arm_get_fails_closed_on_name_state_or_origin(tmp_path, account_kwargs):
    runner = FakeRunner(**account_kwargs)
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(account_variables(tmp_path), tmp_path / "state", execute=runner)
    assert [call[0][:2] for call in runner.calls] == [["az", "rest"]]


def test_account_only_rejects_validation_only_mode(tmp_path):
    runner = FakeRunner()
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(
            account_variables(tmp_path, validation_only=True),
            tmp_path / "state",
            validation_only=True,
            execute=runner,
        )
    assert runner.calls == []


def test_plan_inspection_allows_only_read_only_cognitive_data_sources():
    allowed = {
        "resource_changes": [
            {
                "mode": "data",
                "type": "azapi_resource",
                "address": "data.azapi_resource.existing_account[0]",
                "change": {
                    "actions": ["read"],
                    "after": {"type": "Microsoft.CognitiveServices/accounts@2025-06-01"},
                },
            },
            {
                "mode": "data",
                "type": "azapi_resource",
                "address": "data.azapi_resource.existing_deployment[0]",
                "change": {
                    "actions": ["read"],
                    "after": {
                        "type": "Microsoft.CognitiveServices/accounts/deployments@2025-06-01"
                    },
                },
            },
        ]
    }
    assert terraform_deploy._plan_counts(json.dumps(allowed)) == (
        {"create": 0, "update": 0, "delete": 0},
        [],
    )
    # Moving from an uncounted legacy data source to account-only count=0 can
    # remove only Terraform's cached data-source state, never the Azure target.
    allowed["resource_changes"][1]["change"]["actions"] = ["delete"]
    assert terraform_deploy._plan_counts(json.dumps(allowed)) == (
        {"create": 0, "update": 0, "delete": 0},
        [],
    )
    allowed["resource_changes"][0]["mode"] = "managed"
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy._plan_counts(json.dumps(allowed))
    managed_account = {
        "resource_changes": [
            {
                "mode": "managed",
                "type": "azapi_resource",
                "address": "azapi_resource.unexpected_account",
                "change": {
                    "actions": ["update"],
                    "after": {"type": "Microsoft.CognitiveServices/accounts@2025-06-01"},
                },
            }
        ]
    }
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy._plan_counts(json.dumps(managed_account))


def test_apply_runs_fresh_guard_then_saved_plan_and_saves_public_outputs(tmp_path, capsys):
    runner = FakeRunner()
    state = tmp_path / "private-state"
    state.mkdir()
    # A partial prior apply can have the owned namespace without an output.
    (state / "terraform.tfstate").write_text(
        json.dumps(
            {
                "resources": [
                    {
                        "mode": "managed",
                        "type": "azurerm_servicebus_namespace",
                        "name": "main",
                        "instances": [{"attributes": {"name": "sb-existing"}}],
                    }
                ]
            }
        )
    )
    terraform_deploy.deploy(variables(tmp_path), state, apply=True, execute=runner)
    commands = [c[0] for c in runner.calls]
    guard_index = next(i for i, c in enumerate(commands) if "terraform_guard.py" in " ".join(c))
    apply_index = next(i for i, c in enumerate(commands) if c[:2] == ["terraform", "apply"])
    assert guard_index < apply_index
    assert commands[apply_index][-1] == str(state / "accelerator.tfplan")
    assert "-auto-approve" in commands[apply_index]
    guard_env = runner.calls[guard_index][2]
    assert guard_env["PTU_TF_SERVICEBUS_NAMESPACE"] == "sb-existing"
    assert guard_env["PTU_TF_API_NAME"] == "ca-ptubatchtf-ptu-api"
    assert guard_env["PTU_TF_SUBSCRIPTION_ID"] == SUB
    assert json.loads((state / "outputs.json").read_text()) == {
        "api_endpoint_url": "https://api.example",
        "servicebus_namespace": "sb-test",
    }
    assert "secret-value" not in capsys.readouterr().out
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "outputs.json").stat().st_mode & 0o777 == 0o600
    assert (state / "terraform.tfstate").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("validation_flag,variable_value", [(False, True), (True, False)])
def test_validation_only_flag_must_match_private_variables(
    tmp_path, validation_flag, variable_value
):
    runner = FakeRunner(sku="Standard")
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(
            variables(tmp_path, validation_only=variable_value),
            tmp_path / "state",
            validation_only=validation_flag,
            execute=runner,
        )
    assert runner.calls == []


def test_standard_deployment_blocks_without_explicit_validation_only(tmp_path):
    runner = FakeRunner(sku="Standard")
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(variables(tmp_path), tmp_path / "state", execute=runner)
    assert [c[0][:2] for c in runner.calls] == [["az", "rest"]]


def test_validation_only_standard_reference_emits_warning_and_plans(tmp_path, capsys):
    runner = FakeRunner(sku="Standard")
    terraform_deploy.deploy(
        variables(tmp_path, validation_only=True),
        tmp_path / "state",
        validation_only=True,
        execute=runner,
    )
    assert "VALIDATION ONLY" in capsys.readouterr().out
    assert not any(c[0][:2] == ["terraform", "apply"] for c in runner.calls)


def test_apply_blocks_cloud_deletion_without_explicit_owned_replace(tmp_path):
    runner = FakeRunner(actions=["delete", "create"])
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(variables(tmp_path), tmp_path / "state", apply=True, execute=runner)
    assert not any(c[0][:2] == ["terraform", "apply"] for c in runner.calls)


def test_guard_failure_stops_saved_plan_apply(tmp_path):
    runner = FakeRunner(fail_guard=True)
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(variables(tmp_path), tmp_path / "state", apply=True, execute=runner)
    assert any("terraform_guard.py" in " ".join(c[0]) for c in runner.calls)
    assert not any(c[0][:2] == ["terraform", "apply"] for c in runner.calls)


@pytest.mark.parametrize("resource_type", ["azurerm_cognitive_deployment", "azapi_resource"])
def test_driver_never_applies_a_model_deployment_resource_even_with_replace_flag(
    tmp_path, resource_type
):
    runner = FakeRunner(resource_type=resource_type)
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(
            variables(tmp_path),
            tmp_path / "state",
            apply=True,
            allow_replace_owned=True,
            execute=runner,
        )
    assert not any(c[0][:2] == ["terraform", "apply"] for c in runner.calls)


def test_private_state_directory_cannot_be_repository_tree(tmp_path):
    runner = FakeRunner()
    repo_state = Path(terraform_deploy.__file__).resolve().parents[1] / "state"
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(variables(tmp_path), repo_state, execute=runner)
    assert runner.calls == []


def test_variable_file_cannot_be_inside_published_repository(tmp_path):
    runner = FakeRunner()
    with tempfile.TemporaryDirectory(prefix="tf-vars-test-", dir=terraform_deploy.ROOT) as folder:
        inside = Path(folder) / "variables.json"
        inside.write_text(variables(tmp_path).read_text())
        with pytest.raises(terraform_deploy.DeploymentError):
            terraform_deploy.deploy(inside, tmp_path / "private-state", execute=runner)
    assert runner.calls == []


def test_local_plan_saves_private_manifest_and_readonly_lockfile(tmp_path):
    runner = FakeRunner()
    state = tmp_path / "state"
    plan = terraform_deploy.deploy(variables(tmp_path), state, execute=runner)
    init = next(c[0] for c in runner.calls if c[0][:2] == ["terraform", "init"])
    assert "-lockfile=readonly" in init
    assert f"-backend-config=path={state / 'terraform.tfstate'}" in init
    manifest = Path(str(plan) + ".manifest.json")
    assert manifest.is_file()
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert plan.stat().st_mode & 0o777 == 0o600
    details = json.loads(manifest.read_text())
    assert details["plan_sha256"] and details["variables_sha256"] and details["source_sha256"]


def test_remote_backend_requires_explicit_override_and_omits_local_path(tmp_path, monkeypatch):
    runner = FakeRunner()
    module = tmp_path / "module"
    module.mkdir()
    monkeypatch.setattr(terraform_deploy, "MODULE", module)
    backend = tmp_path / "private-backend.hcl"
    backend.write_text('resource_group_name = "rg-state"\n')
    backend.chmod(0o600)
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(
            variables(tmp_path), tmp_path / "state", backend_config=backend, execute=runner
        )
    assert runner.calls == []
    (module / "backend_override.tf").write_text('terraform { backend "azurerm" {} }\n')
    terraform_deploy.deploy(
        variables(tmp_path), tmp_path / "state", backend_config=backend, execute=runner
    )
    init = next(c[0] for c in runner.calls if c[0][:2] == ["terraform", "init"])
    assert f"-backend-config={backend}" in init
    assert not any(part.startswith("-backend-config=path=") for part in init)
    assert "-lockfile=readonly" in init


def test_remote_apply_reads_namespace_from_terraform_output(tmp_path, monkeypatch):
    runner = FakeRunner()
    module = tmp_path / "module"
    module.mkdir()
    (module / "backend_override.tf").write_text('terraform { backend "azurerm" {} }\n')
    monkeypatch.setattr(terraform_deploy, "MODULE", module)
    backend = tmp_path / "backend.hcl"
    backend.write_text('resource_group_name = "rg-state"\n')
    backend.chmod(0o600)
    terraform_deploy.deploy(
        variables(tmp_path), tmp_path / "state", apply=True, backend_config=backend, execute=runner
    )
    commands = [c[0] for c in runner.calls]
    namespace_output = next(
        i
        for i, c in enumerate(commands)
        if c[:4] == ["terraform", "output", "-json", "servicebus_namespace"]
    )
    guard = next(i for i, c in enumerate(commands) if "terraform_guard.py" in " ".join(c))
    assert namespace_output < guard
    assert runner.calls[guard][2]["PTU_TF_SERVICEBUS_NAMESPACE"] == "sb-remote"


def test_remote_output_authorization_error_does_not_bootstrap(tmp_path, monkeypatch):
    runner = FakeRunner(remote_output_error="Error: AuthorizationFailed reading backend")
    module = tmp_path / "module"
    module.mkdir()
    (module / "backend_override.tf").write_text('terraform { backend "azurerm" {} }\n')
    monkeypatch.setattr(terraform_deploy, "MODULE", module)
    backend = tmp_path / "backend.hcl"
    backend.write_text('resource_group_name = "rg-state"\n')
    backend.chmod(0o600)
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(
            variables(tmp_path),
            tmp_path / "state",
            apply=True,
            backend_config=backend,
            execute=runner,
        )
    assert not any("terraform_guard.py" in " ".join(c[0]) for c in runner.calls)


def test_remote_first_provision_uses_placeholder_only_for_missing_output(tmp_path, monkeypatch):
    runner = FakeRunner(remote_output_error='Error: Output "servicebus_namespace" not found')
    module = tmp_path / "module"
    module.mkdir()
    (module / "backend_override.tf").write_text('terraform { backend "azurerm" {} }\n')
    monkeypatch.setattr(terraform_deploy, "MODULE", module)
    backend = tmp_path / "backend.hcl"
    backend.write_text('resource_group_name = "rg-state"\n')
    backend.chmod(0o600)
    terraform_deploy.deploy(
        variables(tmp_path), tmp_path / "state", apply=True, backend_config=backend, execute=runner
    )
    guard = next(c for c in runner.calls if "terraform_guard.py" in " ".join(c[0]))
    assert guard[2]["PTU_TF_SERVICEBUS_NAMESPACE"] == "sb-bootstrap-placeholder"


def test_remote_backend_config_must_be_private(tmp_path, monkeypatch):
    runner = FakeRunner()
    module = tmp_path / "module"
    module.mkdir()
    (module / "backend_override.tf").write_text('terraform { backend "azurerm" {} }\n')
    monkeypatch.setattr(terraform_deploy, "MODULE", module)
    backend = tmp_path / "backend.hcl"
    backend.write_text('resource_group_name = "rg-state"\n')
    backend.chmod(0o644)
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(
            variables(tmp_path), tmp_path / "state", backend_config=backend, execute=runner
        )
    assert runner.calls == []


def test_reviewed_saved_plan_applies_without_replanning(tmp_path):
    state = tmp_path / "state"
    variable_file = variables(tmp_path)
    plan = terraform_deploy.deploy(variable_file, state, execute=FakeRunner())
    runner = FakeRunner()
    terraform_deploy.deploy(variable_file, state, apply_plan=plan, execute=runner)
    commands = [c[0] for c in runner.calls]
    assert not any(c[:2] == ["terraform", "plan"] for c in commands)
    show = next(i for i, c in enumerate(commands) if c[:3] == ["terraform", "show", "-json"])
    guard = next(i for i, c in enumerate(commands) if "terraform_guard.py" in " ".join(c))
    apply = next(i for i, c in enumerate(commands) if c[:2] == ["terraform", "apply"])
    assert show < guard < apply
    assert commands[apply][-1] == str(plan)


def test_second_plan_keeps_first_reviewed_artifact_intact(tmp_path):
    state = tmp_path / "state"
    variable_file = variables(tmp_path)
    first = terraform_deploy.deploy(variable_file, state, execute=FakeRunner())
    original_plan = first.read_bytes()
    original_manifest = Path(str(first) + ".manifest.json").read_bytes()
    second = terraform_deploy.deploy(variable_file, state, execute=FakeRunner())
    assert second != first
    assert second.is_file()
    assert first.read_bytes() == original_plan
    assert Path(str(first) + ".manifest.json").read_bytes() == original_manifest


@pytest.mark.parametrize("tamper", ["variables", "plan"])
def test_reviewed_plan_rejects_provenance_change(tmp_path, tamper):
    state = tmp_path / "state"
    variable_file = variables(tmp_path)
    plan = terraform_deploy.deploy(variable_file, state, execute=FakeRunner())
    if tamper == "variables":
        data = json.loads(variable_file.read_text())
        data["location"] = "westus"
        variable_file.write_text(json.dumps(data))
    else:
        plan.write_bytes(b"tampered-plan")
    runner = FakeRunner()
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(variable_file, state, apply_plan=plan, execute=runner)
    assert not any(c[0][:2] == ["terraform", "apply"] for c in runner.calls)


def test_reviewed_plan_rejects_source_change(tmp_path, monkeypatch):
    module = tmp_path / "module"
    module.mkdir()
    source = module / "main.tf"
    source.write_text('terraform { required_version = ">= 1.15" }\n')
    monkeypatch.setattr(terraform_deploy, "MODULE", module)
    variable_file = variables(tmp_path)
    plan = terraform_deploy.deploy(variable_file, tmp_path / "state", execute=FakeRunner())
    source.write_text('terraform { required_version = ">= 1.16" }\n')
    runner = FakeRunner()
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(variable_file, tmp_path / "state", apply_plan=plan, execute=runner)
    assert not any(c[0][:2] == ["terraform", "apply"] for c in runner.calls)


def test_source_fingerprint_ignores_python_cache_but_tracks_source_and_dockerignore(
    tmp_path, monkeypatch
):
    root = tmp_path / "source"
    module = root / "infra" / "terraform"
    package = root / "src" / "package"
    package.mkdir(parents=True)
    module.mkdir(parents=True)
    source = package / "worker.py"
    source.write_text("VALUE = 1\n")
    ignore = root / ".dockerignore"
    ignore.write_text("tests\n")
    (module / "main.tf").write_text('terraform { required_version = ">= 1.15" }\n')
    monkeypatch.setattr(terraform_deploy, "ROOT", root)
    monkeypatch.setattr(terraform_deploy, "MODULE", module)

    original = terraform_deploy._source_sha256()
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "worker.cpython-312.pyc").write_bytes(b"first bytecode")
    (package / "worker.pyo").write_bytes(b"optimized bytecode")
    assert terraform_deploy._source_sha256() == original
    (cache / "worker.cpython-312.pyc").write_bytes(b"different bytecode")
    assert terraform_deploy._source_sha256() == original

    source.write_text("VALUE = 2\n")
    changed_source = terraform_deploy._source_sha256()
    assert changed_source != original
    ignore.write_text("tests\nexamples\n")
    assert terraform_deploy._source_sha256() != changed_source


@pytest.mark.parametrize(
    "resource_type",
    [
        "azurerm_resource_group",
        "azurerm_cosmosdb_account",
        "azurerm_storage_account",
        "azurerm_servicebus_namespace",
    ],
)
def test_allow_replace_owned_still_blocks_data_bearing_deletes(tmp_path, resource_type):
    runner = FakeRunner(actions=["delete", "create"], resource_type=resource_type)
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(
            variables(tmp_path),
            tmp_path / "state",
            apply=True,
            allow_replace_owned=True,
            execute=runner,
        )
    assert not any(c[0][:2] == ["terraform", "apply"] for c in runner.calls)


def test_allow_replace_owned_permits_only_stateless_environment_replacement(tmp_path):
    runner = FakeRunner(
        actions=["delete", "create"],
        resource_type="azurerm_container_app_environment",
        address="azurerm_container_app_environment.main",
    )
    terraform_deploy.deploy(
        variables(tmp_path),
        tmp_path / "state",
        apply=True,
        allow_replace_owned=True,
        execute=runner,
    )
    assert any(c[0][:2] == ["terraform", "apply"] for c in runner.calls)


def test_allow_replace_owned_requires_matching_stateless_type_and_address(tmp_path):
    runner = FakeRunner(
        actions=["delete", "create"],
        resource_type="azurerm_storage_account",
        address="azapi_resource.api",
    )
    with pytest.raises(terraform_deploy.DeploymentError):
        terraform_deploy.deploy(
            variables(tmp_path),
            tmp_path / "state",
            apply=True,
            allow_replace_owned=True,
            execute=runner,
        )
    assert not any(c[0][:2] == ["terraform", "apply"] for c in runner.calls)
