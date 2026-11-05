"""Read-only ARM preflight for a configured provisioned Azure OpenAI deployment."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx
from azure.identity.aio import DefaultAzureCredential

from ptu_batch.adapters.azure import validated_deployment
from ptu_batch.errors import DeploymentPreflightError

_ARM_SCOPE = "https://management.azure.com/.default"
_ARM_ORIGIN = "https://management.azure.com"
_API_VERSION = "2025-06-01"
_CACHE_SECONDS = 300
_CAPABILITY_KEYS = {
    "chat/completions": ("chatCompletion", "chatCompletions", "chat_completions"),
    "responses": ("responses", "response"),
}


class AzurePreflight:
    """Check ARM configuration without mutating or probing live inference."""

    def __init__(self, credential=None, http_client=None):
        self._own_credential = credential is None
        self._credential = credential or DefaultAzureCredential()
        self._own_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=10)
        self._cache: dict[tuple[Any, ...], float] = {}

    async def check_account(self, account: Mapping[str, Any] | Any, force: bool = False) -> None:
        """Verify account identity and health without claiming deployment PTU eligibility."""
        if hasattr(account, "model_dump"):
            account = account.model_dump()
        if not isinstance(account, Mapping):
            raise RuntimeError("Invalid Azure account configuration")
        resource_id = account.get("resource_id")
        if not isinstance(resource_id, str):
            raise RuntimeError("Invalid Azure account resource_id")
        # Reuse the deployment validator's strict ARM ID and endpoint binding.
        probe_name = "__account_preflight__"
        try:
            endpoint, _, _, _ = validated_deployment(
                {
                    **account,
                    "resource_id": f"{resource_id}/deployments/{probe_name}",
                    "deployment_name": probe_name,
                }
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        account_name = resource_id.rsplit("/", 1)[-1]
        key = ("account", resource_id.casefold(), endpoint)
        if not force and self._cache.get(key, 0) > time.monotonic():
            return
        try:
            token = await self._credential.get_token(_ARM_SCOPE)
        except Exception as exc:
            raise RuntimeError(
                "Azure account ARM lookup failed: unable to obtain identity token"
            ) from exc
        try:
            response = await self._http.get(
                f"{_ARM_ORIGIN}{resource_id}",
                params={"api-version": _API_VERSION},
                headers={"Authorization": f"Bearer {token.token}"},
            )
            response.raise_for_status()
            arm = response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Azure account ARM lookup failed: HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Azure account ARM lookup failed") from exc
        if not isinstance(arm, dict):
            raise RuntimeError("Azure account ARM response is invalid")
        arm_id = arm.get("id")
        arm_name = arm.get("name")
        if (
            not isinstance(arm_id, str)
            or arm_id.casefold() != resource_id.casefold()
            or not isinstance(arm_name, str)
            or arm_name.casefold() != account_name.casefold()
        ):
            raise RuntimeError("ARM account does not match the configured target")
        properties = arm.get("properties")
        if not isinstance(properties, dict) or properties.get("provisioningState") != "Succeeded":
            raise RuntimeError("ARM account is not provisioned successfully")
        arm_endpoint = properties.get("endpoint")
        if (
            not isinstance(arm_endpoint, str)
            or arm_endpoint.rstrip("/").casefold() != endpoint.casefold()
        ):
            raise RuntimeError("ARM account endpoint does not match the configured target")
        self._cache[key] = time.monotonic() + _CACHE_SECONDS

    async def check(self, deployment: Mapping[str, Any], force: bool = False) -> None:
        try:
            endpoint, resource_id, name, allowed_apis = validated_deployment(deployment)
        except ValueError as exc:
            raise DeploymentPreflightError(str(exc)) from exc
        key = (resource_id.casefold(), endpoint, name.casefold(), tuple(sorted(allowed_apis)))
        if not force and self._cache.get(key, 0) > time.monotonic():
            return
        try:
            token = await self._credential.get_token(_ARM_SCOPE)
        except Exception as exc:
            raise RuntimeError(
                "Azure deployment ARM lookup failed: unable to obtain identity token"
            ) from exc
        try:
            response = await self._http.get(
                f"{_ARM_ORIGIN}{resource_id}",
                params={"api-version": _API_VERSION},
                headers={"Authorization": f"Bearer {token.token}"},
            )
            response.raise_for_status()
            arm = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise DeploymentPreflightError(
                    "Azure deployment ARM lookup failed: HTTP 404", code="model_not_found"
                ) from exc
            raise RuntimeError(
                f"Azure deployment ARM lookup failed: HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Azure deployment ARM lookup failed") from exc
        if not isinstance(arm, dict):
            raise RuntimeError("Azure deployment ARM response is invalid")
        arm_id = arm.get("id")
        arm_name = arm.get("name")
        if (
            not isinstance(arm_id, str)
            or arm_id.casefold() != resource_id.casefold()
            or not isinstance(arm_name, str)
            or arm_name.casefold() != name.casefold()
        ):
            raise DeploymentPreflightError("ARM deployment does not match the configured target")
        sku = arm.get("sku") or {}
        sku_name = sku.get("name") if isinstance(sku, dict) else None
        capacity = sku.get("capacity") if isinstance(sku, dict) else None
        provisioned_skus = {
            "provisionedmanaged",
            "globalprovisionedmanaged",
            "datazoneprovisionedmanaged",
        }
        if (
            not isinstance(sku_name, str)
            or sku_name.casefold() not in provisioned_skus
            or isinstance(capacity, bool)
            or not isinstance(capacity, (int, float))
            or capacity <= 0
        ):
            raise DeploymentPreflightError("ARM deployment is not a provisioned throughput SKU")
        properties = arm.get("properties") or {}
        if not isinstance(properties, dict):
            raise RuntimeError("ARM deployment properties are invalid")
        if properties.get("provisioningState") != "Succeeded":
            raise RuntimeError("ARM deployment is not provisioned successfully")
        if properties.get("spilloverDeploymentName"):
            raise DeploymentPreflightError("ARM deployment has spillover configured")
        capabilities = properties.get("capabilities")
        if capabilities is not None:
            if not isinstance(capabilities, dict):
                raise RuntimeError("ARM deployment capabilities are invalid")
            for api in allowed_apis:
                for capability_key in _CAPABILITY_KEYS[api]:
                    if capability_key in capabilities:
                        value = capabilities[capability_key]
                        if (
                            value is False
                            or value == 0
                            or (isinstance(value, str) and value.casefold() == "false")
                        ):
                            raise DeploymentPreflightError(
                                f"ARM deployment declares {api} incompatible"
                            )
        self._cache[key] = time.monotonic() + _CACHE_SECONDS

    async def close(self) -> None:
        if self._own_http:
            await self._http.aclose()
        if self._own_credential:
            await self._credential.close()
