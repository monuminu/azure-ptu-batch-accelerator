"""Block Terraform app updates until durable admission and old work are drained.

This command never pauses or resumes admission. Operators must pause it through
the deployed control runtime before updating an existing public API.
"""

from __future__ import annotations

import errno
import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping


class DeploymentBlocked(RuntimeError):
    """Existing traffic cannot safely cross a Terraform app update."""


_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_RESOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._()-]{0,89}$")
_APP = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_NAMESPACE = re.compile(r"^[a-z][a-z0-9-]{1,49}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
_MISSING = re.compile(r"\((?:ResourceGroupNotFound|ResourceNotFound)\)")
_MARKER = "PTU_BATCH_DRAIN_SAFE"
_CONTROL_PLACEHOLDER = "mcr.microsoft.com/azure-functions/python:4-python3.12"
_API_PLACEHOLDER = "python:3.12-slim"
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _validate(env: Mapping[str, str]) -> tuple[str, str, str, str, str, str, int]:
    keys = (
        "PTU_TF_SUBSCRIPTION_ID",
        "PTU_TF_RESOURCE_GROUP",
        "PTU_TF_API_NAME",
        "PTU_TF_CONTROL_NAME",
        "PTU_TF_INFERENCE_NAME",
        "PTU_TF_SERVICEBUS_NAMESPACE",
    )
    values = [env.get(key, "") for key in keys]
    if not all(values):
        raise ValueError("All PTU_TF deployment identifiers are required")
    subscription, group, api, control, inference, namespace = values
    if not _GUID.fullmatch(subscription) or not _RESOURCE.fullmatch(group):
        raise ValueError("Invalid Terraform subscription or resource group")
    if not all(_APP.fullmatch(name) for name in (api, control, inference)):
        raise ValueError("Invalid Terraform Container App name")
    if not _NAMESPACE.fullmatch(namespace):
        raise ValueError("Invalid Terraform Service Bus namespace")
    try:
        timeout = int(env.get("PTU_TF_DRAIN_TIMEOUT_SECONDS", "1800"))
    except ValueError as exc:
        raise ValueError("Drain timeout must be a whole number") from exc
    if not 1 <= timeout <= 3600:
        raise ValueError("Drain timeout must be between 1 and 3600 seconds")
    return subscription, group, api, control, inference, namespace, timeout


def _interactive_exec(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Azure Container Apps exec requires a PTY even for a single command."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(command[0], command)
    deadline = time.monotonic() + 90
    output = bytearray()
    status = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 1)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 1024 * 1024:
                    raise DeploymentBlocked("Control maintenance response exceeded 1 MiB")
            if status is None:
                child, child_status = os.waitpid(pid, os.WNOHANG)
                if child:
                    status = child_status
        else:
            raise DeploymentBlocked("Control maintenance check timed out")
    finally:
        os.close(fd)
        if status is None:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(pid, 0)
    return subprocess.CompletedProcess(
        command, os.waitstatus_to_exitcode(status), output.decode("utf-8", errors="replace"), ""
    )


def _execute(command: list[str], *, interactive: bool = False) -> subprocess.CompletedProcess[str]:
    if interactive:
        return _interactive_exec(command)
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=90)


def _query(
    execute: Callable, command: list[str], *, missing_ok: bool = False, interactive: bool = False
) -> str | None:
    outcome = execute(command, interactive=interactive)
    if outcome.returncode:
        if missing_ok and _MISSING.search(outcome.stderr or ""):
            return None
        raise DeploymentBlocked(f"Azure check failed: {' '.join(command[:3])}")
    return (outcome.stdout or "").strip()


def _az(*parts: str, subscription: str) -> list[str]:
    return ["az", *parts, "--subscription", subscription, "--only-show-errors"]


def _count(
    execute: Callable, subscription: str, group: str, namespace: str, queue: str, field: str
) -> int:
    command = _az(
        "servicebus",
        "queue",
        "show",
        "--resource-group",
        group,
        "--namespace-name",
        namespace,
        "--name",
        queue,
        "--query",
        f"countDetails.{field}",
        "--output",
        "tsv",
        subscription=subscription,
    )
    value = _query(execute, command)
    if value is None or not value.isascii() or not value.isdecimal():
        raise DeploymentBlocked("Service Bus queue count is unavailable")
    return int(value)


def _revisions(execute: Callable, subscription: str, group: str, inference: str) -> list[str]:
    command = _az(
        "containerapp",
        "revision",
        "list",
        "--resource-group",
        group,
        "--name",
        inference,
        "--query",
        "[?properties.active].name",
        "--output",
        "tsv",
        subscription=subscription,
    )
    value = _query(execute, command)
    revisions = value.split() if value else []
    if any(not _REVISION.fullmatch(revision) for revision in revisions):
        raise DeploymentBlocked("Invalid active inference revision name")
    return revisions


def _replicas(
    execute: Callable, subscription: str, group: str, inference: str, revisions: list[str]
) -> int:
    total = 0
    for revision in revisions:
        command = _az(
            "containerapp",
            "replica",
            "list",
            "--resource-group",
            group,
            "--name",
            inference,
            "--revision",
            revision,
            "--query",
            "length(@)",
            "--output",
            "tsv",
            subscription=subscription,
        )
        value = _query(execute, command)
        if value is None or not value.isascii() or not value.isdecimal():
            raise DeploymentBlocked("Inference replica count is unavailable")
        total += int(value)
    return total


def _check_maintenance(
    execute: Callable,
    subscription: str,
    group: str,
    control: str,
    pause: Callable[[float], None],
) -> None:
    check = _az(
        "containerapp",
        "exec",
        "--resource-group",
        group,
        "--name",
        control,
        "--container",
        "control",
        "--command",
        "ptu-batch maintenance --check",
        subscription=subscription,
    )
    for attempt in range(3):
        try:
            outcome = execute(check, interactive=True)
        except (OSError, subprocess.SubprocessError, DeploymentBlocked, TimeoutError):
            outcome = None
        if outcome is not None:
            response = f"{outcome.stdout or ''}\n{outcome.stderr or ''}"
            lines = [
                _ANSI.sub("", line).strip() for line in response.replace("\r", "").splitlines()
            ]
            if "PTU_BATCH_DRAIN_BLOCKED" in lines:
                raise DeploymentBlocked("Control reported that admission or work is unsafe")
            for line in lines:
                try:
                    status = json.loads(line)
                except ValueError:
                    continue
                if isinstance(status, dict) and status.get("safe") is False:
                    raise DeploymentBlocked("Control reported that admission or work is unsafe")
            if outcome.returncode == 0 and _MARKER in lines:
                return
        if attempt < 2:
            pause(2)
    raise DeploymentBlocked("Control maintenance check failed after three attempts")


def verify_drain(
    env: Mapping[str, str] | None = None,
    *,
    execute: Callable = _execute,
    pause: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    emit: Callable[[str], None] = print,
) -> bool:
    """Return true only when creating or updating the apps is safe."""
    subscription, group, api, control, inference, namespace, timeout = _validate(
        os.environ if env is None else env
    )
    group_cmd = _az("group", "show", "--name", group, "--output", "json", subscription=subscription)
    if _query(execute, group_cmd, missing_ok=True) is None:
        emit("First provision: accelerator resource group does not exist")
        return True

    images: dict[str, str | None] = {}
    for role, name in (("api", api), ("control", control), ("inference", inference)):
        command = _az(
            "containerapp",
            "show",
            "--resource-group",
            group,
            "--name",
            name,
            "--query",
            "properties.template.containers[0].image",
            "--output",
            "tsv",
            subscription=subscription,
        )
        images[role] = _query(execute, command, missing_ok=True)
        if images[role] == "":
            raise DeploymentBlocked(f"{role} image is unavailable")

    if images["api"] is None and images["control"] in (None, _CONTROL_PLACEHOLDER):
        emit("Bootstrap: no public API can admit jobs")
        return True
    if images["api"] == _API_PLACEHOLDER and images["control"] == _CONTROL_PLACEHOLDER:
        emit("Bootstrap: API and control are placeholders")
        return True
    if images["control"] in (None, _CONTROL_PLACEHOLDER):
        raise DeploymentBlocked("Control runtime is unavailable for durable maintenance check")

    _check_maintenance(execute, subscription, group, control, pause)
    emit("Durable maintenance gate confirmed")

    deadline = now() + timeout
    zero_samples = 0
    last_revisions: list[str] = []
    while True:
        counts = [
            _count(
                execute, subscription, group, namespace, "batch-inference", "activeMessageCount"
            ),
            _count(
                execute, subscription, group, namespace, "batch-inference", "scheduledMessageCount"
            ),
            _count(execute, subscription, group, namespace, "batch-control", "activeMessageCount"),
            _count(
                execute, subscription, group, namespace, "batch-control", "scheduledMessageCount"
            ),
        ]
        last_revisions = (
            _revisions(execute, subscription, group, inference)
            if images["inference"] is not None
            else []
        )
        replicas = _replicas(execute, subscription, group, inference, last_revisions)
        emit(
            f"Drain: inference active={counts[0]} scheduled={counts[1]}; "
            f"control active={counts[2]} scheduled={counts[3]}; replicas={replicas}"
        )
        if all(count == 0 for count in counts) and replicas == 0:
            zero_samples += 1
            if zero_samples == 2:
                break
        else:
            zero_samples = 0
        if now() >= deadline:
            raise DeploymentBlocked("Drain timed out")
        pause(min(2, max(0, deadline - now())))

    _check_maintenance(execute, subscription, group, control, pause)
    for revision in last_revisions:
        command = _az(
            "containerapp",
            "revision",
            "deactivate",
            "--resource-group",
            group,
            "--name",
            inference,
            "--revision",
            revision,
            "--output",
            "none",
            subscription=subscription,
        )
        _query(execute, command)
        emit(f"Deactivated drained inference revision {revision}")
    if images["inference"] is not None and _revisions(execute, subscription, group, inference):
        raise DeploymentBlocked("An inference revision remains active")
    emit("Terraform deployment gate passed; keep admission paused through health checks")
    return True


def main() -> int:
    try:
        verify_drain()
    except (ValueError, DeploymentBlocked) as exc:
        print(f"Terraform deployment blocked: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
