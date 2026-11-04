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


def main() -> None:
    alias = azd_value("PTU_BATCH_PTU_ALIAS")
    deployment = {
        "resource_endpoint": azd_value("PTU_BATCH_PTU_RESOURCE_ENDPOINT"),
        "deployment_name": azd_value("PTU_BATCH_PTU_DEPLOYMENT_NAME"),
        "resource_id": azd_value("PTU_BATCH_PTU_DEPLOYMENT_RESOURCE_ID"),
        "allowed_apis": ["/chat/completions", "/responses"],
        "allowed_subjects": [],
    }
    env = os.environ.copy()
    env.update(
        PTU_BATCH_MODE="azure",
        PTU_BATCH_TENANT_ID=azd_value("PTU_BATCH_TENANT_ID"),
        PTU_BATCH_AUDIENCE=azd_value("PTU_BATCH_AUDIENCE"),
        PTU_BATCH_DEPLOYMENTS=json.dumps({alias: deployment}),
    )
    subprocess.run(
        ["uv", "run", "--locked", "--all-extras", "ptu-batch", "preflight"],
        check=True,
        env=env,
    )


if __name__ == "__main__":
    main()
