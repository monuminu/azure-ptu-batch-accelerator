resource "azurerm_resource_group" "main" {
  name     = local.resource_group
  location = var.location
  tags = {
    project = "azure-ptu-batch-accelerator"
  }
  depends_on = [terraform_data.contract]
}

resource "azurerm_user_assigned_identity" "runtime" {
  name                = "id-${var.environment_name}-ptu-batch"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${var.environment_name}-ptu-batch"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

resource "azurerm_application_insights" "main" {
  name                         = "appi-${var.environment_name}-ptu-batch"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  application_type             = "web"
  workspace_id                 = azurerm_log_analytics_workspace.main.id
  local_authentication_enabled = false
}

resource "azurerm_container_registry" "main" {
  name                = "acr${local.name_prefix}${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Basic"
  admin_enabled       = false
}

resource "azapi_resource" "payload" {
  type                   = "Microsoft.Storage/storageAccounts@2023-05-01"
  name                   = local.payload_storage_name
  parent_id              = azurerm_resource_group.main.id
  location               = azurerm_resource_group.main.location
  response_export_values = ["properties.primaryEndpoints.blob"]
  body = {
    kind = "StorageV2"
    sku  = { name = "Standard_LRS" }
    properties = {
      allowBlobPublicAccess        = false
      allowSharedKeyAccess         = false
      supportsHttpsTrafficOnly     = true
      minimumTlsVersion            = "TLS1_2"
      publicNetworkAccess          = var.private_networking ? "Disabled" : "Enabled"
      defaultToOAuthAuthentication = true
    }
  }
}

resource "azapi_resource" "control_host" {
  type      = "Microsoft.Storage/storageAccounts@2023-05-01"
  name      = "sc${local.name_prefix}${local.suffix}"
  parent_id = azurerm_resource_group.main.id
  location  = azurerm_resource_group.main.location
  body = {
    kind = "StorageV2"
    sku  = { name = "Standard_LRS" }
    properties = {
      allowBlobPublicAccess        = false
      allowSharedKeyAccess         = false
      supportsHttpsTrafficOnly     = true
      minimumTlsVersion            = "TLS1_2"
      publicNetworkAccess          = var.private_networking ? "Disabled" : "Enabled"
      defaultToOAuthAuthentication = true
    }
  }
}

resource "azapi_resource" "inference_host" {
  type      = "Microsoft.Storage/storageAccounts@2023-05-01"
  name      = "si${local.name_prefix}${local.suffix}"
  parent_id = azurerm_resource_group.main.id
  location  = azurerm_resource_group.main.location
  body = {
    kind = "StorageV2"
    sku  = { name = "Standard_LRS" }
    properties = {
      allowBlobPublicAccess        = false
      allowSharedKeyAccess         = false
      supportsHttpsTrafficOnly     = true
      minimumTlsVersion            = "TLS1_2"
      publicNetworkAccess          = var.private_networking ? "Disabled" : "Enabled"
      defaultToOAuthAuthentication = true
    }
  }
}

# ARM control-plane container creation avoids shared-key or data-plane bootstrap.
resource "azapi_resource" "batch_files" {
  type      = "Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01"
  name      = "batch-files"
  parent_id = "${azapi_resource.payload.id}/blobServices/default"
  body = {
    properties = {
      publicAccess = "None"
    }
  }
}

resource "azurerm_cosmosdb_account" "main" {
  name                          = "cosmos-${var.environment_name}-${local.suffix}"
  location                      = var.cosmos_location == null ? azurerm_resource_group.main.location : var.cosmos_location
  resource_group_name           = azurerm_resource_group.main.name
  offer_type                    = "Standard"
  kind                          = "GlobalDocumentDB"
  local_authentication_enabled  = false
  public_network_access_enabled = !var.private_networking

  capabilities {
    name = "EnableServerless"
  }

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = var.cosmos_location == null ? azurerm_resource_group.main.location : var.cosmos_location
    failover_priority = 0
  }
}

resource "azurerm_cosmosdb_sql_database" "main" {
  name                = "ptu-batch"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
}

resource "azurerm_cosmosdb_sql_container" "ledger" {
  name                = "ledger"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  database_name       = azurerm_cosmosdb_sql_database.main.name
  partition_key_paths = ["/pk"]
  partition_key_kind  = "Hash"
}

resource "azurerm_cosmosdb_sql_container" "leases" {
  name                = "leases"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  database_name       = azurerm_cosmosdb_sql_database.main.name
  partition_key_paths = ["/id"]
  partition_key_kind  = "Hash"
}

resource "azurerm_servicebus_namespace" "main" {
  name                          = "sb-${var.environment_name}-${local.suffix}"
  location                      = azurerm_resource_group.main.location
  resource_group_name           = azurerm_resource_group.main.name
  sku                           = "Standard"
  local_auth_enabled            = false
  public_network_access_enabled = true
}

resource "azurerm_servicebus_queue" "control" {
  name                                    = "batch-control"
  namespace_id                            = azurerm_servicebus_namespace.main.id
  lock_duration                           = "PT5M"
  max_delivery_count                      = 10
  default_message_ttl                     = "P14D"
  requires_duplicate_detection            = true
  duplicate_detection_history_time_window = "P7D"
  dead_lettering_on_message_expiration    = true
}

resource "azurerm_servicebus_queue" "inference" {
  name                                    = "batch-inference"
  namespace_id                            = azurerm_servicebus_namespace.main.id
  lock_duration                           = "PT5M"
  max_delivery_count                      = 10
  default_message_ttl                     = "P14D"
  requires_duplicate_detection            = true
  duplicate_detection_history_time_window = "P7D"
  dead_lettering_on_message_expiration    = true
}

resource "azurerm_container_app_environment" "main" {
  name                       = var.private_networking ? "cae-${var.environment_name}-ptu-batch-vnet" : "cae-${var.environment_name}-ptu-batch"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  logs_destination           = "log-analytics"
  infrastructure_subnet_id   = var.private_networking ? azurerm_subnet.apps[0].id : null

  lifecycle {
    create_before_destroy = true
  }

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}
