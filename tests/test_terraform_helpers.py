"""Terraform hook tests; external Azure and Docker commands are simulated."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import terraform_guard, terraform_images

SUBSCRIPTION = "11111111-2222-4333-8444-555555555555"
TOKEN = "super-secret-acr-token"


def image_env(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "source"
    root.mkdir()
    for role in ("api", "control", "inference"):
        (root / f"Dockerfile.{role}").write_text("FROM scratch\n")
    return {
        "PTU_TF_SUBSCRIPTION_ID": SUBSCRIPTION,
        "PTU_TF_RESOURCE_GROUP": "rg-ptubatchtf-ptu-batch",
        "PTU_TF_REGISTRY_NAME": "acrptubatch1234",
        "PTU_TF_REGISTRY_LOGIN_SERVER": "acrptubatch1234.azurecr.io",
        "PTU_TF_SOURCE_ROOT": str(root),
        "PTU_TF_IMAGE_TAG": "123456789abc",
    }


def guard_env() -> dict[str, str]:
    return {
        "PTU_TF_SUBSCRIPTION_ID": SUBSCRIPTION,
        "PTU_TF_RESOURCE_GROUP": "rg-ptubatchtf-ptu-batch",
        "PTU_TF_API_NAME": "ca-ptubatchtf-ptu-api",
        "PTU_TF_CONTROL_NAME": "ca-ptubatchtf-ptu-control",
        "PTU_TF_INFERENCE_NAME": "ca-ptubatchtf-ptu-inference",
        "PTU_TF_SERVICEBUS_NAMESPACE": "sb-ptubatchtf-1234",
    }


def result(cmd, stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(cmd, code, stdout, stderr)


def test_images_builds_three_amd64_references_and_pushes_with_private_auth(tmp_path, capsys):
    env = image_env(tmp_path)
    calls = []

    def execute(cmd, *, input=None, log_file=None, env=None):
        calls.append((list(cmd), input, log_file, dict(env or {})))
        if cmd[:3] == ["az", "acr", "login"]:
            return result(cmd, json.dumps({"accessToken": TOKEN}))
        return result(cmd)

    refs = terraform_images.publish_images(env, execute=execute, pause=lambda _: None)
    assert refs == {
        role: f"acrptubatch1234.azurecr.io/ptu-batch-{role}:123456789abc"
        for role in ("api", "control", "inference")
    }
    builds = [call for call in calls if call[0][:3] == ["docker", "buildx", "build"]]
    pushes = [call for call in calls if call[0][:2] == ["docker", "push"]]
    assert len(builds) == len(pushes) == 3
    assert all(["--platform", "linux/amd64"] == c[0][3:5] for c in builds)
    assert [c[0][c[0].index("--tag") + 1] for c in builds] == list(refs.values())
    assert [c[0][2] for c in pushes] == list(refs.values())
    assert all(
        "--build-arg" in c[0] and "PIP_INDEX_URL=https://pypi.org/simple" in c[0] for c in builds
    )
    login = next(c for c in calls if c[0][:2] == ["docker", "login"])
    assert login[1] == TOKEN
    assert "--password-stdin" in login[0]
    assert login[3]["DOCKER_CONFIG"] != str(Path.home() / ".docker")
    assert not Path(login[3]["DOCKER_CONFIG"]).exists()  # owned config is removed
    assert all(TOKEN not in " ".join(c[0]) for c in calls)
    assert TOKEN not in capsys.readouterr().out
    assert all("--subscription" in c[0] for c in calls if c[0][0] == "az")


@pytest.mark.parametrize(
    "field,value",
    [
        ("PTU_TF_SUBSCRIPTION_ID", "not-a-guid"),
        ("PTU_TF_RESOURCE_GROUP", "bad group; echo leaked"),
        ("PTU_TF_REGISTRY_NAME", "otherregistry"),
        ("PTU_TF_REGISTRY_LOGIN_SERVER", "example.com"),
        ("PTU_TF_IMAGE_TAG", "latest"),
        ("PTU_TF_IMAGE_TAG", "v1;touch /tmp/oops"),
        ("PTU_TF_PIP_INDEX_URL", "https://user:pass@pypi.org/simple"),
        ("PTU_TF_PIP_INDEX_URL", "https://@pypi.org/simple"),
        ("PTU_TF_PIP_INDEX_URL", "http://pypi.org/simple"),
    ],
)
def test_images_rejects_unsafe_inputs_before_commands(tmp_path, field, value):
    env = image_env(tmp_path)
    env[field] = value
    calls = []
    with pytest.raises(ValueError):
        terraform_images.publish_images(env, execute=lambda cmd, **kw: calls.append(cmd))
    assert calls == []


def test_images_writes_logs_only_to_requested_directory(tmp_path, capsys):
    env = image_env(tmp_path)
    env["PTU_TF_LOG_DIR"] = str(tmp_path / "private-logs")

    def execute(cmd, *, input=None, log_file=None, env=None):
        if log_file:
            Path(log_file).write_text("build detail\n")
        if cmd[:3] == ["az", "acr", "login"]:
            return result(cmd, json.dumps({"accessToken": TOKEN}))
        return result(cmd)

    terraform_images.publish_images(env, execute=execute, pause=lambda _: None)
    logs = list((tmp_path / "private-logs").glob("*.log"))
    assert len(logs) >= 3
    assert all("build detail" in p.read_text() for p in logs)
    assert "build detail" not in capsys.readouterr().out


def test_images_never_writes_auth_into_inherited_docker_config(tmp_path, capsys):
    env = image_env(tmp_path)
    inherited = tmp_path / "source" / ".docker"
    inherited.mkdir()
    original = '{"auths":{"other.example":{"auth":"existing-secret"}}}\n'
    (inherited / "config.json").write_text(original)
    env["DOCKER_CONFIG"] = str(inherited)
    env["DOCKER_HOST"] = "unix:///private/test-docker.sock"
    used_configs = []

    def execute(cmd, *, input=None, log_file=None, env=None):
        used_configs.append(env["DOCKER_CONFIG"])
        assert env["DOCKER_HOST"] == "unix:///private/test-docker.sock"
        if cmd[:3] == ["az", "acr", "login"]:
            return result(cmd, json.dumps({"accessToken": TOKEN}))
        if cmd[:2] == ["docker", "login"]:
            Path(env["DOCKER_CONFIG"], "config.json").write_text(
                json.dumps({"auths": {"acrptubatch1234.azurecr.io": {"auth": TOKEN}}})
            )
        return result(cmd)

    terraform_images.publish_images(env, execute=execute, pause=lambda _: None)
    assert used_configs
    assert all(path != str(inherited) and not Path(path).exists() for path in used_configs)
    assert (inherited / "config.json").read_text() == original
    assert TOKEN not in capsys.readouterr().out


def test_images_retries_acr_push_within_configured_window(tmp_path):
    env = image_env(tmp_path)
    env["PTU_TF_ACR_RETRY_TIMEOUT_SECONDS"] = "20"
    now = [0.0]
    push_attempts = []

    def execute(cmd, *, input=None, log_file=None, env=None):
        if cmd[:3] == ["az", "acr", "login"]:
            return result(cmd, json.dumps({"accessToken": TOKEN}))
        if cmd[:2] == ["docker", "push"]:
            push_attempts.append(cmd[2])
            if len(push_attempts) == 1:
                return result(cmd, stderr="denied", code=1)
        return result(cmd)

    terraform_images.publish_images(
        env,
        execute=execute,
        pause=lambda seconds: now.__setitem__(0, now[0] + seconds),
        now=lambda: now[0],
    )
    assert now[0] == 15
    assert len(push_attempts) == 4
    assert push_attempts[0] == push_attempts[1]


@pytest.mark.parametrize("value", ["0", "601", "forever"])
def test_images_rejects_unbounded_retry_window(tmp_path, value):
    env = image_env(tmp_path)
    env["PTU_TF_ACR_RETRY_TIMEOUT_SECONDS"] = value
    with pytest.raises(ValueError):
        terraform_images.publish_images(env, execute=lambda cmd, **kw: pytest.fail("command ran"))


def test_guard_initial_missing_resource_group_is_safe():
    commands = []

    def execute(cmd, *, interactive=False):
        commands.append(cmd)
        return result(cmd, stderr="(ResourceGroupNotFound) group absent", code=3)

    assert terraform_guard.verify_drain(guard_env(), execute=execute, pause=lambda _: None)
    assert len(commands) == 1
    assert commands[0][:3] == ["az", "group", "show"]
    assert "--subscription" in commands[0]


def test_guard_rejects_unauthorized_group_check_without_bootstrap():
    commands = []

    def execute(cmd, *, interactive=False):
        commands.append(cmd)
        return result(cmd, stderr="(AuthorizationFailed) access denied", code=1)

    with pytest.raises(terraform_guard.DeploymentBlocked):
        terraform_guard.verify_drain(guard_env(), execute=execute, pause=lambda _: None)
    assert len(commands) == 1


def test_guard_rejects_unauthorized_app_check_without_bootstrap():
    commands = []

    def execute(cmd, *, interactive=False):
        commands.append(cmd)
        if cmd[:3] == ["az", "group", "show"]:
            return result(cmd, "{}")
        return result(cmd, stderr="(AuthorizationFailed) app access denied", code=1)

    with pytest.raises(terraform_guard.DeploymentBlocked):
        terraform_guard.verify_drain(guard_env(), execute=execute, pause=lambda _: None)
    assert len(commands) == 2


def test_guard_partial_bootstrap_without_public_api_is_safe():
    seen = []

    def execute(cmd, *, interactive=False):
        seen.append(cmd)
        if cmd[:3] == ["az", "group", "show"]:
            return result(cmd, "{}")
        return result(cmd, stderr="(ResourceNotFound) app absent", code=3)

    assert terraform_guard.verify_drain(guard_env(), execute=execute, pause=lambda _: None)
    assert len(seen) == 4
    assert not any(c[:3] == ["az", "containerapp", "exec"] for c in seen)


def guard_fake(
    *,
    api="real:1",
    control="real:1",
    inference="real:1",
    safe=True,
    counts=("0", "0", "0", "0"),
    replicas="0",
    revisions="oldrev",
):
    commands = []
    samples = []
    active_revisions = revisions.splitlines()

    def execute(cmd, *, interactive=False):
        commands.append((list(cmd), interactive))
        if cmd[:3] == ["az", "group", "show"]:
            return result(cmd, "{}")
        if cmd[:3] == ["az", "containerapp", "show"]:
            name = cmd[cmd.index("--name") + 1]
            if name.endswith("-api"):
                return result(cmd, api)
            if name.endswith("-control"):
                return result(cmd, control)
            return result(cmd, inference)
        if cmd[:3] == ["az", "containerapp", "exec"]:
            return result(cmd, "PTU_BATCH_DRAIN_SAFE\n" if safe else "unsafe\n")
        if cmd[:4] == ["az", "servicebus", "queue", "show"]:
            samples.append(cmd)
            return result(cmd, counts[(len(samples) - 1) % 4])
        if cmd[:4] == ["az", "containerapp", "revision", "list"]:
            return result(cmd, "\n".join(active_revisions))
        if cmd[:4] == ["az", "containerapp", "replica", "list"]:
            return result(cmd, replicas)
        if cmd[:4] == ["az", "containerapp", "revision", "deactivate"]:
            active_revisions.remove(cmd[cmd.index("--revision") + 1])
            return result(cmd)
        raise AssertionError(cmd)

    return execute, commands, samples


def test_guard_unsafe_durable_admission_blocks_without_deactivation():
    execute, commands, _ = guard_fake(safe=False)
    with pytest.raises(terraform_guard.DeploymentBlocked):
        terraform_guard.verify_drain(guard_env(), execute=execute, pause=lambda _: None)
    assert not any(c[:4] == ["az", "containerapp", "revision", "deactivate"] for c, _ in commands)


def test_guard_scheduled_work_blocks_deactivation_until_deadline():
    execute, commands, _ = guard_fake(counts=("0", "1", "0", "0"))
    now = [0.0]

    def pause(seconds):
        now[0] += seconds

    env = guard_env()
    env["PTU_TF_DRAIN_TIMEOUT_SECONDS"] = "3"
    with pytest.raises(terraform_guard.DeploymentBlocked):
        terraform_guard.verify_drain(env, execute=execute, pause=pause, now=lambda: now[0])
    assert not any(c[:4] == ["az", "containerapp", "revision", "deactivate"] for c, _ in commands)


def test_guard_rechecks_durable_pause_after_drain_before_deactivation():
    base_execute, commands, _ = guard_fake()
    checks = [0]

    def execute(cmd, *, interactive=False):
        if cmd[:3] == ["az", "containerapp", "exec"]:
            checks[0] += 1
            if checks[0] == 2:
                commands.append((list(cmd), interactive))
                return result(cmd, "PTU_BATCH_DRAIN_BLOCKED\n", code=1)
        return base_execute(cmd, interactive=interactive)

    with pytest.raises(terraform_guard.DeploymentBlocked):
        terraform_guard.verify_drain(guard_env(), execute=execute, pause=lambda _: None)
    assert checks[0] == 2
    assert not any(c[:4] == ["az", "containerapp", "revision", "deactivate"] for c, _ in commands)


def test_guard_retries_transient_control_exec_then_requires_safe_marker():
    base_execute, commands, samples = guard_fake()
    checks = [0]
    pauses = []

    def execute(cmd, *, interactive=False):
        if cmd[:3] == ["az", "containerapp", "exec"]:
            checks[0] += 1
            if checks[0] == 1:
                commands.append((list(cmd), interactive))
                raise ConnectionAbortedError("WebSocket closed")
        return base_execute(cmd, interactive=interactive)

    assert terraform_guard.verify_drain(guard_env(), execute=execute, pause=pauses.append)
    assert checks[0] == 3  # retry first check, then recheck after drain
    assert len(samples) == 8
    assert pauses.count(2) >= 1


def test_guard_retries_missing_safe_marker_then_accepts_exact_marker():
    base_execute, commands, samples = guard_fake()
    checks = [0]

    def execute(cmd, *, interactive=False):
        if cmd[:3] == ["az", "containerapp", "exec"]:
            checks[0] += 1
            if checks[0] == 1:
                commands.append((list(cmd), interactive))
                return result(cmd, "control response incomplete\n")
        return base_execute(cmd, interactive=interactive)

    assert terraform_guard.verify_drain(guard_env(), execute=execute, pause=lambda _: None)
    assert checks[0] == 3
    assert len(samples) == 8


def test_guard_exhausts_three_transport_failures_before_queue_checks():
    base_execute, commands, samples = guard_fake()
    checks = [0]
    pauses = []

    def execute(cmd, *, interactive=False):
        if cmd[:3] == ["az", "containerapp", "exec"]:
            checks[0] += 1
            commands.append((list(cmd), interactive))
            return result(cmd, stderr="ConnectionAbortedError", code=1)
        return base_execute(cmd, interactive=interactive)

    with pytest.raises(terraform_guard.DeploymentBlocked):
        terraform_guard.verify_drain(guard_env(), execute=execute, pause=pauses.append)
    assert checks[0] == 3
    assert pauses == [2, 2]
    assert samples == []
    assert not any(c[:4] == ["az", "containerapp", "revision", "deactivate"] for c, _ in commands)


@pytest.mark.parametrize("unsafe_output", ["PTU_BATCH_DRAIN_BLOCKED\n", '{"safe": false}\n'])
def test_guard_explicit_unsafe_status_never_retries(unsafe_output):
    base_execute, commands, samples = guard_fake()
    checks = [0]
    pauses = []

    def execute(cmd, *, interactive=False):
        if cmd[:3] == ["az", "containerapp", "exec"]:
            checks[0] += 1
            commands.append((list(cmd), interactive))
            return result(cmd, unsafe_output, code=1)
        return base_execute(cmd, interactive=interactive)

    with pytest.raises(terraform_guard.DeploymentBlocked):
        terraform_guard.verify_drain(guard_env(), execute=execute, pause=pauses.append)
    assert checks[0] == 1
    assert pauses == [] and samples == []
    assert not any(c[:4] == ["az", "containerapp", "revision", "deactivate"] for c, _ in commands)


def test_guard_two_zero_samples_deactivate_only_inference_revision():
    execute, commands, samples = guard_fake(revisions="oldrev\nolderrev", replicas="0")
    assert terraform_guard.verify_drain(guard_env(), execute=execute, pause=lambda _: None)
    assert len(samples) == 8
    deactivations = [
        c for c, _ in commands if c[:4] == ["az", "containerapp", "revision", "deactivate"]
    ]
    assert [c[c.index("--revision") + 1] for c in deactivations] == ["oldrev", "olderrev"]
    assert all(c[c.index("--name") + 1].endswith("-inference") for c in deactivations)
    assert all("--subscription" in c for c, _ in commands)
    assert any(
        interactive for c, interactive in commands if c[:3] == ["az", "containerapp", "exec"]
    )


def test_guard_no_active_revision_is_idempotent():
    execute, commands, samples = guard_fake(revisions="")
    assert terraform_guard.verify_drain(guard_env(), execute=execute, pause=lambda _: None)
    assert len(samples) == 8
    assert not any(c[:4] == ["az", "containerapp", "revision", "deactivate"] for c, _ in commands)
