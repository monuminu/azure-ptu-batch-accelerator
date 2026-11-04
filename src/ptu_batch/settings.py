"""Validated environment configuration. Azure is the fail-closed default."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PTU_BATCH_", extra="ignore")
    mode: Literal["azure", "local"] = "azure"
    tenant_id: str = ""
    audience: str = ""
    required_permission: str = "Batch.ReadWrite"
    application_role: str = "Batch.ReadWrite.Application"
    deployments: dict[str, Deployment] = Field(default_factory=dict)
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
