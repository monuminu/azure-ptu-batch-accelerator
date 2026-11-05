"""Operator commands. Preflight is read-only; maintenance writes the admission gate."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from .settings import Deployment, Settings


def deployment_configuration(settings, values):
    if settings.deployments:
        return {key: value.model_dump() for key, value in settings.deployments.items()}
    if values.get("PTU_BATCH_DEPLOYMENTS"):
        return {
            key: Deployment.model_validate(value).model_dump()
            for key, value in json.loads(values["PTU_BATCH_DEPLOYMENTS"]).items()
        }
    required = [
        "PTU_BATCH_PTU_RESOURCE_ENDPOINT",
        "PTU_BATCH_PTU_DEPLOYMENT_NAME",
        "PTU_BATCH_PTU_DEPLOYMENT_RESOURCE_ID",
    ]
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ValueError("Missing deployment configuration: " + ", ".join(missing))
    return {
        values.get("PTU_BATCH_PTU_ALIAS", "ptu"): Deployment(
            resource_endpoint=values[required[0]],
            deployment_name=values[required[1]],
            resource_id=values[required[2]],
        ).model_dump()
    }


def account_configuration(settings, values):
    """Use account routing when configured, including values loaded from azd."""
    if account := getattr(settings, "deployment_account", None):
        return account
    if raw := values.get("PTU_BATCH_DEPLOYMENT_ACCOUNT"):
        from .settings import DeploymentAccount

        return DeploymentAccount.model_validate(json.loads(raw))
    if resource_id := values.get("PTU_BATCH_PTU_ACCOUNT_RESOURCE_ID"):
        from .settings import DeploymentAccount

        return DeploymentAccount(
            resource_id=resource_id,
            resource_endpoint=values.get("PTU_BATCH_PTU_RESOURCE_ENDPOINT", ""),
        )
    return None


def usage_report(jobs, *, input_rate=None, output_rate=None, infrastructure_cost=None):
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    counts = {"completed": 0, "failed": 0}
    batches = 0
    for job in jobs:
        batches += 1
        for key in usage:
            usage[key] += job.get("usage", {}).get(key, 0)
        for key in counts:
            counts[key] += job.get("request_counts", {}).get(key, 0)
    avoided = None
    if input_rate is not None and output_rate is not None:
        if min(input_rate, output_rate) < 0:
            raise ValueError("Rates cannot be negative")
        avoided = round(
            (usage["input_tokens"] * input_rate + usage["output_tokens"] * output_rate) / 1000000, 6
        )
    if infrastructure_cost is not None and infrastructure_cost < 0:
        raise ValueError("Infrastructure cost cannot be negative")
    return {
        "batches": batches,
        "request_counts": counts,
        "known_usage": usage,
        "estimated_avoided_batch_cost": avoided,
        "supplied_infrastructure_cost": infrastructure_cost,
        "estimated_net_savings": round(avoided - infrastructure_cost, 6)
        if avoided is not None and infrastructure_cost is not None
        else None,
        "notes": "Rates and infrastructure cost are operator supplied; unknown inference outcomes are excluded from known usage.",
    }


async def _preflight(args):
    from azure.identity.aio import DefaultAzureCredential

    from .adapters.preflight import AzurePreflight

    # Explicit targets must remain usable even when unrelated stored routing JSON is stale.
    settings = (
        Settings.model_construct(deployments={}, deployment_account=None)
        if args.resource_id
        else Settings()
    )
    values = dict(os.environ)
    if args.resource_id:
        # An explicit target must override both configured routing formats.
        values.pop("PTU_BATCH_DEPLOYMENTS", None)
        values.pop("PTU_BATCH_DEPLOYMENT_ACCOUNT", None)
        values.pop("PTU_BATCH_PTU_ACCOUNT_RESOURCE_ID", None)
        settings.deployments = {}
        settings.deployment_account = None
        values.update(
            PTU_BATCH_PTU_DEPLOYMENT_RESOURCE_ID=args.resource_id,
            PTU_BATCH_PTU_RESOURCE_ENDPOINT=args.endpoint or "",
            PTU_BATCH_PTU_DEPLOYMENT_NAME=args.deployment or "",
            PTU_BATCH_PTU_ALIAS=args.alias,
        )
        if "/deployments/" not in args.resource_id.casefold():
            from .settings import DeploymentAccount

            settings.deployment_account = DeploymentAccount(
                resource_id=args.resource_id, resource_endpoint=args.endpoint or ""
            )
    if (
        not settings.deployments
        and not account_configuration(settings, values)
        and not values.get("PTU_BATCH_DEPLOYMENTS")
        and not values.get("PTU_BATCH_PTU_DEPLOYMENT_RESOURCE_ID")
    ):
        result = subprocess.run(
            ["azd", "env", "get-values", "--output", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
        # Read values into this process only. Never print environment/credential files.
        values = {**json.loads(result.stdout), **values}
    account = account_configuration(settings, values)
    credential_options = {}
    if (
        client_id := values.get("PTU_BATCH_MANAGED_IDENTITY_CLIENT_ID")
        or settings.managed_identity_client_id
    ):
        credential_options["managed_identity_client_id"] = client_id
    async with DefaultAzureCredential(**credential_options) as credential:
        preflight = AzurePreflight(credential)
        try:
            if account is not None:
                if args.deployment is None:
                    await preflight.check_account(account, force=True)
                    print(
                        json.dumps(
                            {
                                "scope": "account",
                                "preflight": "passed",
                                "ptu_eligibility_checked": False,
                            }
                        )
                    )
                else:
                    target = account.resolve(args.deployment).model_dump()
                    await preflight.check(target, force=True)
                    print(
                        json.dumps(
                            {
                                "scope": "deployment",
                                "deployment_name": args.deployment,
                                "preflight": "passed",
                                "ptu_only": True,
                                "ptu_eligibility_checked": True,
                            }
                        )
                    )
            else:
                targets = deployment_configuration(settings, values)
                for alias, target in targets.items():
                    await preflight.check(target, force=True)
                    print(
                        json.dumps(
                            {"deployment_alias": alias, "preflight": "passed", "ptu_only": True}
                        )
                    )
        finally:
            await preflight.close()


async def _operator(args):
    from .bootstrap import build_runtime
    from .engine import Engine

    runtime = await build_runtime()
    try:
        engine = Engine(runtime)
        if args.command == "maintenance":
            if args.pause:
                await engine.set_admission(paused=True)
            elif args.resume:
                await engine.set_admission(paused=False)
            status = await engine.maintenance_status()
            if args.check:
                print("PTU_BATCH_DRAIN_SAFE" if status["safe"] else "PTU_BATCH_DRAIN_BLOCKED")
                if not status["safe"]:
                    print(json.dumps(status))
                    raise SystemExit(1)
            else:
                print(json.dumps(status))
        else:
            jobs = [job async for job in runtime.store.scan(kind="batch")]
            print(
                json.dumps(
                    usage_report(
                        jobs,
                        input_rate=args.input_rate,
                        output_rate=args.output_rate,
                        infrastructure_cost=args.infrastructure_cost,
                    ),
                    indent=2,
                )
            )
    finally:
        await runtime.close()


def main():
    parser = argparse.ArgumentParser(description="Azure PTU Batch Accelerator")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser(
        "demo", help="Run a volatile local synthetic demo; makes no Azure calls"
    )
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8000)
    serve = commands.add_parser("serve", help="Run the configured API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    export = commands.add_parser("export-openapi", help="Export the API contract offline")
    export.add_argument("--output", type=Path, default=Path("openapi.json"))
    preflight = commands.add_parser(
        "preflight", help="Read-only ARM PTU/spillover check; reads azd env if needed"
    )
    preflight.add_argument("--resource-id")
    preflight.add_argument("--endpoint")
    preflight.add_argument("--deployment")
    preflight.add_argument("--alias", default="ptu")
    maintenance = commands.add_parser(
        "maintenance", help="Control durable submission admission and inspect drain status"
    )
    action = maintenance.add_mutually_exclusive_group()
    action.add_argument("--pause", action="store_true")
    action.add_argument("--resume", action="store_true")
    action.add_argument("--check", action="store_true")
    usage = commands.add_parser(
        "usage", help="Report known tokens and optional supplied-rate savings"
    )
    usage.add_argument(
        "--input-rate", type=float, help="Alternative Batch input cost per million tokens"
    )
    usage.add_argument(
        "--output-rate", type=float, help="Alternative Batch output cost per million tokens"
    )
    usage.add_argument(
        "--infrastructure-cost",
        type=float,
        help="Actual accelerator infrastructure spend supplied by operator",
    )
    args = parser.parse_args()
    try:
        if args.command in ("demo", "serve"):
            if args.command == "demo":
                os.environ["PTU_BATCH_MODE"] = "local"
            import uvicorn

            uvicorn.run("ptu_batch.api:app", host=args.host, port=args.port, workers=1)
        elif args.command == "export-openapi":
            from .api import create_app

            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(create_app().openapi(), indent=2) + "\n")
            print(f"OpenAPI exported to {args.output}")
        elif args.command == "preflight":
            asyncio.run(_preflight(args))
        else:
            asyncio.run(_operator(args))
    except (ValueError, RuntimeError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
