"""Validate Microsoft Entra access tokens for a single configured tenant and app."""

from __future__ import annotations

import asyncio
import re
import time

import httpx
import jwt

from ptu_batch.errors import AuthError
from ptu_batch.ports import Principal

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_MAX_JWKS_AGE = 300
_UNKNOWN_KID_REFRESH_INTERVAL = 30


class EntraAuthenticator:
    """Fixed-issuer RS256 JWT verification with bounded JWKS caching."""

    def __init__(
        self,
        tenant_id: str,
        audience: str,
        required_permission: str = "Batch.ReadWrite",
        http_client=None,
        *,
        application_role: str | None = None,
    ):
        if not _GUID.fullmatch(tenant_id) or not _GUID.fullmatch(audience):
            raise ValueError("tenant_id and audience must be GUIDs")
        if not required_permission:
            raise ValueError("required_permission is required")
        self.tenant_id = tenant_id.lower()
        self.audience = audience.lower()
        self.required_permission = required_permission
        self.application_role = application_role
        self._issuer = f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"
        self._jwks_url = f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"
        self._http = http_client or httpx.AsyncClient(timeout=10)
        self._own_http = http_client is None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires_at = 0.0
        self._refresh_lock = asyncio.Lock()
        self._next_forced_refresh_at = 0.0

    async def _refresh_keys(self) -> None:
        try:
            response = await self._http.get(self._jwks_url)
            response.raise_for_status()
            data = response.json()
            keys = data["keys"]
            if not isinstance(keys, list) or not 1 <= len(keys) <= 128:
                raise ValueError("Invalid JWKS keys")
            parsed: dict[str, jwt.PyJWK] = {}
            for entry in keys:
                if (
                    not isinstance(entry, dict)
                    or entry.get("kty") != "RSA"
                    or entry.get("use", "sig") != "sig"
                    or entry.get("alg", "RS256") != "RS256"
                ):
                    continue
                kid = entry.get("kid")
                if isinstance(kid, str) and kid:
                    parsed[kid] = jwt.PyJWK.from_dict(entry, algorithm="RS256")
            if not parsed:
                raise ValueError("No valid RS256 JWKS keys")
            age = _MAX_JWKS_AGE
            cache_control = response.headers.get("cache-control", "")
            if match := re.search(r"(?:^|,)\s*max-age=(\d+)", cache_control):
                age = min(int(match.group(1)), _MAX_JWKS_AGE)
            self._keys = parsed
            self._expires_at = time.monotonic() + age
        except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError) as exc:
            raise AuthError("Unable to validate bearer token signing key") from exc

    async def authenticate(self, bearer: str) -> Principal:
        if not isinstance(bearer, str):
            raise AuthError()
        token = bearer[7:] if bearer.lower().startswith("bearer ") else bearer
        if not token or len(token) > 65536:
            raise AuthError()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError() from exc
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not kid
            or "jku" in header
            or "x5u" in header
        ):
            raise AuthError("Unsupported bearer token header")
        refreshed = False
        if time.monotonic() >= self._expires_at or not self._keys:
            async with self._refresh_lock:
                if time.monotonic() >= self._expires_at or not self._keys:
                    await self._refresh_keys()
            refreshed = True
        key = self._keys.get(kid)
        if key is None and not refreshed:
            async with self._refresh_lock:
                key = self._keys.get(kid)
                now = time.monotonic()
                if key is None and now >= self._next_forced_refresh_at:
                    # Admit the first rotation miss, then bound unknown-kid traffic.
                    self._next_forced_refresh_at = now + _UNKNOWN_KID_REFRESH_INTERVAL
                    await self._refresh_keys()
                    key = self._keys.get(kid)
        if key is None:
            raise AuthError("Unknown bearer token signing key")
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self._issuer,
                options={"require": ["iss", "aud", "exp", "nbf", "iat", "tid", "oid"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError() from exc
        tenant = claims.get("tid")
        oid = claims.get("oid")
        if (
            not isinstance(tenant, str)
            or tenant.lower() != self.tenant_id
            or not isinstance(oid, str)
            or not _GUID.fullmatch(oid)
        ):
            raise AuthError("Bearer token tenant or object ID is invalid")
        scopes = claims.get("scp", "")
        roles = claims.get("roles", [])
        if not isinstance(scopes, str) or not isinstance(roles, list):
            raise AuthError("Bearer token permissions are invalid")
        delegated = set(scopes.split())
        application = {role for role in roles if isinstance(role, str)}
        permissions = delegated | application
        permitted = self.required_permission in permissions or (
            self.application_role is not None and self.application_role in application
        )
        if not permitted:
            raise AuthError(
                "Required application permission is missing",
                status=403,
                code="insufficient_permissions",
            )
        return Principal(
            tenant_id=self.tenant_id, subject_id=oid.lower(), roles=frozenset(permissions)
        )

    async def close(self) -> None:
        if self._own_http:
            await self._http.aclose()
