"""Account-scoped routing keeps JSONL deployment names inside a trusted account."""

import json

import httpx
import pytest
from openai import AsyncOpenAI
from test_engine import Clock, drain, row, submit
from test_engine import rig as rig

from ptu_batch.adapters.azure import AzureInferenceClient
from ptu_batch.adapters.preflight import AzurePreflight
from ptu_batch.api import create_app
from ptu_batch.bootstrap import build_runtime
from ptu_batch.engine import Engine
from ptu_batch.memory import LocalAuthenticator, LocalBlobStore, MemoryBroker, MemoryDocumentStore
from ptu_batch.ports import Mutation, Principal
from ptu_batch.runtime import Runtime
from ptu_batch.settings import Settings

ACCOUNT_ID = (
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/customer"
)
ACCOUNT = {
    "resource_endpoint": "https://customer.cognitiveservices.azure.com/",
    "resource_id": ACCOUNT_ID,
    "allowed_apis": ["/chat/completions", "/responses"],
    "allowed_subjects": ["alice"],
}


def configured_account(values=None):
    settings = Settings(mode="local", deployments={}, deployment_account=values or ACCOUNT)
    account = getattr(settings, "deployment_account", None)
    assert account is not None, "Account configuration must not be discarded"
    return account


def test_actual_deployment_name_resolves_without_an_alias_mapping():
    account = configured_account()
    deployment = account.resolve("customer-global-ptu-v2")
    assert deployment.deployment_name == "customer-global-ptu-v2"
    assert deployment.resource_id == ACCOUNT_ID + "/deployments/customer-global-ptu-v2"
    assert deployment.resource_endpoint == "https://customer.cognitiveservices.azure.com/"
    assert deployment.allowed_apis == ["/chat/completions", "/responses"]
    assert deployment.allowed_subjects == ["alice"]
    assert deployment.token_scope == "https://cognitiveservices.azure.com/.default"


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "../other",
        "https://other.openai.azure.com/deployments/other",
        "deployments/other",
        "other?api-version=1",
        "other#fragment",
        "other%2Fname",
        "other\\name",
        " other",
        "other\nname",
        "x" * 65,
    ],
)
def test_jsonl_deployment_name_cannot_change_resource_path(name):
    with pytest.raises(ValueError, match="deployment name|Deployment name"):
        configured_account().resolve(name)


@pytest.mark.parametrize(
    "override",
    [
        {"resource_endpoint": "https://other.openai.azure.com/"},
        {"resource_endpoint": "https://customer.openai.azure.com.evil.example/"},
        {"resource_endpoint": "http://customer.openai.azure.com/"},
        {"resource_endpoint": "https://customer.openai.azure.com/openai/v1/"},
        {"resource_endpoint": "https://customer.openai.azure.com/?api-key=secret"},
        {"resource_endpoint": "https://user@customer.openai.azure.com/"},
        {"resource_id": ACCOUNT_ID + "/deployments/already-a-deployment"},
        {"resource_id": ACCOUNT_ID + "?api-version=1"},
        {"allowed_apis": ["/embeddings"]},
    ],
)
def test_account_configuration_rejects_untrusted_origins_and_nonaccount_ids(override):
    with pytest.raises(ValueError):
        Settings(mode="local", deployments={}, deployment_account={**ACCOUNT, **override})


class TokenCredential:
    async def get_token(self, _):
        return type("Token", (), {"token": "test-token"})()

    async def close(self):
        return None


@pytest.fixture
async def account_rig(tmp_path):
    settings = Settings(
        mode="local",
        local_autorun=False,
        deployments={},
        deployment_account={**ACCOUNT, "allowed_subjects": ["alice", "developer"]},
    )
    observed = {
        "arm": [],
        "inference": [],
        "sku": "GlobalProvisionedManaged",
        "not_found": False,
        "capabilities": {},
        "spillover": None,
    }

    def arm_response(request):
        observed["arm"].append(request)
        if observed.get("network_error"):
            raise httpx.ConnectError("connection unavailable", request=request)
        if observed["not_found"]:
            return httpx.Response(404, json={"error": {"code": "DeploymentNotFound"}})
        if observed.get("http_status"):
            return httpx.Response(observed["http_status"], json={"error": {"code": "busy"}})
        return httpx.Response(
            200,
            json={
                "id": request.url.path,
                "name": request.url.path.rsplit("/", 1)[-1],
                "sku": {"name": observed["sku"], "capacity": 10},
                "properties": {
                    "provisioningState": "Succeeded",
                    "capabilities": observed["capabilities"],
                    "spilloverDeploymentName": observed["spillover"],
                },
            },
        )

    def inference_response(request):
        observed["inference"].append(request)
        return httpx.Response(
            200,
            json={
                "id": "remote-model-result",
                "object": "response"
                if request.url.path.endswith("responses")
                else "chat.completion",
                "model": json.loads(request.content)["model"],
                "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            },
            headers={"x-ms-request-id": "remote-request"},
        )

    arm_http = httpx.AsyncClient(transport=httpx.MockTransport(arm_response))
    inference_http = httpx.AsyncClient(transport=httpx.MockTransport(inference_response))
    runtime = Runtime(
        settings,
        MemoryDocumentStore(),
        LocalBlobStore(tmp_path),
        MemoryBroker(),
        AzureInferenceClient(TokenCredential(), http_client=inference_http),
        LocalAuthenticator(),
        AzurePreflight(TokenCredential(), http_client=arm_http),
        clock=Clock(),
    )
    try:
        yield runtime, Engine(runtime), Principal("local", "alice"), observed
    finally:
        await runtime.close()
        await arm_http.aclose()
        await inference_http.aclose()


async def test_batch_uses_unmapped_actual_name_and_resolved_target(account_rig):
    rt, engine, principal, observed = account_rig
    _, batch = await submit(engine, principal, [row(model="customer-global-ptu-v2")])
    await drain(rt, engine)
    status = await engine.get_batch(principal, batch["id"])
    assert status["status"] == "completed"
    assert rt.settings.deployments == {}
    stored = await rt.store.get(batch["id"], batch["id"])
    assert stored["deployment_account_snapshot"]["resource_id"] == ACCOUNT_ID
    assert stored["target"]["resource_id"] == ACCOUNT_ID + "/deployments/customer-global-ptu-v2"
    assert observed["arm"][0].url.path == ACCOUNT_ID + "/deployments/customer-global-ptu-v2"
    assert len(observed["inference"]) == 1
    assert json.loads(observed["inference"][0].content)["model"] == "customer-global-ptu-v2"
    assert observed["inference"][0].url.host == "customer.cognitiveservices.azure.com"


async def test_submission_account_snapshot_survives_configuration_change(account_rig):
    rt, engine, principal, observed = account_rig
    _, batch = await submit(engine, principal, [row(model="customer-global-ptu-v2")])
    rt.settings.deployment_account = configured_account(
        {
            "resource_endpoint": "https://replacement.openai.azure.com/",
            "resource_id": ACCOUNT_ID.replace("/customer", "/replacement"),
            "allowed_apis": ["/responses"],
            "allowed_subjects": ["different-owner"],
        }
    )
    await drain(rt, engine)
    assert (await engine.get_batch(principal, batch["id"]))["status"] == "completed"
    assert observed["arm"][0].url.path == ACCOUNT_ID + "/deployments/customer-global-ptu-v2"
    assert observed["inference"][0].url.host == "customer.cognitiveservices.azure.com"


@pytest.mark.parametrize("api", ["/chat/completions", "/responses"])
async def test_official_sdk_routes_jsonl_actual_deployment_without_server_aliases(account_rig, api):
    rt, engine, _, observed = account_rig
    request = row("sdk-row", url="/v1" + api, model="unmapped-actual-ptu")
    if api == "/responses":
        request["body"] = {"model": "unmapped-actual-ptu", "input": "hello"}
    app = create_app(rt)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as transport:
            async with AsyncOpenAI(
                base_url="http://test/openai/v1/",
                api_key="local-dev-token",
                http_client=transport,
                max_retries=0,
            ) as client:
                uploaded = await client.files.create(
                    file=("requests.jsonl", json.dumps(request).encode()), purpose="batch"
                )
                batch = await client.batches.create(
                    input_file_id=uploaded.id,
                    endpoint="/chat/completions",
                    completion_window="24h",
                )
                await drain(rt, engine)
                completed = await client.batches.retrieve(batch.id)
                assert completed.status == "completed"
                content = await client.files.content(completed.output_file_id)
                result = json.loads(content.text)
                assert result["custom_id"] == "sdk-row"
                assert result["response"]["body"]["id"] == "remote-model-result"
                assert result["response"]["body"]["model"] == "unmapped-actual-ptu"
                assert rt.settings.deployments == {}
                assert observed["inference"][0].url.path == "/openai/v1" + api


@pytest.mark.parametrize(
    ("requests", "account_override", "expected_code"),
    [
        ([row(model="one"), row("b", model="two")], {}, "invalid_request_error"),
        ([row(model="one"), row("b", model="../two")], {}, "invalid_request_error"),
        ([row(model="one")], {"allowed_subjects": ["other"]}, "permission_denied"),
        ([row(model="one")], {"allowed_apis": ["/responses"]}, "invalid_request_error"),
    ],
)
async def test_invalid_or_unauthorized_account_rows_never_reach_arm_or_inference(
    account_rig, requests, account_override, expected_code
):
    rt, engine, principal, observed = account_rig
    if account_override:
        rt.settings.deployment_account = configured_account({**ACCOUNT, **account_override})
    _, batch = await submit(engine, principal, requests)
    await drain(rt, engine)
    state = await engine.get_batch(principal, batch["id"])
    assert state["status"] == "failed"
    assert state["errors"]["data"][0]["code"] == expected_code
    assert observed["arm"] == []
    assert observed["inference"] == []


async def test_late_invalid_row_stages_without_dispatch_or_arm_lookup(account_rig):
    rt, engine, principal, observed = account_rig
    requests = [row(str(i), model="actual-ptu") for i in range(41)]
    requests.append(row("invalid", model="other?deployment=external"))
    _, batch = await submit(engine, principal, requests)
    await drain(rt, engine)
    assert (await engine.get_batch(principal, batch["id"]))["status"] == "failed"
    assert observed["arm"] == []
    assert observed["inference"] == []
    assert any([request async for request in rt.store.scan(pk=batch["id"], kind="request")])


@pytest.mark.parametrize("sku", ["Standard", "GlobalStandard", "DataZoneStandard"])
async def test_actual_name_still_requires_ptu_before_any_request_is_dispatched(account_rig, sku):
    rt, engine, principal, observed = account_rig
    observed["sku"] = sku
    _, batch = await submit(engine, principal, [row(model="actual-standard-deployment")])
    await drain(rt, engine)
    state = await engine.get_batch(principal, batch["id"])
    assert state["status"] == "failed"
    assert state["errors"]["data"][0]["code"] == "invalid_deployment"
    assert state["errors"]["data"][0]["param"] == "body.model"
    assert len(observed["arm"]) == 1
    assert observed["inference"] == []
    requests = [request async for request in rt.store.scan(pk=batch["id"], kind="request")]
    assert requests and all(request["state"] == "staged" for request in requests)


async def test_nonexistent_deployment_is_blocked_by_arm_before_dispatch(account_rig):
    rt, engine, principal, observed = account_rig
    observed["not_found"] = True
    _, batch = await submit(engine, principal, [row(model="not-in-azure")])
    await drain(rt, engine)
    state = await engine.get_batch(principal, batch["id"])
    assert state["status"] == "failed"
    assert state["errors"]["data"][0]["code"] == "model_not_found"
    assert state["errors"]["data"][0]["param"] == "body.model"
    assert len(observed["arm"]) == 1
    assert observed["inference"] == []


@pytest.mark.parametrize(
    "rejection",
    [{"spillover": "standard-backend"}, {"capabilities": {"chatCompletion": False}}],
)
async def test_definite_spillover_or_api_rejection_fails_batch_terminally(account_rig, rejection):
    rt, engine, principal, observed = account_rig
    observed.update(rejection)
    _, batch = await submit(engine, principal, [row(model="ineligible-ptu")])
    await drain(rt, engine)
    state = await engine.get_batch(principal, batch["id"])
    assert state["status"] == "failed"
    assert state["errors"]["data"][0]["code"] == "invalid_deployment"
    assert state["errors"]["data"][0]["param"] == "body.model"
    assert observed["inference"] == []


@pytest.mark.parametrize(
    "transient",
    [{"http_status": 429}, {"http_status": 503}, {"network_error": True}],
)
async def test_transient_preflight_failure_preserves_batch_for_recovery(account_rig, transient):
    rt, engine, principal, observed = account_rig
    observed.update(transient)
    _, batch = await submit(engine, principal, [row(model="actual-ptu")])
    with pytest.raises(RuntimeError, match="ARM lookup failed"):
        await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    assert (await engine.get_batch(principal, batch["id"]))["status"] == "validating"
    assert observed["inference"] == []
    observed.pop("http_status", None)
    observed.pop("network_error", None)
    rt.clock.value += rt.settings.control_lease_seconds + 1
    await engine.handle_control({"kind": "validate", "batch_id": batch["id"]})
    await drain(rt, engine)
    assert (await engine.get_batch(principal, batch["id"]))["status"] == "completed"


@pytest.mark.parametrize(
    ("api", "capabilities"),
    [
        ("/chat/completions", {"chatCompletion": True, "responses": False}),
        ("/responses", {"chatCompletion": False, "responses": True}),
    ],
)
async def test_resolved_target_preflights_only_the_files_normalized_api(
    account_rig, api, capabilities
):
    rt, engine, principal, observed = account_rig
    observed["capabilities"] = capabilities
    request = row(url="/v1" + api, model="single-api-ptu")
    if api == "/responses":
        request["body"] = {"model": "single-api-ptu", "input": "hello"}
    _, batch = await submit(engine, principal, [request])
    await drain(rt, engine)
    assert (await engine.get_batch(principal, batch["id"]))["status"] == "completed"
    job = await rt.store.get(batch["id"], batch["id"])
    assert job["target"]["allowed_apis"] == [api]
    assert observed["inference"][0].url.path == "/openai/v1" + api


async def test_account_routing_takes_precedence_over_a_legacy_alias(account_rig):
    rt, engine, principal, observed = account_rig
    rt.settings.deployments["actual-ptu"] = configured_account().resolve("legacy-backend")
    _, batch = await submit(engine, principal, [row(model="actual-ptu")])
    await drain(rt, engine)
    assert (await engine.get_batch(principal, batch["id"]))["status"] == "completed"
    assert json.loads(observed["inference"][0].content)["model"] == "actual-ptu"


async def test_legacy_job_without_account_snapshot_retains_original_alias(rig):
    rt, engine, principal = rig
    _, batch = await submit(engine, principal, [row()])
    job = await rt.store.get(batch["id"], batch["id"])
    job.pop("deployment_account_snapshot", None)
    await rt.store.apply(job["pk"], [Mutation("replace", job["id"], job, job["_etag"])])
    rt.settings.deployment_account = configured_account()
    rt.settings.deployments = {}
    await drain(rt, engine)
    assert (await engine.get_batch(principal, batch["id"]))["status"] == "completed"
    assert rt.inference.calls[0][0]["deployment_name"] == "existing-ptu"


def azure_settings():
    return Settings(
        mode="azure",
        tenant_id="00000000-0000-0000-0000-000000000001",
        audience="00000000-0000-0000-0000-000000000002",
        cosmos_endpoint="https://ledger.documents.azure.com:443/",
        storage_account_url="https://files.blob.core.windows.net/",
        servicebus_fqdn="jobs.servicebus.windows.net",
        deployments={},
        deployment_account=ACCOUNT,
    )


async def test_azure_runtime_accepts_account_configuration_without_alias_map(monkeypatch):
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr("azure.identity.aio.DefaultAzureCredential", lambda **_: TokenCredential())
    runtime = await build_runtime(azure_settings())
    try:
        assert isinstance(runtime.inference, AzureInferenceClient)
        assert runtime.settings.deployments == {}
        assert runtime.settings.deployment_account.resource_id == ACCOUNT_ID
    finally:
        await runtime.close()


async def test_bootstrap_revalidates_account_before_constructing_azure_clients(monkeypatch):
    credentials = []
    monkeypatch.setattr(
        "azure.identity.aio.DefaultAzureCredential", lambda **_: credentials.append("constructed")
    )
    settings = azure_settings()
    settings.deployment_account.resource_endpoint = "https://untrusted.example.com/"
    with pytest.raises(ValueError, match="matching public Azure account"):
        await build_runtime(settings)
    assert credentials == []
