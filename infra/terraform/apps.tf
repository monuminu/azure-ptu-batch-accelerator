resource "terraform_data" "images" {
  count = var.build_images ? 1 : 0

  triggers_replace = sha256(jsonencode({
    source   = local.source_fingerprint
    registry = azurerm_container_registry.main.login_server
    tag      = local.effective_image_tag
  }))

  provisioner "local-exec" {
    working_dir = local.source_root
    command     = "python3 \"${local.source_root}/scripts/terraform_images.py\""
    environment = {
      PTU_TF_SUBSCRIPTION_ID       = var.subscription_id
      PTU_TF_RESOURCE_GROUP        = azurerm_resource_group.main.name
      PTU_TF_REGISTRY_NAME         = azurerm_container_registry.main.name
      PTU_TF_REGISTRY_LOGIN_SERVER = azurerm_container_registry.main.login_server
      PTU_TF_SOURCE_ROOT           = local.source_root
      PTU_TF_IMAGE_TAG             = local.effective_image_tag
      PTU_TF_PIP_INDEX_URL         = var.pip_index_url
    }
  }

  depends_on = [azurerm_role_assignment.builder_acr_push]
}

# The helper drains and deactivates an existing inference revision before any app update.
resource "terraform_data" "application_guard" {
  triggers_replace = sha256(jsonencode({
    source             = local.source_fingerprint
    images             = local.image_references
    audience           = local.effective_api_app_id
    ptu                = local.deployments
    container_api      = var.container_apps_api_version
    private_networking = var.private_networking
    tenant             = local.tenant_id
    resource_group     = azurerm_resource_group.main.name
  }))

  provisioner "local-exec" {
    working_dir = local.source_root
    command     = "python3 \"${local.source_root}/scripts/terraform_guard.py\""
    environment = {
      PTU_TF_SUBSCRIPTION_ID      = var.subscription_id
      PTU_TF_RESOURCE_GROUP       = azurerm_resource_group.main.name
      PTU_TF_CONTROL_NAME         = local.control_name
      PTU_TF_INFERENCE_NAME       = local.inference_name
      PTU_TF_API_NAME             = local.api_name
      PTU_TF_SERVICEBUS_NAMESPACE = azurerm_servicebus_namespace.main.name
    }
  }

  depends_on = [terraform_data.images]
}

# 2026-01-01 native Functions kind is newer than AzAPI's bundled schema.
# Azure validates the submitted control-plane body; no deployment template wrapper is used.
resource "azapi_resource" "api" {
  type                      = "Microsoft.App/containerApps@${var.container_apps_api_version}"
  name                      = local.api_name
  parent_id                 = azurerm_resource_group.main.id
  location                  = azurerm_resource_group.main.location
  schema_validation_enabled = false
  response_export_values    = ["properties.configuration.ingress.fqdn"]
  tags = {
    "azd-service-name" = "api"
    "azd-env-name"     = var.environment_name
  }
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runtime.id]
  }
  body = {
    properties = {
      environmentId       = azurerm_container_app_environment.main.id
      workloadProfileName = "Consumption"
      configuration = {
        activeRevisionsMode = "Single"
        registries = [{
          server   = azurerm_container_registry.main.login_server
          identity = azurerm_user_assigned_identity.runtime.id
        }]
        ingress = {
          external      = true
          targetPort    = 8000
          transport     = "Auto"
          allowInsecure = false
        }
      }
      template = {
        containers = [{
          name  = "api"
          image = local.image_references.api
          resources = {
            cpu    = 1
            memory = "2Gi"
          }
          env = concat(local.common_env, [{ name = "PTU_BATCH_IMAGE_SOURCE_FINGERPRINT", value = local.source_fingerprint }])
        }]
        scale = {
          minReplicas = 0
          maxReplicas = 2
        }
      }
    }
  }
  lifecycle {
    replace_triggered_by = [azurerm_container_app_environment.main.id]
  }
  depends_on = [
    terraform_data.application_guard,
    azurerm_private_endpoint.data,
    azurerm_private_dns_zone_virtual_network_link.private,
    azapi_resource.batch_files,
    azurerm_cosmosdb_sql_container.ledger,
    azurerm_cosmosdb_sql_container.leases,
    azurerm_cosmosdb_sql_role_assignment.runtime,
    azurerm_role_assignment.runtime,
    azurerm_role_assignment.existing_ptu,
  ]
}

resource "azapi_resource" "control" {
  type                      = "Microsoft.App/containerApps@${var.container_apps_api_version}"
  name                      = local.control_name
  parent_id                 = azurerm_resource_group.main.id
  location                  = azurerm_resource_group.main.location
  schema_validation_enabled = false
  tags = {
    "azd-service-name" = "control"
    "azd-env-name"     = var.environment_name
  }
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runtime.id]
  }
  body = {
    kind = "functionapp"
    properties = {
      environmentId       = azurerm_container_app_environment.main.id
      workloadProfileName = "Consumption"
      configuration = {
        activeRevisionsMode = "Single"
        registries = [{
          server   = azurerm_container_registry.main.login_server
          identity = azurerm_user_assigned_identity.runtime.id
        }]
        ingress = {
          external      = false
          targetPort    = 80
          transport     = "Auto"
          allowInsecure = false
        }
      }
      template = {
        containers = [{
          name  = "control"
          image = local.image_references.control
          resources = {
            cpu    = 1
            memory = "2Gi"
          }
          env = concat(local.common_env, local.function_env, [
            { name = "AzureWebJobsStorage__accountName", value = azapi_resource.control_host.name },
            { name = "AzureWebJobsStorage__credential", value = "managedidentity" },
            { name = "AzureWebJobsStorage__clientId", value = azurerm_user_assigned_identity.runtime.client_id },
            { name = "PTU_BATCH_IMAGE_SOURCE_FINGERPRINT", value = local.source_fingerprint },
          ])
        }]
        # Cosmos change feed with managed identity needs a live control replica.
        scale = {
          minReplicas = 1
          maxReplicas = 1
        }
      }
    }
  }
  lifecycle {
    replace_triggered_by = [azurerm_container_app_environment.main.id]
  }
  depends_on = [
    terraform_data.application_guard,
    azurerm_private_endpoint.data,
    azurerm_private_dns_zone_virtual_network_link.private,
    azapi_resource.batch_files,
    azurerm_cosmosdb_sql_container.ledger,
    azurerm_cosmosdb_sql_container.leases,
    azurerm_cosmosdb_sql_role_assignment.runtime,
    azurerm_role_assignment.runtime,
    azurerm_role_assignment.existing_ptu,
  ]
}

resource "azapi_resource" "inference" {
  type                      = "Microsoft.App/containerApps@${var.container_apps_api_version}"
  name                      = local.inference_name
  parent_id                 = azurerm_resource_group.main.id
  location                  = azurerm_resource_group.main.location
  schema_validation_enabled = false
  tags = {
    "azd-service-name" = "inference"
    "azd-env-name"     = var.environment_name
  }
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runtime.id]
  }
  body = {
    kind = "functionapp"
    properties = {
      environmentId       = azurerm_container_app_environment.main.id
      workloadProfileName = "Consumption"
      configuration = {
        activeRevisionsMode = "Single"
        registries = [{
          server   = azurerm_container_registry.main.login_server
          identity = azurerm_user_assigned_identity.runtime.id
        }]
        ingress = {
          external      = false
          targetPort    = 80
          transport     = "Auto"
          allowInsecure = false
        }
      }
      template = {
        containers = [{
          name  = "inference"
          image = local.image_references.inference
          resources = {
            cpu    = 1
            memory = "2Gi"
          }
          env = concat(local.common_env, local.function_env, [
            { name = "AzureWebJobsStorage__accountName", value = azapi_resource.inference_host.name },
            { name = "AzureWebJobsStorage__credential", value = "managedidentity" },
            { name = "AzureWebJobsStorage__clientId", value = azurerm_user_assigned_identity.runtime.client_id },
            { name = "PTU_BATCH_IMAGE_SOURCE_FINGERPRINT", value = local.source_fingerprint },
          ])
        }]
        # Native Functions creates the Service Bus trigger scaler; no custom rule.
        scale = {
          minReplicas = 0
          maxReplicas = 1
        }
      }
    }
  }
  lifecycle {
    replace_triggered_by = [azurerm_container_app_environment.main.id]
  }
  depends_on = [
    terraform_data.application_guard,
    azurerm_private_endpoint.data,
    azurerm_private_dns_zone_virtual_network_link.private,
    azapi_resource.batch_files,
    azurerm_cosmosdb_sql_container.ledger,
    azurerm_cosmosdb_sql_container.leases,
    azurerm_cosmosdb_sql_role_assignment.runtime,
    azurerm_role_assignment.runtime,
    azurerm_role_assignment.existing_ptu,
  ]
}
