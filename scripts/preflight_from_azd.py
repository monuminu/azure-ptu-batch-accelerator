"""Run the read-only CLI preflight with deployment values from the selected azd env."""

from __future__ import annotations

import json
import os
import subprocess


def azd_value(name: str) -> str:
    result = subprocess.run(
        ["azd", "env", "get-value", name],
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value:
        raise ValueError(f"azd environment value {name} is empty")
    return value


def azd_values() -> dict[str, str]:
    result = subprocess.run(
        ["azd", "env", "get-values", "--output", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    values = json.loads(result.stdout)
    if not isinstance(values, dict):
        raise ValueError("azd environment values must be a JSON object")
    return values


def required(values: dict[str, str], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"azd environment value {name} is empty")
    return value


def main() -> None:
    values = azd_values()
    env = os.environ.copy()
    env["PTU_BATCH_MODE"] = "azure"
    for key in ("PTU_BATCH_TENANT_ID", "PTU_BATCH_AUDIENCE"):
        if values.get(key):
            env[key] = values[key]
    endpoint = required(values, "PTU_BATCH_PTU_RESOURCE_ENDPOINT")
    if account_id := values.get("PTU_BATCH_PTU_ACCOUNT_RESOURCE_ID"):
        env.pop("PTU_BATCH_DEPLOYMENTS", None)
        env["PTU_BATCH_DEPLOYMENT_ACCOUNT"] = json.dumps(
            {
                "resource_endpoint": endpoint,
                "resource_id": account_id,
                "allowed_apis": ["/chat/completions", "/responses"],
                "allowed_subjects": [],
            }
        )
    else:
        env.pop("PTU_BATCH_DEPLOYMENT_ACCOUNT", None)
        alias = values.get("PTU_BATCH_PTU_ALIAS", "ptu")
        deployment = {
            "resource_endpoint": endpoint,
            "deployment_name": required(values, "PTU_BATCH_PTU_DEPLOYMENT_NAME"),
            "resource_id": required(values, "PTU_BATCH_PTU_DEPLOYMENT_RESOURCE_ID"),
            "allowed_apis": ["/chat/completions", "/responses"],
            "allowed_subjects": [],
        }
        env["PTU_BATCH_DEPLOYMENTS"] = json.dumps({alias: deployment})
    subprocess.run(
        ["uv", "run", "--locked", "--all-extras", "ptu-batch", "preflight"],
        check=True,
        env=env,
    )


if __name__ == "__main__":
    main()
