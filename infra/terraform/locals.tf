locals {
  source_root          = abspath("${path.module}/../..")
  name_prefix          = substr(replace(var.environment_name, "-", ""), 0, 8)
  suffix               = random_string.suffix.result
  payload_storage_name = "st${local.name_prefix}${local.suffix}"
  resource_group       = "rg-${var.environment_name}-ptu-batch"
  api_name             = "ca-${var.environment_name}-ptu-api"
  control_name         = "ca-${var.environment_name}-ptu-control"
  inference_name       = "ca-${var.environment_name}-ptu-inference"
  tenant_id            = var.tenant_id == null ? data.azurerm_client_config.current.tenant_id : var.tenant_id
  own_api_app          = var.api_app_id == null && var.create_api_registration
  effective_api_app_id = local.own_api_app ? azuread_application.api[0].client_id : var.api_app_id
  account_mode         = var.openai_account_resource_id != null
  ptu_account_id       = local.account_mode ? var.openai_account_resource_id : try(join("/", slice(split("/", var.ptu_deployment_resource_id), 0, 9)), null)
  ptu_subscription_id  = local.ptu_account_id == null ? var.subscription_id : split("/", local.ptu_account_id)[2]
  effective_endpoint   = local.account_mode ? var.openai_resource_endpoint : var.ptu_resource_endpoint

  source_files = sort(concat(
    ["Dockerfile.api", "Dockerfile.control", "Dockerfile.inference", ".dockerignore", "pyproject.toml", "uv.lock", "README.md", "LICENSE"],
    [for file in fileset(local.source_root, "src/**") : file if !endswith(file, ".pyc") && !strcontains(file, "__pycache__")],
    [for file in fileset(local.source_root, "functions/**") : file if !endswith(file, ".pyc") && !strcontains(file, "__pycache__")]
  ))
  source_fingerprint  = sha256(join("\n", [for file in local.source_files : "${file}:${filesha256("${local.source_root}/${file}")}"]))
  effective_image_tag = var.image_tag == null ? substr(local.source_fingerprint, 0, 12) : var.image_tag

  legacy_deployments = {
    for alias in(local.account_mode ? [] : [var.ptu_alias]) : alias => {
      resource_endpoint = var.ptu_resource_endpoint
      deployment_name   = var.ptu_deployment_name
      resource_id       = var.ptu_deployment_resource_id
      allowed_apis      = var.allowed_apis
      allowed_subjects  = var.allowed_subjects
    }
  }
  deployment_account = local.account_mode ? {
    resource_id       = var.openai_account_resource_id
    resource_endpoint = var.openai_resource_endpoint
    allowed_apis      = var.allowed_apis
    allowed_subjects  = var.allowed_subjects
  } : null
  # Existing application_guard uses this snapshot as its update fingerprint.
  deployments = {
    account = local.deployment_account
    legacy  = local.legacy_deployments
  }

  common_env = concat([
    { name = "PTU_BATCH_MODE", value = "azure" },
    { name = "PTU_BATCH_TENANT_ID", value = local.tenant_id },
    { name = "PTU_BATCH_AUDIENCE", value = local.effective_api_app_id },
    { name = "PTU_BATCH_REQUIRED_PERMISSION", value = "Batch.ReadWrite" },
    { name = "PTU_BATCH_APPLICATION_ROLE", value = "Batch.ReadWrite.Application" },
    ], local.account_mode ? [
    { name = "PTU_BATCH_DEPLOYMENT_ACCOUNT", value = jsonencode(local.deployment_account) }
    ] : [
    { name = "PTU_BATCH_DEPLOYMENTS", value = jsonencode(local.legacy_deployments) }
    ], [
    { name = "PTU_BATCH_COSMOS_ENDPOINT", value = azurerm_cosmosdb_account.main.endpoint },
    { name = "PTU_BATCH_COSMOS_DATABASE", value = "ptu-batch" },
    { name = "PTU_BATCH_COSMOS_CONTAINER", value = "ledger" },
    { name = "PTU_BATCH_COSMOS_LEASE_CONTAINER", value = "leases" },
    # Azure public-cloud Blob DNS is stable across storage policy updates.
    { name = "PTU_BATCH_STORAGE_ACCOUNT_URL", value = "https://${local.payload_storage_name}.blob.core.windows.net/" },
    { name = "PTU_BATCH_BLOB_CONTAINER", value = "batch-files" },
    { name = "PTU_BATCH_SERVICEBUS_FQDN", value = "${azurerm_servicebus_namespace.main.name}.servicebus.windows.net" },
    { name = "PTU_BATCH_CONTROL_QUEUE", value = azurerm_servicebus_queue.control.name },
    { name = "PTU_BATCH_INFERENCE_QUEUE", value = azurerm_servicebus_queue.inference.name },
    { name = "PTU_BATCH_MANAGED_IDENTITY_CLIENT_ID", value = azurerm_user_assigned_identity.runtime.client_id },
    { name = "PTU_BATCH_INFERENCE_CONCURRENCY", value = "8" },
    { name = "PTU_BATCH_INFERENCE_TIMEOUT_SECONDS", value = "300" },
    { name = "PTU_BATCH_REQUEST_LEASE_SECONDS", value = "660" },
    { name = "APPLICATIONINSIGHTS_CONNECTION_STRING", value = azurerm_application_insights.main.connection_string },
    { name = "APPLICATIONINSIGHTS_AUTHENTICATION_STRING", value = "Authorization=AAD;ClientId=${azurerm_user_assigned_identity.runtime.client_id}" }
  ])

  function_env = [
    { name = "FUNCTIONS_EXTENSION_VERSION", value = "~4" },
    { name = "FUNCTIONS_WORKER_RUNTIME", value = "python" },
    { name = "FUNCTIONS_WORKER_PROCESS_COUNT", value = "1" },
    { name = "WEBSITES_ENABLE_APP_SERVICE_STORAGE", value = "false" },
    { name = "ServiceBusConnection__fullyQualifiedNamespace", value = "${azurerm_servicebus_namespace.main.name}.servicebus.windows.net" },
    { name = "ServiceBusConnection__credential", value = "managedidentity" },
    { name = "ServiceBusConnection__clientId", value = azurerm_user_assigned_identity.runtime.client_id },
    { name = "CosmosConnection__accountEndpoint", value = azurerm_cosmosdb_account.main.endpoint },
    { name = "CosmosConnection__credential", value = "managedidentity" },
    { name = "CosmosConnection__clientId", value = azurerm_user_assigned_identity.runtime.client_id }
  ]

  image_references = {
    api       = "${azurerm_container_registry.main.login_server}/ptu-batch-api:${local.effective_image_tag}"
    control   = "${azurerm_container_registry.main.login_server}/ptu-batch-control:${local.effective_image_tag}"
    inference = "${azurerm_container_registry.main.login_server}/ptu-batch-inference:${local.effective_image_tag}"
  }
}

data "azurerm_client_config" "current" {}

# Read-only ARM GET: confirms the referenced deployment exists and inspects its actual SKU.
data "azapi_resource" "existing_deployment" {
  count                  = local.account_mode ? 0 : 1
  type                   = "Microsoft.CognitiveServices/accounts/deployments@2025-06-01"
  resource_id            = var.ptu_deployment_resource_id
  response_export_values = ["*"]
  ignore_not_found       = false
}

# Account mode reads only the existing Azure OpenAI account. Runtime resolves
# and validates each JSONL model deployment before submitting work.
data "azapi_resource" "existing_account" {
  count                  = local.account_mode ? 1 : 0
  type                   = "Microsoft.CognitiveServices/accounts@2025-06-01"
  resource_id            = var.openai_account_resource_id
  response_export_values = ["*"]
  ignore_not_found       = false
}

resource "random_string" "suffix" {
  length     = 8
  lower      = true
  upper      = false
  numeric    = true
  special    = false
  depends_on = [terraform_data.contract]
}

resource "terraform_data" "contract" {
  input = local.account_mode ? var.openai_account_resource_id : var.ptu_deployment_resource_id

  lifecycle {
    precondition {
      condition = local.account_mode ? (
        var.openai_resource_endpoint != null &&
        var.ptu_deployment_resource_id == null && var.ptu_deployment_name == null && var.ptu_resource_endpoint == null &&
        !var.validation_only
        ) : (
        var.openai_resource_endpoint == null &&
        var.ptu_deployment_resource_id != null && var.ptu_deployment_name != null && var.ptu_resource_endpoint != null
      )
      error_message = "Provide either an existing account ID and origin for JSONL model routing or all three legacy deployment fields; do not combine modes or use validation_only with account routing."
    }
    precondition {
      condition     = local.account_mode || try(lower(split("/", var.ptu_deployment_resource_id)[10]) == lower(var.ptu_deployment_name), false)
      error_message = "ptu_deployment_name must match the final segment of ptu_deployment_resource_id."
    }
    precondition {
      condition     = !local.account_mode || try(lower(split("/", var.openai_account_resource_id)[2]) == lower(var.subscription_id), false)
      error_message = "The existing account must be in the requested subscription."
    }
    precondition {
      condition     = var.api_app_id != null || var.create_api_registration
      error_message = "Supply api_app_id or allow this module to create the API registration."
    }
    precondition {
      condition     = local.tenant_id == data.azurerm_client_config.current.tenant_id
      error_message = "The deployment tenant must match the signed-in Azure tenant."
    }
    precondition {
      condition     = local.account_mode || contains(["ProvisionedManaged", "GlobalProvisionedManaged", "DataZoneProvisionedManaged"], try(data.azapi_resource.existing_deployment[0].output.sku.name, "")) || (var.validation_only && try(data.azapi_resource.existing_deployment[0].output.sku.name, "") == "Standard")
      error_message = "Production mode requires an existing provisioned PTU deployment. A Standard deployment is permitted only with validation_only=true."
    }
    precondition {
      condition     = local.account_mode || try(tonumber(data.azapi_resource.existing_deployment[0].output.sku.capacity), 0) > 0
      error_message = "The existing deployment must have positive SKU capacity."
    }
    precondition {
      condition     = local.account_mode || lower(try(data.azapi_resource.existing_deployment[0].output.properties.provisioningState, "")) == "succeeded"
      error_message = "The existing deployment provisioning state must be Succeeded."
    }
    precondition {
      condition     = local.account_mode || try(trimspace(data.azapi_resource.existing_deployment[0].output.properties.spilloverDeploymentName), "") == ""
      error_message = "Spillover must be disabled on the existing deployment."
    }
    precondition {
      condition     = !local.account_mode || try(lower(data.azapi_resource.existing_account[0].output.id) == lower(var.openai_account_resource_id) && lower(data.azapi_resource.existing_account[0].output.name) == lower(split("/", var.openai_account_resource_id)[8]), false)
      error_message = "Existing account ARM ID and name must match the requested account."
    }
    precondition {
      condition     = !local.account_mode || try(lower(data.azapi_resource.existing_account[0].output.properties.provisioningState) == "succeeded", false)
      error_message = "Existing account provisioning state must be Succeeded."
    }
    precondition {
      condition     = !local.account_mode || try(lower(trimsuffix(data.azapi_resource.existing_account[0].output.properties.endpoint, "/")) == lower(trimsuffix(var.openai_resource_endpoint, "/")), false)
      error_message = "Existing account endpoint must match openai_resource_endpoint."
    }
  }
}
