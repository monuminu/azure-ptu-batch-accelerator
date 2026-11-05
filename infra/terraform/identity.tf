resource "azuread_application" "api" {
  count                   = local.own_api_app ? 1 : 0
  display_name            = "${var.environment_name} PTU batch API"
  sign_in_audience        = "AzureADMyOrg"
  owners                  = [data.azurerm_client_config.current.object_id]
  prevent_duplicate_names = true

  api {
    requested_access_token_version = 2
  }
  lifecycle {
    # These three fields are owned by the granular resources below. The parent
    # still manages API token version 2 and its other declared properties.
    ignore_changes = [identifier_uris, app_role, api[0].oauth2_permission_scope, service_management_reference]
  }
  depends_on = [terraform_data.contract]
}

resource "azuread_application_identifier_uri" "api" {
  count          = local.own_api_app ? 1 : 0
  application_id = azuread_application.api[0].id
  identifier_uri = "api://${azuread_application.api[0].client_id}"
}

resource "random_uuid" "delegated_scope" {
  depends_on = [terraform_data.contract]
}
resource "random_uuid" "application_role" {
  depends_on = [terraform_data.contract]
}

resource "azuread_application_permission_scope" "delegated" {
  count                      = local.own_api_app ? 1 : 0
  application_id             = azuread_application.api[0].id
  scope_id                   = random_uuid.delegated_scope.result
  value                      = "Batch.ReadWrite"
  type                       = "Admin"
  admin_consent_display_name = "Access PTU batch accelerator"
  admin_consent_description  = "Create, read, and cancel batch jobs and files."
  depends_on                 = [azuread_application_identifier_uri.api]
}

# Entra requires app-role and delegated-scope values to be distinct.
resource "azuread_application_app_role" "application" {
  count                = local.own_api_app ? 1 : 0
  application_id       = azuread_application.api[0].id
  role_id              = random_uuid.application_role.result
  value                = "Batch.ReadWrite.Application"
  display_name         = "PTU batch application access"
  description          = "Application access to PTU batch jobs and files."
  allowed_member_types = ["Application"]
  depends_on           = [azuread_application_permission_scope.delegated]
}

resource "azuread_service_principal" "api" {
  count     = local.own_api_app ? 1 : 0
  client_id = azuread_application.api[0].client_id
  owners    = [data.azurerm_client_config.current.object_id]
  depends_on = [
    azuread_application_identifier_uri.api,
    azuread_application_permission_scope.delegated,
    azuread_application_app_role.application,
  ]
}

resource "azuread_application_pre_authorized" "azure_cli" {
  count                = local.own_api_app && var.preauthorize_azure_cli ? 1 : 0
  application_id       = azuread_application.api[0].id
  authorized_client_id = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
  permission_ids       = [azuread_application_permission_scope.delegated[0].scope_id]
  depends_on           = [azuread_application_app_role.application]
}

locals {
  runtime_roles = {
    insights_publisher = {
      scope = azurerm_application_insights.main.id
      id    = "3913510d-42f4-4e42-8a64-420c390055eb"
    }
    acr_pull = {
      scope = azurerm_container_registry.main.id
      id    = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
    }
    payload_blob = {
      scope = azapi_resource.payload.id
      id    = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
    }
    control_host_blob = {
      scope = azapi_resource.control_host.id
      id    = "b7e6dc6d-f1e8-4753-8033-0f276bb0955b"
    }
    control_host_queue = {
      scope = azapi_resource.control_host.id
      id    = "974c5e8b-45b9-4653-ba55-5f855dd0fb88"
    }
    control_host_table = {
      scope = azapi_resource.control_host.id
      id    = "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3"
    }
    control_host_account = {
      scope = azapi_resource.control_host.id
      id    = "17d1049b-9a84-46fb-8f53-869881c3d3ab"
    }
    inference_host_blob = {
      scope = azapi_resource.inference_host.id
      id    = "b7e6dc6d-f1e8-4753-8033-0f276bb0955b"
    }
    inference_host_queue = {
      scope = azapi_resource.inference_host.id
      id    = "974c5e8b-45b9-4653-ba55-5f855dd0fb88"
    }
    inference_host_table = {
      scope = azapi_resource.inference_host.id
      id    = "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3"
    }
    inference_host_account = {
      scope = azapi_resource.inference_host.id
      id    = "17d1049b-9a84-46fb-8f53-869881c3d3ab"
    }
    servicebus_owner = {
      scope = azurerm_servicebus_namespace.main.id
      id    = "090c5cfd-751d-490a-894a-3ce6f1109419"
    }
  }
}

resource "azurerm_role_assignment" "runtime" {
  for_each                         = local.runtime_roles
  scope                            = each.value.scope
  role_definition_id               = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${each.value.id}"
  principal_id                     = azurerm_user_assigned_identity.runtime.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_cosmosdb_sql_role_assignment" "runtime" {
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  principal_id        = azurerm_user_assigned_identity.runtime.principal_id
  role_definition_id  = "${azurerm_cosmosdb_account.main.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  scope               = azurerm_cosmosdb_account.main.id
}

resource "azurerm_role_assignment" "builder_acr_push" {
  count              = var.assign_builder_acr_push && var.build_images ? 1 : 0
  scope              = azurerm_container_registry.main.id
  role_definition_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/8311e382-0749-4cb8-b61a-304f252e45ec"
  principal_id       = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "existing_ptu" {
  # Keep the existing role addresses while account-only routing changes the
  # scope source from a deployment-derived account ID to the account input.
  for_each = var.assign_runtime_ptu_roles && !var.validation_only ? {
    openai_user = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
    reader      = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
  } : {}

  scope                            = local.ptu_account_id
  role_definition_id               = "/subscriptions/${local.ptu_subscription_id}/providers/Microsoft.Authorization/roleDefinitions/${each.value}"
  principal_id                     = azurerm_user_assigned_identity.runtime.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}
