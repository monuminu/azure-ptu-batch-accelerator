"""Explicit, customer-run Entra app setup. No tenant write without --apply."""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid


def az(*args: str) -> dict:
    result = subprocess.run(
        ["az", *args, "--output", "json", "--only-show-errors"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display-name", required=True, help="New customer-owned API app name")
    parser.add_argument("--apply", action="store_true", help="Create app and service principal")
    args = parser.parse_args()
    if not args.apply:
        print("Dry run: would create an Entra API app and service principal with delegated scope")
        print("and application role Batch.ReadWrite. Re-run with --apply to make tenant changes.")
        return

    app = az("ad", "app", "create", "--display-name", args.display_name)
    app_id = app["appId"]
    object_id = app["id"]
    scope_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())
    body = {
        "identifierUris": [f"api://{app_id}"],
        "api": {
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": [
                {
                    "id": scope_id,
                    "value": "Batch.ReadWrite",
                    "type": "Admin",
                    "isEnabled": True,
                    "adminConsentDisplayName": "Access PTU batch accelerator",
                    "adminConsentDescription": "Create, read, and cancel batch jobs and files.",
                }
            ],
        },
        "appRoles": [
            {
                "id": role_id,
                "value": "Batch.ReadWrite.Application",
                "displayName": "PTU batch access",
                "description": "Application access to PTU batch jobs and files.",
                "isEnabled": True,
                "allowedMemberTypes": ["Application"],
            }
        ],
    }
    subprocess.run(
        [
            "az",
            "rest",
            "--method",
            "PATCH",
            "--uri",
            f"https://graph.microsoft.com/v1.0/applications/{object_id}",
            "--headers",
            "Content-Type=application/json",
            "--body",
            json.dumps(body),
            "--only-show-errors",
            "--output",
            "none",
        ],
        check=True,
    )
    az("ad", "sp", "create", "--id", app_id)
    print(f"Accelerator application ID: {app_id}")
    print(f"Audience: {app_id}; scope for clients: api://{app_id}/.default")
    print(
        "Grant delegated consent or assign the application role to each authorized caller separately."
    )


if __name__ == "__main__":
    main()
