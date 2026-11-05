"""Validated environment configuration. Azure is the fail-closed default."""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ACCOUNT_ID = re.compile(
    r"/subscriptions/([^/?#\\]+)/resourceGroups/([^/?#\\]+)/providers/"
    r"Microsoft\.CognitiveServices/accounts/([a-z0-9][a-z0-9-]*)",
    re.IGNORECASE,
)
_ACCOUNT_HOST = re.compile(
    r"[a-z0-9][a-z0-9-]*\.(?:openai\.azure\.com|cognitiveservices\.azure\.com|services\.ai\.azure\.com)",
    re.IGNORECASE,
)
_DEPLOYMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class Deployment(BaseModel):
    resource_endpoint: str
    deployment_name: str
    resource_id: str
    allowed_apis: list[str] = Field(default_factory=lambda: ["/chat/completions", "/responses"])
    allowed_subjects: list[str] = Field(default_factory=list)
    token_scope: str = "https://cognitiveservices.azure.com/.default"

    @field_validator("allowed_apis")
    @classmethod
    def supported_apis(cls, values):
        if not values or set(values) - {"/chat/completions", "/responses"}:
            raise ValueError("Only chat completions and stateless Responses are supported")
        return values


class DeploymentAccount(BaseModel):
    """An operator-configured account; JSONL selects only a literal deployment name."""

    resource_endpoint: str
    resource_id: str
    allowed_apis: list[str] = Field(default_factory=lambda: ["/chat/completions", "/responses"])
    allowed_subjects: list[str] = Field(default_factory=list)
    token_scope: str = "https://cognitiveservices.azure.com/.default"

    @field_validator("allowed_apis")
    @classmethod
    def supported_apis(cls, values):
        if not values or set(values) - {"/chat/completions", "/responses"}:
            raise ValueError("Only chat completions and stateless Responses are supported")
        return values

    @model_validator(mode="after")
    def trusted_account(self):
        match = _ACCOUNT_ID.fullmatch(self.resource_id)
        if not match:
            raise ValueError("Invalid Azure account resource_id")
        url = urlsplit(self.resource_endpoint)
        if (
            any(character.isspace() for character in self.resource_endpoint)
            or url.scheme != "https"
            or url.username
            or url.password
            or url.port is not None
            or url.query
            or url.fragment
            or url.path not in ("", "/")
            or not url.hostname
            or not _ACCOUNT_HOST.fullmatch(url.hostname)
            or url.hostname.split(".", 1)[0].casefold() != match.group(3).casefold()
        ):
            raise ValueError("Resource endpoint must be the matching public Azure account hostname")
        return self

    def resolve(self, name: str) -> Deployment:
        self.trusted_account()
        if not isinstance(name, str) or not _DEPLOYMENT_NAME.fullmatch(name):
            raise ValueError(
                "Deployment name must be 1-64 letters, digits, underscores, hyphens or dots, "
                "starting with a letter or digit"
            )
        return Deployment.model_validate(
            {
                **self.model_dump(),
                "resource_id": f"{self.resource_id}/deployments/{name}",
                "deployment_name": name,
            }
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PTU_BATCH_", extra="ignore")
    mode: Literal["azure", "local"] = "azure"
    tenant_id: str = ""
    audience: str = ""
    required_permission: str = "Batch.ReadWrite"
    application_role: str = "Batch.ReadWrite.Application"
    deployments: dict[str, Deployment] = Field(default_factory=dict)
    deployment_account: DeploymentAccount | None = None
    cosmos_endpoint: str = ""
    cosmos_database: str = "ptu-batch"
    cosmos_container: str = "ledger"
    cosmos_lease_container: str = "leases"
    storage_account_url: str = ""
    blob_container: str = "batch-files"
    servicebus_fqdn: str = ""
    control_queue: str = "batch-control"
    inference_queue: str = "batch-inference"
    managed_identity_client_id: str = ""
    inference_concurrency: int = Field(default=8, ge=1, le=256)
    inference_timeout_seconds: int = Field(default=300, ge=1, le=480)
    request_lease_seconds: int = Field(default=660, ge=60)
    control_lease_seconds: int = Field(default=660, ge=60)
    cancellation_drain_seconds: int = Field(default=600, ge=1)
    submission_timeout_seconds: int = Field(default=120, ge=1, le=240)
    max_file_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    max_requests: int = Field(default=100000, ge=1, le=100000)
    local_autorun: bool = True
