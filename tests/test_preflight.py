"""Read-only ARM preflight checks with a mocked HTTP transport."""

import httpx
import pytest

from ptu_batch.adapters.preflight import AzurePreflight

RESOURCE_ID = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/deployments/ptu"
DEPLOYMENT = {
    "resource_id": RESOURCE_ID,
    "resource_endpoint": "https://acct.openai.azure.com/",
    "deployment_name": "ptu",
    "allowed_apis": ["chat/completions", "responses"],
}


class TokenCredential:
    def __init__(self):
        self.scopes = []

    async def get_token(self, scope):
        self.scopes.append(scope)
        return type("Token", (), {"token": "arm-token"})()


def response(*, sku="ProvisionedManaged", spillover=None):
    properties = {"provisioningState": "Succeeded", "model": {"name": "gpt-x"}}
    if spillover is not None:
        properties["spilloverDeploymentName"] = spillover
    return {
        "id": RESOURCE_ID,
        "name": "ptu",
        "sku": {"name": sku, "capacity": 10},
        "properties": properties,
    }


@pytest.mark.asyncio
async def test_preflight_reads_arm_once_and_caches_short_check():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=response())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    credential = TokenCredential()
    preflight = AzurePreflight(credential=credential, http_client=http)
    await preflight.check(DEPLOYMENT)
    await preflight.check(DEPLOYMENT)
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == RESOURCE_ID
    assert requests[0].url.params["api-version"] == "2025-06-01"
    assert credential.scopes == ["https://management.azure.com/.default"]
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", [response(sku="Standard"), response(spillover="fallback")])
async def test_preflight_rejects_wrong_sku_or_spillover(arm):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=arm)))
    preflight = AzurePreflight(credential=TokenCredential(), http_client=http)
    with pytest.raises(RuntimeError):
        await preflight.check(DEPLOYMENT)
    await http.aclose()


@pytest.mark.asyncio
async def test_preflight_rejects_untrusted_resource_endpoint_before_arm_call():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    preflight = AzurePreflight(credential=TokenCredential(), http_client=http)
    with pytest.raises(RuntimeError):
        await preflight.check(
            {**DEPLOYMENT, "resource_endpoint": "https://acct.openai.azure.com/path?key=secret"}
        )
    assert calls == []
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("sku", ["GlobalProvisionedManaged", "DataZoneProvisionedManaged"])
async def test_preflight_accepts_all_provisioned_sku_families(sku):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response(sku=sku)))
    )
    preflight = AzurePreflight(credential=TokenCredential(), http_client=http)
    await preflight.check(DEPLOYMENT)
    await http.aclose()


@pytest.mark.asyncio
async def test_preflight_rejects_declared_api_incompatibility():
    arm = response()
    arm["properties"]["capabilities"] = {"chatCompletion": "FALSE"}
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=arm)))
    preflight = AzurePreflight(credential=TokenCredential(), http_client=http)
    with pytest.raises(RuntimeError, match="incompatible"):
        await preflight.check(DEPLOYMENT)
    await http.aclose()


@pytest.mark.asyncio
async def test_preflight_force_refreshes_cached_arm_result():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    preflight = AzurePreflight(credential=TokenCredential(), http_client=http)
    await preflight.check(DEPLOYMENT)
    await preflight.check(DEPLOYMENT, force=True)
    assert len(calls) == 2
    await http.aclose()


@pytest.mark.asyncio
async def test_preflight_surfaces_identity_failure_as_runtime_error():
    class BrokenCredential:
        async def get_token(self, _):
            raise RuntimeError("credential unavailable")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response()))
    )
    preflight = AzurePreflight(credential=BrokenCredential(), http_client=http)
    with pytest.raises(RuntimeError, match="Azure deployment ARM lookup failed"):
        await preflight.check(DEPLOYMENT)
    await http.aclose()


@pytest.mark.asyncio
async def test_preflight_accepts_configured_leading_slash_allowed_apis():
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response()))
    )
    preflight = AzurePreflight(credential=TokenCredential(), http_client=http)
    await preflight.check({**DEPLOYMENT, "allowed_apis": ["/chat/completions", "/responses"]})
    await http.aclose()
