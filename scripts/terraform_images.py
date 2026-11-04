"""Build and publish Terraform's three immutable Container Apps images.

Terraform passes only public deployment identifiers through PTU_TF_* variables.
The short lived ACR token stays in process memory and Docker's private config.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

ROLES = ("api", "control", "inference")
ACR_USERNAME = "00000000-0000-0000-0000-000000000000"
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_RESOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._()-]{0,89}$")
_REGISTRY = re.compile(r"^[a-z0-9]{5,50}$")
_TAG = re.compile(r"^(?:[0-9a-f]{12}|v[A-Za-z0-9][A-Za-z0-9._-]{0,126})$")


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _validate(
    env: Mapping[str, str],
) -> tuple[str, str, str, str, Path, str, str, Path | None, int]:
    subscription = _required(env, "PTU_TF_SUBSCRIPTION_ID")
    group = _required(env, "PTU_TF_RESOURCE_GROUP")
    registry = _required(env, "PTU_TF_REGISTRY_NAME")
    server = _required(env, "PTU_TF_REGISTRY_LOGIN_SERVER")
    source = Path(_required(env, "PTU_TF_SOURCE_ROOT")).resolve()
    tag = _required(env, "PTU_TF_IMAGE_TAG")
    index = env.get("PTU_TF_PIP_INDEX_URL", "https://pypi.org/simple")
    log_value = env.get("PTU_TF_LOG_DIR")
    try:
        retry_seconds = int(env.get("PTU_TF_ACR_RETRY_TIMEOUT_SECONDS", "300"))
    except ValueError as exc:
        raise ValueError("PTU_TF_ACR_RETRY_TIMEOUT_SECONDS must be a whole number") from exc
    if not 1 <= retry_seconds <= 600:
        raise ValueError("PTU_TF_ACR_RETRY_TIMEOUT_SECONDS must be between 1 and 600")
    if not _GUID.fullmatch(subscription):
        raise ValueError("PTU_TF_SUBSCRIPTION_ID must be a GUID")
    if not _RESOURCE.fullmatch(group):
        raise ValueError("PTU_TF_RESOURCE_GROUP is invalid")
    if not _REGISTRY.fullmatch(registry) or server != f"{registry}.azurecr.io":
        raise ValueError("ACR name and login server must match")
    if not _TAG.fullmatch(tag):
        raise ValueError("PTU_TF_IMAGE_TAG must be a source hash or version tag")
    parsed = urlsplit(index)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or "@" in parsed.netloc
        or any(character.isspace() or ord(character) < 32 for character in index)
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValueError("PTU_TF_PIP_INDEX_URL must be an unauthenticated HTTPS package index")
    if not source.is_dir() or any(not (source / f"Dockerfile.{role}").is_file() for role in ROLES):
        raise ValueError("PTU_TF_SOURCE_ROOT must contain all three Dockerfiles")
    log_dir = Path(log_value).resolve() if log_value else None
    return subscription, group, registry, server, source, tag, index, log_dir, retry_seconds


def _execute(
    command: list[str],
    *,
    input: str | None = None,
    log_file: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if log_file is None:
        return subprocess.run(
            command, input=input, text=True, capture_output=True, env=env, check=False
        )
    # Create files without a window in which a Docker build log is world-readable.
    descriptor = os.open(log_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        return subprocess.run(
            command,
            input=input,
            text=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )


def _install_buildx(config_dir: Path) -> None:
    # Docker Desktop normally finds this in ~/.docker, which this hook never reads.
    candidates = (
        Path("/Applications/Docker.app/Contents/Resources/cli-plugins/docker-buildx"),
        Path("/usr/local/lib/docker/cli-plugins/docker-buildx"),
        Path("/opt/homebrew/lib/docker/cli-plugins/docker-buildx"),
    )
    for candidate in candidates:
        if candidate.is_file():
            plugin_dir = config_dir / "cli-plugins"
            plugin_dir.mkdir(mode=0o700, exist_ok=True)
            (plugin_dir / "docker-buildx").symlink_to(candidate)
            return


@contextmanager
def _docker_config():
    # Never let docker login update an inherited profile, even if it is explicit.
    # Docker's connection remains selectable through DOCKER_HOST below.
    with tempfile.TemporaryDirectory(prefix="ptu-tf-docker-") as temporary:
        path = Path(temporary)
        path.chmod(0o700)
        config = path / "config.json"
        descriptor = os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write('{"auths":{}}\n')
        _install_buildx(path)
        yield path


def _checked(
    command: list[str],
    *,
    execute: Callable,
    env: Mapping[str, str],
    log_file: Path | None = None,
    input: str | None = None,
    phase: str,
) -> subprocess.CompletedProcess[str]:
    outcome = execute(command, input=input, log_file=log_file, env=env)
    if outcome.returncode:
        suffix = f"; details: {log_file}" if log_file else ""
        raise RuntimeError(f"{phase} failed{suffix}")
    return outcome


def publish_images(
    env: Mapping[str, str] | None = None,
    *,
    execute: Callable = _execute,
    pause: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    emit: Callable[[str], None] = print,
) -> dict[str, str]:
    """Publish the images and return the exact immutable references used by Terraform."""
    values = os.environ if env is None else env
    subscription, _group, registry, server, source, tag, index, log_dir, retry_seconds = _validate(
        values
    )
    refs = {role: f"{server}/ptu-batch-{role}:{tag}" for role in ROLES}
    if log_dir is not None:
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_dir.chmod(0o700)
    with _docker_config() as config_dir:
        command_env = os.environ.copy()
        command_env["DOCKER_CONFIG"] = str(config_dir)
        command_env.pop("DOCKER_CONTEXT", None)
        if values.get("DOCKER_HOST"):
            command_env["DOCKER_HOST"] = values["DOCKER_HOST"]
        for role in ROLES:
            emit(f"Building {role} image for linux/amd64")
            command = [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/amd64",
                "--load",
                "--file",
                str(source / f"Dockerfile.{role}"),
                "--tag",
                refs[role],
                "--build-arg",
                f"PIP_INDEX_URL={index}",
                str(source),
            ]
            _checked(
                command,
                execute=execute,
                env=command_env,
                log_file=log_dir / f"build-{role}.log" if log_dir else None,
                phase=f"{role} build",
            )

        emit("Requesting short lived ACR login token")
        login = [
            "az",
            "acr",
            "login",
            "--subscription",
            subscription,
            "--name",
            registry,
            "--expose-token",
            "--output",
            "json",
            "--only-show-errors",
        ]
        deadline = now() + retry_seconds
        while True:
            outcome = execute(login, input=None, log_file=None, env=command_env)
            if outcome.returncode == 0:
                try:
                    token = json.loads(outcome.stdout)["accessToken"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise RuntimeError("ACR login returned no access token") from exc
                if not isinstance(token, str) or not token:
                    raise RuntimeError("ACR login returned no access token")
                break
            if now() >= deadline:
                raise RuntimeError("ACR login failed after retry window")
            pause(min(15, max(0, deadline - now())))

        emit("Logging Docker into the private ACR only")
        _checked(
            ["docker", "login", server, "--username", ACR_USERNAME, "--password-stdin"],
            execute=execute,
            env=command_env,
            input=token,
            phase="Docker ACR login",
        )
        token = ""  # Python cannot guarantee erasure, but no later command receives it.
        for role in ROLES:
            emit(f"Pushing {role} image")
            command = ["docker", "push", refs[role]]
            while True:
                outcome = execute(
                    command,
                    input=None,
                    log_file=log_dir / f"push-{role}.log" if log_dir else None,
                    env=command_env,
                )
                if outcome.returncode == 0:
                    break
                if now() >= deadline:
                    suffix = f"; details: {log_dir / f'push-{role}.log'}" if log_dir else ""
                    raise RuntimeError(f"{role} push failed after retry window{suffix}")
                emit(f"{role} push pending ACR role propagation; retrying")
                pause(min(15, max(0, deadline - now())))
    emit("All three images published")
    return refs


def main() -> int:
    try:
        publish_images()
    except (ValueError, RuntimeError) as exc:
        print(f"Terraform image publishing blocked: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
