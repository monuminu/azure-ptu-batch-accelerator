mock_provider "azurerm" {
  mock_data "azurerm_client_config" {
    defaults = {
      tenant_id       = "11111111-1111-1111-1111-111111111111"
      object_id       = "22222222-2222-2222-2222-222222222222"
      subscription_id = "33333333-3333-3333-3333-333333333333"
    }
  }
}
mock_provider "azapi" {
  mock_data "azapi_resource" {
    defaults = {
      output = {
        sku = {
          name     = "ProvisionedManaged"
          capacity = 10
        }
        properties = {
          provisioningState       = "Succeeded"
          spilloverDeploymentName = null
        }
      }
    }
  }
}
mock_provider "azuread" {}
mock_provider "random" {}

variables {
  subscription_id            = "33333333-3333-3333-3333-333333333333"
  api_app_id                 = "44444444-4444-4444-4444-444444444444"
  ptu_deployment_resource_id = "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg-existing/providers/Microsoft.CognitiveServices/accounts/aoai-existing/deployments/ptu-existing"
  ptu_resource_endpoint      = "https://example.openai.azure.com/"
  ptu_deployment_name        = "ptu-existing"
  build_images               = false
  assign_runtime_ptu_roles   = false
}

run "native_apps_and_safe_defaults" {
  command = plan

  assert {
    condition     = azapi_resource.control.body.kind == "functionapp" && azapi_resource.inference.body.kind == "functionapp"
    error_message = "Both Functions apps must be native Microsoft.App/containerApps kind=functionapp resources."
  }
  assert {
    condition     = azapi_resource.api.body.properties.template.scale.minReplicas == 0 && azapi_resource.api.body.properties.template.scale.maxReplicas == 2 && azapi_resource.control.body.properties.template.scale.minReplicas == 1 && azapi_resource.control.body.properties.template.scale.maxReplicas == 1 && azapi_resource.inference.body.properties.template.scale.minReplicas == 0 && azapi_resource.inference.body.properties.template.scale.maxReplicas == 1
    error_message = "API, control, and inference scale bounds must preserve the tested architecture."
  }
  assert {
    condition     = azapi_resource.payload.body.properties.allowSharedKeyAccess == false && azapi_resource.control_host.body.properties.allowSharedKeyAccess == false && azapi_resource.inference_host.body.properties.allowSharedKeyAccess == false
    error_message = "Storage must use managed identity, with shared keys disabled."
  }
  assert {
    condition     = azapi_resource.payload.type == "Microsoft.Storage/storageAccounts@2023-05-01" && azapi_resource.control_host.type == "Microsoft.Storage/storageAccounts@2023-05-01" && azapi_resource.inference_host.type == "Microsoft.Storage/storageAccounts@2023-05-01" && azapi_resource.payload.body.kind == "StorageV2" && azapi_resource.payload.body.sku.name == "Standard_LRS" && azapi_resource.payload.body.properties.defaultToOAuthAuthentication == true
    error_message = "All three storage accounts must use native AzAPI control-plane resources with OAuth defaults."
  }
  assert {
    condition     = length(azurerm_cosmosdb_sql_container.ledger.partition_key_paths) == 1 && contains(azurerm_cosmosdb_sql_container.ledger.partition_key_paths, "/pk") && length(azurerm_cosmosdb_sql_container.leases.partition_key_paths) == 1 && contains(azurerm_cosmosdb_sql_container.leases.partition_key_paths, "/id")
    error_message = "Ledger and leases must have the runtime's partition keys."
  }
  assert {
    condition     = azurerm_servicebus_queue.control.lock_duration == "PT5M" && azurerm_servicebus_queue.control.default_message_ttl == "P14D" && azurerm_servicebus_queue.control.duplicate_detection_history_time_window == "P7D" && azurerm_servicebus_queue.control.max_delivery_count == 10
    error_message = "Control queue reliability settings differ from the production contract."
  }
  assert {
    condition     = azurerm_servicebus_queue.inference.lock_duration == "PT5M" && azurerm_servicebus_queue.inference.default_message_ttl == "P14D" && azurerm_servicebus_queue.inference.duplicate_detection_history_time_window == "P7D" && azurerm_servicebus_queue.inference.max_delivery_count == 10
    error_message = "Inference queue reliability settings differ from the production contract."
  }
  assert {
    condition     = azapi_resource.api.body.properties.configuration.ingress.external == true && azapi_resource.control.body.properties.configuration.ingress.external == false && azapi_resource.inference.body.properties.configuration.ingress.external == false
    error_message = "Only the API may have external ingress."
  }
  assert {
    condition     = azapi_resource.api.body.properties.configuration.ingress.transport == "Auto" && azapi_resource.control.body.properties.configuration.ingress.transport == "Auto" && azapi_resource.inference.body.properties.configuration.ingress.transport == "Auto"
    error_message = "All ingress transports must use Azure's canonical Auto value."
  }
  assert {
    condition     = azapi_resource.control.type == "Microsoft.App/containerApps@2026-01-01" && azapi_resource.inference.type == "Microsoft.App/containerApps@2026-01-01" && azapi_resource.batch_files.type == "Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01"
    error_message = "Native Container Apps and control-plane Blob container types are required."
  }
  assert {
    condition     = azurerm_cosmosdb_account.main.local_authentication_enabled == false && azurerm_servicebus_namespace.main.local_auth_enabled == false && azurerm_application_insights.main.local_authentication_enabled == false && azurerm_container_registry.main.admin_enabled == false
    error_message = "Support services must disable local or admin credentials."
  }
  assert {
    condition     = contains([for env in local.common_env : env.name], "PTU_BATCH_APPLICATION_ROLE") && contains([for env in local.function_env : env.name], "WEBSITES_ENABLE_APP_SERVICE_STORAGE") && contains([for env in local.function_env : env.name], "ServiceBusConnection__credential")
    error_message = "Application role and native Functions managed-identity settings are required."
  }
  assert {
    condition     = contains([for env in local.common_env : env.name], "PTU_BATCH_DEPLOYMENTS") && !contains([for env in local.common_env : env.name], "PTU_BATCH_DEPLOYMENT_ACCOUNT")
    error_message = "Legacy deployment references must keep their original runtime environment contract."
  }
  assert {
    condition     = azurerm_role_assignment.existing_ptu == {} && length(azurerm_role_assignment.runtime) == 12
    error_message = "The offline plan must skip existing-PTU grants but retain support-service identity grants."
  }
  assert {
    condition     = azapi_resource.payload.body.properties.publicNetworkAccess == "Disabled" && azapi_resource.control_host.body.properties.publicNetworkAccess == "Disabled" && azapi_resource.inference_host.body.properties.publicNetworkAccess == "Disabled" && azurerm_cosmosdb_account.main.public_network_access_enabled == false
    error_message = "Private networking must disable public Storage and Cosmos data access."
  }
  assert {
    condition     = length(azurerm_private_endpoint.data) == 8 && length(azurerm_private_dns_zone.private) == 4 && length(azurerm_private_dns_zone_virtual_network_link.private) == 4 && azurerm_container_app_environment.main.name == "cae-ptubatchtf-ptu-batch-vnet"
    error_message = "Private networking requires eight endpoints, four DNS zones and links, and a distinct ACA environment."
  }
  assert {
    condition     = alltrue([for key in ["payload_blob", "control_blob", "control_queue", "control_table", "inference_blob", "inference_queue", "inference_table", "cosmos_sql"] : contains(keys(azurerm_private_endpoint.data), key)]) && contains(azurerm_private_endpoint.data["cosmos_sql"].private_service_connection[0].subresource_names, "Sql") && azurerm_private_dns_zone.private["cosmos"].name == "privatelink.documents.azure.com" && azurerm_private_dns_zone.private["blob"].name == "privatelink.blob.core.windows.net" && azurerm_private_dns_zone.private["queue"].name == "privatelink.queue.core.windows.net" && azurerm_private_dns_zone.private["table"].name == "privatelink.table.core.windows.net"
    error_message = "Private endpoints and DNS zones must cover payload Blob, both hosts' Blob/Queue/Table, and Cosmos SQL."
  }
  assert {
    condition     = contains(azurerm_subnet.apps[0].address_prefixes, "10.248.0.0/23") && contains(azurerm_subnet.private_endpoints[0].address_prefixes, "10.248.2.0/24") && azurerm_subnet.private_endpoints[0].private_endpoint_network_policies == "Disabled"
    error_message = "The default VNet must reserve separate /23 app and /24 private endpoint subnets."
  }
  assert {
    condition     = try(contains(azurerm_subnet.apps[0].delegation[0].service_delegation[0].actions, "Microsoft.Network/virtualNetworks/subnets/join/action"), false)
    error_message = "The ACA subnet must declare Azure's canonical delegation join action to prevent perpetual drift."
  }
}

run "reject_non_deployment_ptu_id" {
  command = plan
  variables {
    ptu_deployment_resource_id = "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg-existing/providers/Microsoft.CognitiveServices/accounts/aoai-existing"
  }
  expect_failures = [var.ptu_deployment_resource_id]
}

run "owned_registration_and_image_build" {
  command = plan
  variables {
    api_app_id              = null
    create_api_registration = true
    preauthorize_azure_cli  = false
    build_images            = true
  }
  assert {
    condition     = length(azuread_application.api) == 1 && length(azuread_service_principal.api) == 1 && azuread_application.api[0].api[0].requested_access_token_version == 2 && length(azuread_application_identifier_uri.api) == 1 && azuread_application_permission_scope.delegated[0].value == "Batch.ReadWrite" && azuread_application_app_role.application[0].value == "Batch.ReadWrite.Application"
    error_message = "Owned API registration must expose distinct delegated and application permissions."
  }
  assert {
    condition     = length(azuread_application_pre_authorized.azure_cli) == 0 && length(terraform_data.images) == 1 && length(azurerm_role_assignment.builder_acr_push) == 1
    error_message = "Images must build in the graph and Azure CLI preauthorization must remain opt-in."
  }
}

run "stable_public_cloud_blob_origin" {
  command = plan
  override_resource {
    target          = random_string.suffix
    override_during = plan
    values = {
      result = "abc12345"
    }
  }
  assert {
    condition     = one([for env in local.common_env : env.value if env.name == "PTU_BATCH_STORAGE_ACCOUNT_URL"]) == "https://stptubatchabc12345.blob.core.windows.net/"
    error_message = "The Blob endpoint must be a stable Azure public-cloud origin derived from the account name."
  }
}

run "public_networking_opt_out" {
  command = plan
  variables {
    private_networking = false
  }
  assert {
    condition     = length(azurerm_private_endpoint.data) == 0 && length(azurerm_private_dns_zone.private) == 0 && length(azurerm_virtual_network.main) == 0 && azapi_resource.payload.body.properties.publicNetworkAccess == "Enabled" && azurerm_cosmosdb_account.main.public_network_access_enabled == true && azurerm_container_app_environment.main.name == "cae-ptubatchtf-ptu-batch"
    error_message = "Public opt-out must omit private endpoints and retain the original environment name."
  }
}

run "reject_invalid_image_tag_before_apply" {
  command = plan
  variables {
    image_tag = "latest"
  }
  expect_failures = [var.image_tag]
}

run "reject_standard_deployment_in_production" {
  command = plan
  override_data {
    target = data.azapi_resource.existing_deployment[0]
    values = {
      output = {
        sku = {
          name     = "Standard"
          capacity = 20
        }
        properties = {
          provisioningState = "Succeeded"
        }
      }
    }
  }
  expect_failures = [terraform_data.contract]
}

run "validation_only_accepts_existing_standard_for_paused_smoke" {
  command = plan
  variables {
    validation_only          = true
    assign_runtime_ptu_roles = true
  }
  override_data {
    target = data.azapi_resource.existing_deployment[0]
    values = {
      output = {
        sku = {
          name     = "Standard"
          capacity = 20
        }
        properties = {
          provisioningState = "Succeeded"
        }
      }
    }
  }
  assert {
    condition     = output.validation_only == true && strcontains(output.validation_only_warning, "admission paused")
    error_message = "Validation-only mode must report that admission remains paused."
  }
  assert {
    condition     = length(azurerm_role_assignment.existing_ptu) == 0
    error_message = "Validation-only mode must not alter access on a non-PTU account."
  }
}

run "cosmos_region_can_differ_from_compute_region" {
  command = plan
  variables {
    location        = "eastus"
    cosmos_location = "swedencentral"
  }
  assert {
    condition     = azurerm_resource_group.main.location == "eastus" && azurerm_container_app_environment.main.location == "eastus" && azurerm_cosmosdb_account.main.location == "swedencentral" && contains([for geo in azurerm_cosmosdb_account.main.geo_location : geo.location], "swedencentral")
    error_message = "Only Cosmos should move to the configured Cosmos region."
  }
}

run "validation_only_rejects_batch_sku" {
  command = plan
  variables {
    validation_only = true
  }
  override_data {
    target = data.azapi_resource.existing_deployment[0]
    values = {
      output = {
        sku = {
          name     = "GlobalBatch"
          capacity = 20
        }
        properties = {
          provisioningState = "Succeeded"
        }
      }
    }
  }
  expect_failures = [terraform_data.contract]
}

run "account_only_uses_jsonl_model_without_legacy_deployment_map" {
  command = plan
  variables {
    openai_account_resource_id = "/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/rg-existing/providers/Microsoft.CognitiveServices/accounts/aoai-existing"
    openai_resource_endpoint   = "https://aoai-existing.cognitiveservices.azure.com/"
    ptu_deployment_resource_id = null
    ptu_resource_endpoint      = null
    ptu_deployment_name        = null
    assign_runtime_ptu_roles   = true
  }
  override_data {
    target = data.azapi_resource.existing_account[0]
    values = {
      output = {
        id   = "/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/rg-existing/providers/Microsoft.CognitiveServices/accounts/aoai-existing"
        name = "aoai-existing"
        properties = {
          provisioningState = "Succeeded"
          endpoint          = "https://aoai-existing.cognitiveservices.azure.com/"
        }
      }
    }
  }
  assert {
    condition     = !contains([for env in local.common_env : env.name], "PTU_BATCH_DEPLOYMENTS") && contains([for env in local.common_env : env.name], "PTU_BATCH_DEPLOYMENT_ACCOUNT")
    error_message = "Account mode must emit account configuration without a legacy per-deployment map."
  }
  assert {
    condition     = jsondecode(one([for env in local.common_env : env.value if env.name == "PTU_BATCH_DEPLOYMENT_ACCOUNT"])).resource_id == var.openai_account_resource_id && jsondecode(one([for env in local.common_env : env.value if env.name == "PTU_BATCH_DEPLOYMENT_ACCOUNT"])).resource_endpoint == var.openai_resource_endpoint && toset(jsondecode(one([for env in local.common_env : env.value if env.name == "PTU_BATCH_DEPLOYMENT_ACCOUNT"])).allowed_apis) == toset(var.allowed_apis) && length(keys(jsondecode(one([for env in local.common_env : env.value if env.name == "PTU_BATCH_DEPLOYMENT_ACCOUNT"])))) == 4
    error_message = "Account configuration must leave deployment resolution to each JSONL model value."
  }
  assert {
    condition     = output.deployment_resolution == "jsonl_model" && output.openai_account_resource_id == var.openai_account_resource_id && output.openai_resource_endpoint == var.openai_resource_endpoint
    error_message = "Account-only output must identify the account and JSONL routing mode."
  }
  assert {
    condition     = toset(keys(azurerm_role_assignment.existing_ptu)) == toset(["openai_user", "reader"]) && alltrue([for role in values(azurerm_role_assignment.existing_ptu) : role.scope == var.openai_account_resource_id])
    error_message = "The existing runtime role addresses must grant account-level access in JSONL routing mode."
  }
}

run "account_only_rejects_legacy_fields" {
  command = plan
  variables {
    openai_account_resource_id = "/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/rg-existing/providers/Microsoft.CognitiveServices/accounts/aoai-existing"
    openai_resource_endpoint   = "https://aoai-existing.cognitiveservices.azure.com/"
  }
  expect_failures = [var.openai_account_resource_id]
}

run "reject_missing_account_and_deployment_references" {
  command = plan
  variables {
    ptu_deployment_resource_id = null
    ptu_resource_endpoint      = null
    ptu_deployment_name        = null
  }
  expect_failures = [var.openai_account_resource_id]
}

run "account_only_rejects_validation_only" {
  command = plan
  variables {
    openai_account_resource_id = "/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/rg-existing/providers/Microsoft.CognitiveServices/accounts/aoai-existing"
    openai_resource_endpoint   = "https://aoai-existing.cognitiveservices.azure.com/"
    ptu_deployment_resource_id = null
    ptu_resource_endpoint      = null
    ptu_deployment_name        = null
    validation_only            = true
  }
  expect_failures = [terraform_data.contract]
}
