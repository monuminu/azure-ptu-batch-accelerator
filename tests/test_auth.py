"""Entra bearer validation against a fixed tenant's JWKS endpoint."""

import base64
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ptu_batch.adapters.auth import EntraAuthenticator
from ptu_batch.errors import AuthError

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
AUDIENCE = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def keypair(kid):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()

    def encoded(value):
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return private, {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": encoded(numbers.n),
        "e": encoded(numbers.e),
    }


def token(
    private,
    kid,
    *,
    tenant=TENANT,
    audience=AUDIENCE,
    expires=300,
    scope="Batch.ReadWrite",
    role=None,
):
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": audience,
        "iat": now,
        "nbf": now - 1,
        "exp": now + expires,
        "tid": tenant,
        "oid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
    }
    if scope is not None:
        claims["scp"] = scope
    if role is not None:
        claims["roles"] = [role]
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": kid})


@pytest.mark.asyncio
async def test_auth_accepts_scoped_or_app_role_token_and_fixed_jwks_url():
    private, jwk = keypair("k1")
    requests = []

    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"keys": [jwk]}, headers={"cache-control": "max-age=300"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    auth = EntraAuthenticator(TENANT, AUDIENCE, http_client=http)
    scoped = await auth.authenticate(token(private, "k1"))
    app = await auth.authenticate(token(private, "k1", scope=None, role="Batch.ReadWrite"))
    assert scoped.owner == app.owner == f"{TENANT}:cccccccc-cccc-cccc-cccc-cccccccccccc"
    assert "Batch.ReadWrite" in scoped.roles
    assert "Batch.ReadWrite" in app.roles
    assert requests == [f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"]
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"tenant": "dddddddd-dddd-dddd-dddd-dddddddddddd"},
        {"audience": "wrong-app"},
        {"expires": -1},
        {"scope": "Other.Permission"},
    ],
)
async def test_auth_rejects_wrong_tenant_audience_expiry_or_permission(override):
    private, jwk = keypair("k1")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"keys": [jwk]}))
    )
    auth = EntraAuthenticator(TENANT, AUDIENCE, http_client=http)
    with pytest.raises(AuthError):
        await auth.authenticate(token(private, "k1", **override))
    await http.aclose()


@pytest.mark.asyncio
async def test_auth_refreshes_unknown_kid_once_for_rotation():
    old_private, old_jwk = keypair("old")
    new_private, new_jwk = keypair("new")
    calls = 0

    def handle(_):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"keys": [old_jwk if calls == 1 else new_jwk]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    auth = EntraAuthenticator(TENANT, AUDIENCE, http_client=http)
    await auth.authenticate(token(old_private, "old"))
    principal = await auth.authenticate(token(new_private, "new"))
    assert principal.subject_id == "cccccccc-cccc-cccc-cccc-cccccccccccc"
    assert calls == 2
    await http.aclose()


@pytest.mark.parametrize("permission_source", ["application", "legacy", "scope"])
async def test_auth_distinct_application_role_and_delegated_scope(permission_source):
    private, jwk = keypair("role-key")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"keys": [jwk]}))
    ) as http:
        auth = EntraAuthenticator(
            TENANT, AUDIENCE, http_client=http, application_role="Batch.ReadWrite.Application"
        )
        options = {"scope": None, "role": "Batch.ReadWrite.Application"}
        if permission_source == "legacy":
            options["role"] = "Batch.ReadWrite"
        elif permission_source == "scope":
            options = {"scope": "Batch.ReadWrite"}
        principal = await auth.authenticate(token(private, "role-key", **options))
        assert principal.subject_id == "cccccccc-cccc-cccc-cccc-cccccccccccc"


async def test_application_role_value_is_not_accepted_as_delegated_scope():
    private, jwk = keypair("role-key")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"keys": [jwk]}))
    ) as http:
        auth = EntraAuthenticator(
            TENANT, AUDIENCE, http_client=http, application_role="Batch.ReadWrite.Application"
        )
        with pytest.raises(AuthError) as error:
            await auth.authenticate(token(private, "role-key", scope="Batch.ReadWrite.Application"))
        assert error.value.status == 403
