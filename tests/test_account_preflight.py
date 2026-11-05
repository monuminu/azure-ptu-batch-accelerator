"""Read-only account preflight: authentication and account health, not PTU eligibility."""

import httpx
import pytest

from ptu_batch.adapters.preflight import AzurePreflight

ACCOUNT_ID = (
    "/subscriptions/11111111-2222-4333-8444-555555555555/resourceGroups/rg-model/"
    "providers/Microsoft.CognitiveServices/accounts/customer"
)
ACCOUNT = {
    "resource_id": ACCOUNT_ID,
    "resource_endpoint": "https://customer.openai.azure.com/",
    "allowed_apis": ["/chat/completions", "/responses"],
}


class TokenCredential:
    def __init__(self):
        self.scopes = []

    async def get_token(self, scope):
        self.scopes.append(scope)
        return type("Token", (), {"token": "arm-token"})()


def account_response(**changes):
    arm = {
        "id": ACCOUNT_ID,
        "name": "customer",
        "sku": {"name": "S0"},
        "properties": {
            "provisioningState": "Succeeded",
            "endpoint": "https://customer.openai.azure.com/",
        },
    }
    arm.update(changes)
    return arm


@pytest.mark.asyncio
async def test_account_preflight_reads_only_account_arm_url_and_caches_separately():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=account_response())

    credential = TokenCredential()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    preflight = AzurePreflight(credential=credential, http_client=http)
    await preflight.check_account(ACCOUNT)
    await preflight.check_account(ACCOUNT)
    await preflight.check_account(ACCOUNT, force=True)
    assert len(requests) == 2
    assert all(request.method == "GET" and request.url.path == ACCOUNT_ID for request in requests)
    assert all(request.url.params["api-version"] == "2025-06-01" for request in requests)
    assert credential.scopes == ["https://management.azure.com/.default"] * 2
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arm",
    [
        account_response(id=ACCOUNT_ID + "/deployments/other"),
        account_response(name="other"),
        account_response(
            properties={"provisioningState": "Creating", "endpoint": ACCOUNT["resource_endpoint"]}
        ),
        account_response(
            properties={
                "provisioningState": "Succeeded",
                "endpoint": "https://other.openai.azure.com/",
            }
        ),
    ],
)
async def test_account_preflight_rejects_mismatched_or_unready_account(arm):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=arm)))
    preflight = AzurePreflight(credential=TokenCredential(), http_client=http)
    with pytest.raises(RuntimeError):
        await preflight.check_account(ACCOUNT)
    await http.aclose()


@pytest.mark.asyncio
async def test_account_preflight_rejects_invalid_config_before_arm_call():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=account_response())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    preflight = AzurePreflight(credential=TokenCredential(), http_client=http)
    with pytest.raises((ValueError, RuntimeError)):
        await preflight.check_account({**ACCOUNT, "resource_id": ACCOUNT_ID + "/deployments/other"})
    with pytest.raises((ValueError, RuntimeError)):
        await preflight.check_account(
            {**ACCOUNT, "resource_endpoint": "https://customer.openai.azure.com/path?token=x"}
        )
    assert requests == []
    await http.aclose()


@pytest.mark.asyncio
async def test_account_preflight_fails_closed_on_unauthorized_get():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403)))
    preflight = AzurePreflight(credential=TokenCredential(), http_client=http)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        await preflight.check_account(ACCOUNT)
    await http.aclose()
