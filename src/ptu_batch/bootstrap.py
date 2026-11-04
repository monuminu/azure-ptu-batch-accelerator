"""Explicit selection of Azure services or an isolated, volatile local demo."""

import os
import tempfile

from .runtime import Runtime
from .settings import Deployment, Settings


async def build_runtime(settings=None):
    settings = settings or Settings()
    if settings.mode == "local":
        from .memory import (
            EchoInference,
            LocalAuthenticator,
            LocalBlobStore,
            MemoryBroker,
            MemoryDocumentStore,
        )

        directory = tempfile.TemporaryDirectory(prefix="ptu-batch-local-")
        if not settings.deployments:
            target = Deployment(
                resource_endpoint="https://local.openai.azure.com/",
                deployment_name="local-echo",
                resource_id="/subscriptions/local/deployments/local-echo",
            )
            settings.deployments = {"demo": target, "ptu": target}
        return Runtime(
            settings,
            MemoryDocumentStore(),
            LocalBlobStore(directory.name),
            MemoryBroker(),
            EchoInference(),
            LocalAuthenticator(),
            cleanup=directory.cleanup,
        )
    required = [
        "tenant_id",
        "audience",
        "cosmos_endpoint",
        "storage_account_url",
        "servicebus_fqdn",
    ]
    missing = [key for key in required if not getattr(settings, key)]
    if missing or not settings.deployments:
        raise ValueError(
            "Azure configuration is incomplete: "
            + ", ".join(missing + ([] if settings.deployments else ["deployments"]))
        )
    if settings.request_lease_seconds <= settings.inference_timeout_seconds + 60:
        raise ValueError("Request lease must exceed inference timeout plus persistence margin")
    from azure.identity.aio import DefaultAzureCredential

    from .adapters.auth import EntraAuthenticator
    from .adapters.azure import (
        AzureBlobStore,
        AzureDocumentStore,
        AzureInferenceClient,
        ServiceBusBroker,
        validated_deployment,
    )
    from .adapters.preflight import AzurePreflight

    for deployment in settings.deployments.values():
        validated_deployment(deployment.model_dump())
    credential = DefaultAzureCredential(
        managed_identity_client_id=settings.managed_identity_client_id or None
    )
    runtime = Runtime(
        settings,
        AzureDocumentStore(
            settings.cosmos_endpoint,
            settings.cosmos_database,
            settings.cosmos_container,
            credential,
        ),
        AzureBlobStore(settings.storage_account_url, settings.blob_container, credential),
        ServiceBusBroker(settings.servicebus_fqdn, credential),
        AzureInferenceClient(credential, settings.inference_timeout_seconds),
        EntraAuthenticator(
            settings.tenant_id,
            settings.audience,
            settings.required_permission,
            application_role=settings.application_role,
        ),
        AzurePreflight(credential),
        credential=credential,
    )
    if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(enable_live_metrics=False)
    return runtime
