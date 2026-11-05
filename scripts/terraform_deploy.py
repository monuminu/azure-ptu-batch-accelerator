"""Private Terraform plan/apply wrapper with a fresh deployment drain gate.

Use this wrapper for every apply, including retries. It does not create, edit,
or invoke an Azure OpenAI model deployment; account and legacy deployment
prechecks use read-only ARM GETs only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


class DeploymentError(RuntimeError):
    """The deployment was stopped before unsafe Terraform apply."""


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "infra" / "terraform"
PUBLIC_OUTPUTS = {
    "api_endpoint_url",
    "api_base_url",
    "api_name",
    "control_name",
    "inference_name",
    "resource_group_name",
    "registry_name",
    "registry_login_server",
    "tenant_id",
    "api_app_id",
    "managed_identity_client_id",
    "managed_identity_principal_id",
    "servicebus_namespace",
    "ptu_alias",
    "openai_account_resource_id",
    "openai_resource_endpoint",
    "deployment_resolution",
    "validation_only",
    "validation_only_warning",
}
REPLACEABLE_ADDRESSES = {
    "azapi_resource.api": "azapi_resource",
    "azapi_resource.control": "azapi_resource",
    "azapi_resource.inference": "azapi_resource",
    "azurerm_container_app_environment.main": "azurerm_container_app_environment",
}
PTU_SKUS = {"ProvisionedManaged", "GlobalProvisionedManaged", "DataZoneProvisionedManaged"}
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ENV_NAME = re.compile(r"^[a-z][a-z0-9-]{0,12}[a-z0-9]$")
_DEPLOYMENT_ID = re.compile(
    r"^/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/[^/]+/providers/"
    r"Microsoft\.CognitiveServices/accounts/[^/]+/deployments/([^/]+)$",
    re.IGNORECASE,
)
_ACCOUNT_ID = re.compile(
    r"^/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/[^/]+/providers/"
    r"Microsoft\.CognitiveServices/accounts/([^/]+)$",
    re.IGNORECASE,
)
_ORIGIN = re.compile(r"^https://[A-Za-z0-9][A-Za-z0-9.-]*/?$")
_READ_ONLY_REFERENCE = re.compile(
    r"^data\.azapi_resource\.(?:existing_account|existing_deployment)(?:\[0\])?$"
)


def _private_file(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)
    path.chmod(0o600)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_sha256() -> str:
    """Fingerprint every input that can change the Terraform graph or image tag."""
    files = set(MODULE.rglob("*.tf")) | set(MODULE.rglob("*.tf.json"))
    files.add(MODULE / ".terraform.lock.hcl")
    files.update(ROOT.glob("Dockerfile.*"))
    files.update(
        ROOT / name
        for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", ".dockerignore")
    )
    files.update(
        path
        for base in (ROOT / "src", ROOT / "functions")
        if base.exists()
        for path in base.rglob("*")
        if path.is_file()
    )
    files.update(ROOT.glob("scripts/terraform_*.py"))
    digest = hashlib.sha256()
    for path in sorted(files):
        if (
            path.is_file()
            and ".terraform" not in path.parts
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        ):
            digest.update(
                str(
                    path.relative_to(MODULE) if MODULE in path.parents else path.relative_to(ROOT)
                ).encode()
            )
            digest.update(b"\0")
            digest.update(bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def _manifest_path(plan: Path) -> Path:
    return Path(str(plan) + ".manifest.json")


def _provenance(
    variables: Path, backend_config: Path | None, state_dir: Path, validation_only: bool
) -> dict[str, Any]:
    return {
        "format": 1,
        "variables_path": str(variables),
        "variables_sha256": _sha256(variables),
        "source_sha256": _source_sha256(),
        "backend_mode": "azurerm" if backend_config else "local",
        "backend_path": str(backend_config) if backend_config else None,
        "backend_sha256": _sha256(backend_config) if backend_config else None,
        "state_dir": str(state_dir),
        "validation_only": validation_only,
    }


def _verify_saved_plan(plan: Path, provenance: dict[str, Any]) -> None:
    manifest_path = _manifest_path(plan)
    if not plan.is_file() or not manifest_path.is_file():
        raise DeploymentError("Saved plan and private manifest must both exist")
    if (plan.stat().st_mode | manifest_path.stat().st_mode) & 0o077:
        raise DeploymentError("Saved plan and manifest must not be group or world accessible")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise DeploymentError("Saved plan manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("plan_sha256") != _sha256(plan):
        raise DeploymentError("Saved plan digest does not match its manifest")
    if any(manifest.get(key) != value for key, value in provenance.items()):
        raise DeploymentError("Saved plan inputs or backend changed since review")


def _backend_mode(backend_config: Path | None, state_file: Path) -> list[str]:
    override = MODULE / "backend_override.tf"
    if backend_config is None:
        if override.exists():
            raise DeploymentError("Remote backend override requires --backend-config")
        return [f"-backend-config=path={state_file}"]
    if backend_config == ROOT or ROOT in backend_config.parents or not backend_config.is_file():
        raise DeploymentError("Backend config must be an existing private file outside repository")
    if backend_config.stat().st_mode & 0o077:
        raise DeploymentError("Backend config must not be group or world accessible")
    if not override.is_file() or not re.search(r'backend\s+"azurerm"\s*\{', override.read_text()):
        raise DeploymentError("Remote mode requires backend_override.tf declaring azurerm backend")
    if state_file.exists():
        raise DeploymentError(
            "Local state exists; migrate state manually before remote backend use"
        )
    return [f"-backend-config={backend_config}"]


def _execute(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    log_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    if log_file is None:
        return subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, check=False
        )
    descriptor = os.open(log_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    log_file.chmod(0o600)
    return result


def _run(
    execute: Callable,
    command: list[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str],
    phase: str,
    log_file: Path | None = None,
) -> str:
    try:
        result = execute(command, cwd=cwd, env=env, log_file=log_file)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError(f"{phase} could not run") from exc
    if result.returncode:
        suffix = f"; log: {log_file}" if log_file else ""
        raise DeploymentError(f"{phase} failed{suffix}")
    return result.stdout or ""


def _load_variables(path: Path, validation_only: bool) -> dict[str, Any]:
    try:
        values = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise DeploymentError("Variable file must be readable JSON") from exc
    if not isinstance(values, dict):
        raise DeploymentError("Variable file must contain a JSON object")
    subscription = values.get("subscription_id")
    tenant = values.get("tenant_id")
    env_name = values.get("environment_name", "ptubatchtf")
    resource_id = values.get("ptu_deployment_resource_id")
    account_id = values.get("openai_account_resource_id")
    account_endpoint = values.get("openai_resource_endpoint")
    if not isinstance(subscription, str) or not _GUID.fullmatch(subscription):
        raise DeploymentError("subscription_id must be an explicit GUID")
    if not isinstance(tenant, str) or not _GUID.fullmatch(tenant):
        raise DeploymentError("tenant_id must be an explicit GUID")
    if not isinstance(env_name, str) or not _ENV_NAME.fullmatch(env_name):
        raise DeploymentError("environment_name is invalid")
    if values.get("validation_only", False) is not validation_only:
        raise DeploymentError(
            "--validation-only must exactly match validation_only in the variable file"
        )
    if account_id is not None or account_endpoint is not None:
        if validation_only:
            raise DeploymentError("Account-only JSONL routing cannot use validation_only")
        if any(
            values.get(field) is not None
            for field in (
                "ptu_deployment_resource_id",
                "ptu_deployment_name",
                "ptu_resource_endpoint",
            )
        ):
            raise DeploymentError("Account-only JSONL routing cannot mix legacy deployment fields")
        if not isinstance(account_id, str) or not (match := _ACCOUNT_ID.fullmatch(account_id)):
            raise DeploymentError("openai_account_resource_id must be an existing account ARM ID")
        if match.group(1).lower() != subscription.lower():
            raise DeploymentError("Account reference must be in the requested subscription")
        if not isinstance(account_endpoint, str) or not _ORIGIN.fullmatch(account_endpoint):
            raise DeploymentError("openai_resource_endpoint must be a plain HTTPS origin")
    else:
        if not isinstance(resource_id, str) or not (match := _DEPLOYMENT_ID.fullmatch(resource_id)):
            raise DeploymentError(
                "ptu_deployment_resource_id must be an existing deployment ARM ID"
            )
        if match.group(1).lower() != subscription.lower():
            raise DeploymentError("Deployment reference must be in the requested subscription")
        if match.group(2).lower() != str(values.get("ptu_deployment_name", "")).lower():
            raise DeploymentError("Deployment name must match the ARM ID")
    return values


def _check_deployment(
    values: Mapping[str, Any], *, execute: Callable, env: Mapping[str, str], validation_only: bool
) -> None:
    account_id = values.get("openai_account_resource_id")
    if account_id is not None:
        command = [
            "az",
            "rest",
            "--method",
            "get",
            "--url",
            f"https://management.azure.com{account_id}?api-version=2025-06-01",
            "--subscription",
            values["subscription_id"],
            "--output",
            "json",
            "--only-show-errors",
        ]
        raw = _run(execute, command, cwd=None, env=env, phase="Existing account ARM GET")
        try:
            data = json.loads(raw)
            actual_id = data["id"]
            name = data["name"]
            state = data["properties"]["provisioningState"]
            endpoint = data["properties"]["endpoint"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DeploymentError("Existing account response is incomplete") from exc
        if not all(isinstance(value, str) for value in (actual_id, name, state, endpoint)):
            raise DeploymentError("Existing account response is incomplete")
        if (
            actual_id.lower() != account_id.lower()
            or name.lower() != account_id.rsplit("/", 1)[-1].lower()
        ):
            raise DeploymentError(
                "Existing account ID or name does not match the requested account"
            )
        if state.lower() != "succeeded":
            raise DeploymentError("Existing account provisioning state must be Succeeded")
        if endpoint.rstrip("/").lower() != values["openai_resource_endpoint"].rstrip("/").lower():
            raise DeploymentError("Existing account endpoint does not match the requested origin")
        return
    resource_id = values["ptu_deployment_resource_id"]
    command = [
        "az",
        "rest",
        "--method",
        "get",
        "--url",
        f"https://management.azure.com{resource_id}?api-version=2025-06-01",
        "--subscription",
        values["subscription_id"],
        "--output",
        "json",
        "--only-show-errors",
    ]
    raw = _run(execute, command, cwd=None, env=env, phase="Existing deployment ARM GET")
    try:
        data = json.loads(raw)
        sku = data["sku"]["name"]
        capacity = data["sku"]["capacity"]
        state = data["properties"]["provisioningState"]
        spillover = data["properties"].get("spilloverDeploymentName")
    except (ValueError, KeyError, TypeError) as exc:
        raise DeploymentError("Existing deployment response is incomplete") from exc
    if not isinstance(capacity, (int, float)) or isinstance(capacity, bool) or capacity <= 0:
        raise DeploymentError("Existing deployment capacity must be positive")
    if state != "Succeeded" or spillover not in (None, ""):
        raise DeploymentError("Existing deployment must be Succeeded with no spillover")
    if sku not in PTU_SKUS and not (validation_only and sku == "Standard"):
        raise DeploymentError("Existing deployment is not an allowed PTU SKU")


def _namespace_from_state(state_file: Path) -> str:
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text())
            output = data.get("outputs", {}).get("servicebus_namespace", {}).get("value")
            if isinstance(output, str) and output:
                return output
            for resource in data.get("resources", []):
                if (
                    resource.get("mode") == "managed"
                    and resource.get("type") == "azurerm_servicebus_namespace"
                    and resource.get("name") == "main"
                ):
                    for instance in resource.get("instances", []):
                        name = instance.get("attributes", {}).get("name")
                        if isinstance(name, str) and name:
                            return name
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise DeploymentError(
                "Cannot read owned Terraform state for Service Bus namespace"
            ) from exc
    # First provision exits the guard before queue lookup; this is never used for a live queue.
    return "sb-bootstrap-placeholder"


def _namespace_from_remote(execute: Callable, env: Mapping[str, str]) -> str:
    command = ["terraform", "output", "-json", "servicebus_namespace"]
    try:
        result = execute(command, cwd=MODULE, env=env, log_file=None)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError("Cannot query remote Service Bus namespace") from exc
    if result.returncode:
        detail = (result.stderr or "").lower()
        if (
            "no outputs found" in detail
            or 'output "servicebus_namespace" not found' in detail
            or "the output variable requested could not be found" in detail
        ):
            # A first provision has no output yet. The guard checks public API
            # existence before any queue query and fails closed for a live API.
            return "sb-bootstrap-placeholder"
        raise DeploymentError("Cannot read remote Service Bus namespace")
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise DeploymentError("Remote Service Bus namespace output is invalid") from exc
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,49}", value):
        raise DeploymentError("Remote Service Bus namespace output is invalid")
    return value


def _plan_counts(raw: str) -> tuple[dict[str, int], list[dict[str, Any]]]:
    try:
        changes = json.loads(raw).get("resource_changes", [])
        if not isinstance(changes, list):
            raise ValueError("resource_changes not a list")
        counts = {"create": 0, "update": 0, "delete": 0}
        destructive = []
        for change in changes:
            details = change.get("change", {})
            before = details.get("before") or {}
            after = details.get("after") or {}
            azapi_types = [before.get("type", ""), after.get("type", "")]
            actions = details["actions"]
            if (
                change.get("mode") == "data"
                and change.get("type") == "azapi_resource"
                and _READ_ONLY_REFERENCE.fullmatch(change.get("address", ""))
                # Deleting a data-source address only drops Terraform's cached
                # read result when legacy routing is replaced by account mode.
                and actions in (["read"], ["no-op"], ["delete"])
            ):
                continue
            if (
                change.get("type", "").startswith("azurerm_cognitive_")
                or "existing_deployment" in change.get("address", "")
                or "existing_account" in change.get("address", "")
                or any(
                    value.lower().startswith("microsoft.cognitiveservices/")
                    for value in azapi_types
                    if isinstance(value, str)
                )
            ):
                raise DeploymentError(
                    "The referenced Azure OpenAI account or deployment cannot be modified"
                )
            for action in counts:
                if action in actions:
                    counts[action] += 1
            if "delete" in actions and change.get("type") != "terraform_data":
                destructive.append(change)
        return counts, destructive
    except (ValueError, KeyError, TypeError) as exc:
        raise DeploymentError("Saved plan JSON is incomplete") from exc


def deploy(
    variable_file: Path | str,
    state_dir: Path | str,
    *,
    apply: bool = False,
    apply_plan: Path | str | None = None,
    backend_config: Path | str | None = None,
    validation_only: bool = False,
    allow_replace_owned: bool = False,
    execute: Callable = _execute,
    emit: Callable[[str], None] = print,
) -> Path:
    """Plan privately, and optionally guard then apply the exact saved plan."""
    if apply and apply_plan is not None:
        raise DeploymentError("Use either --apply or --apply-plan, not both")
    variables_path = Path(variable_file).expanduser().resolve()
    private = Path(state_dir).expanduser().resolve()
    backend_path = Path(backend_config).expanduser().resolve() if backend_config else None
    reviewed_plan = Path(apply_plan).expanduser().resolve() if apply_plan else None
    if variables_path == ROOT or ROOT in variables_path.parents:
        raise DeploymentError("Variable file must be outside the published repository")
    if private == ROOT or ROOT in private.parents:
        raise DeploymentError("State directory must be outside the published repository")
    if reviewed_plan and (reviewed_plan == ROOT or ROOT in reviewed_plan.parents):
        raise DeploymentError("Saved plan must be outside the published repository")
    values = _load_variables(variables_path, validation_only)
    previous_umask = os.umask(0o077)
    try:
        private.mkdir(parents=True, mode=0o700, exist_ok=True)
        private.chmod(0o700)
        data_dir = private / "data"
        data_dir.mkdir(mode=0o700, exist_ok=True)
        data_dir.chmod(0o700)
        state_file = private / "terraform.tfstate"
        if state_file.exists():
            state_file.chmod(0o600)
        backend_args = _backend_mode(backend_path, state_file)
        plan_file = reviewed_plan or private / "accelerator.tfplan"
        if reviewed_plan is None and plan_file.exists():
            plan_file = private / f"accelerator-{uuid.uuid4().hex[:12]}.tfplan"
        provenance = _provenance(variables_path, backend_path, private, validation_only)
        if reviewed_plan:
            _verify_saved_plan(reviewed_plan, provenance)
        command_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("TF_CLI_ARGS")
            and not key.startswith("TF_VAR_")
            and key not in {"TF_LOG", "TF_LOG_PATH", "TF_LOG_CORE", "TF_LOG_PROVIDER"}
        }
        command_env.update(
            {
                "TF_DATA_DIR": str(data_dir),
                "TF_IN_AUTOMATION": "1",
                "TF_INPUT": "0",
                "ARM_SUBSCRIPTION_ID": values["subscription_id"],
                "ARM_TENANT_ID": values["tenant_id"],
            }
        )

        emit(
            "Checking existing Azure OpenAI account with a read-only ARM GET"
            if values.get("openai_account_resource_id") is not None
            else "Checking existing model deployment with a read-only ARM GET"
        )
        _check_deployment(values, execute=execute, env=command_env, validation_only=validation_only)
        if validation_only:
            emit("VALIDATION ONLY: keep admission paused; make no model calls")

        for phase, command in (
            (
                "Terraform init",
                [
                    "terraform",
                    "init",
                    "-reconfigure",
                    "-lockfile=readonly",
                    "-input=false",
                    "-no-color",
                    *backend_args,
                ],
            ),
            ("Terraform format check", ["terraform", "fmt", "-check", "-recursive"]),
            ("Terraform validate", ["terraform", "validate", "-no-color"]),
        ):
            emit(phase)
            _run(
                execute,
                command,
                cwd=MODULE,
                env=command_env,
                phase=phase,
                log_file=private / f"{phase.lower().replace(' ', '-')}.log",
            )
        if not reviewed_plan:
            emit("Terraform plan")
            _run(
                execute,
                [
                    "terraform",
                    "plan",
                    "-input=false",
                    "-no-color",
                    "-lock-timeout=10m",
                    f"-var-file={variables_path}",
                    f"-out={plan_file}",
                ],
                cwd=MODULE,
                env=command_env,
                phase="Terraform plan",
                log_file=private / "terraform-plan.log",
            )
            if not plan_file.is_file():
                raise DeploymentError("Terraform did not create the saved plan")
            plan_file.chmod(0o600)
            manifest = {**provenance, "plan_sha256": _sha256(plan_file)}
            _private_file(
                _manifest_path(plan_file), json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            )
        else:
            _verify_saved_plan(plan_file, provenance)
        raw_plan = _run(
            execute,
            ["terraform", "show", "-json", str(plan_file)],
            cwd=MODULE,
            env=command_env,
            phase="Terraform plan inspection",
        )
        counts, destructive = _plan_counts(raw_plan)
        emit(
            f"Plan summary: create={counts['create']} update={counts['update']} delete={counts['delete']}"
        )
        if not apply and not reviewed_plan:
            emit(f"Saved private plan: {plan_file}")
            return plan_file
        for change in destructive:
            actions = change["change"]["actions"]
            address = change.get("address", "")
            if (
                not allow_replace_owned
                or REPLACEABLE_ADDRESSES.get(address) != change.get("type")
                or "create" not in actions
            ):
                raise DeploymentError(
                    "Plan deletes a protected resource; only reviewed stateless replacements are allowed"
                )
        guard_env = command_env.copy()
        environment = values["environment_name"]
        guard_env.update(
            {
                "PTU_TF_SUBSCRIPTION_ID": values["subscription_id"],
                "PTU_TF_RESOURCE_GROUP": f"rg-{environment}-ptu-batch",
                "PTU_TF_API_NAME": f"ca-{environment}-ptu-api",
                "PTU_TF_CONTROL_NAME": f"ca-{environment}-ptu-control",
                "PTU_TF_INFERENCE_NAME": f"ca-{environment}-ptu-inference",
                "PTU_TF_SERVICEBUS_NAMESPACE": (
                    _namespace_from_remote(execute, command_env)
                    if backend_path
                    else _namespace_from_state(state_file)
                ),
            }
        )
        emit("Running fresh durable drain guard")
        _run(
            execute,
            [sys.executable, str(ROOT / "scripts" / "terraform_guard.py")],
            cwd=ROOT,
            env=guard_env,
            phase="Fresh deployment guard",
            log_file=private / "terraform-guard.log",
        )
        _verify_saved_plan(plan_file, provenance)
        emit("Applying exact saved plan")
        _run(
            execute,
            [
                "terraform",
                "apply",
                "-input=false",
                "-no-color",
                "-auto-approve",
                "-lock-timeout=10m",
                str(plan_file),
            ],
            cwd=MODULE,
            env=command_env,
            phase="Terraform apply",
            log_file=private / "terraform-apply.log",
        )
        if state_file.exists():
            state_file.chmod(0o600)
        raw_outputs = _run(
            execute,
            ["terraform", "output", "-json"],
            cwd=MODULE,
            env=command_env,
            phase="Terraform output capture",
        )
        try:
            output_data = json.loads(raw_outputs)
            public = {
                key: item["value"]
                for key, item in output_data.items()
                if key in PUBLIC_OUTPUTS and item.get("sensitive") is False
            }
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise DeploymentError("Terraform outputs JSON is incomplete") from exc
        _private_file(private / "outputs.json", json.dumps(public, indent=2, sort_keys=True) + "\n")
        emit("Apply completed; nonsecret outputs saved privately")
        return plan_file
    finally:
        os.umask(previous_umask)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variables", required=True, type=Path, help="Private JSON variable file")
    parser.add_argument(
        "--state-dir", required=True, type=Path, help="Private persistent state directory"
    )
    parser.add_argument(
        "--backend-config",
        type=Path,
        help="Private AzureRM backend config; requires backend_override.tf in the module",
    )
    apply_choice = parser.add_mutually_exclusive_group()
    apply_choice.add_argument(
        "--apply", action="store_true", help="Apply the newly saved plan after a fresh drain guard"
    )
    apply_choice.add_argument(
        "--apply-plan",
        type=Path,
        help="Apply a reviewed saved plan with matching private manifest and fresh drain guard",
    )
    parser.add_argument(
        "--validation-only",
        action="store_true",
        help="Allow a Standard reference for paused platform validation",
    )
    parser.add_argument(
        "--allow-replace-owned",
        action="store_true",
        help="Permit reviewed replacement of Terraform-owned resources",
    )
    arguments = parser.parse_args(argv)
    try:
        deploy(
            arguments.variables,
            arguments.state_dir,
            apply=arguments.apply,
            apply_plan=arguments.apply_plan,
            backend_config=arguments.backend_config,
            validation_only=arguments.validation_only,
            allow_replace_owned=arguments.allow_replace_owned,
        )
    except DeploymentError as exc:
        print(f"Terraform deployment blocked: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
