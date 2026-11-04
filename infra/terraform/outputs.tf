output "api_endpoint_url" {
  description = "Public API origin. Admission remains paused until an operator resumes it."
  value       = "https://${azapi_resource.api.output.properties.configuration.ingress.fqdn}"
}

output "api_base_url" {
  description = "OpenAI compatible base URL."
  value       = "https://${azapi_resource.api.output.properties.configuration.ingress.fqdn}/openai/v1/"
}

output "api_name" {
  value = azapi_resource.api.name
}

output "control_name" {
  value = azapi_resource.control.name
}

output "inference_name" {
  value = azapi_resource.inference.name
}

output "resource_group_name" {
  value = azurerm_resource_group.main.name
}

output "registry_name" {
  value = azurerm_container_registry.main.name
}

output "registry_login_server" {
  value = azurerm_container_registry.main.login_server
}

output "tenant_id" {
  value = local.tenant_id
}

output "api_app_id" {
  value = local.effective_api_app_id
}

output "managed_identity_client_id" {
  value = azurerm_user_assigned_identity.runtime.client_id
}

output "managed_identity_principal_id" {
  value = azurerm_user_assigned_identity.runtime.principal_id
}

output "servicebus_namespace" {
  value = azurerm_servicebus_namespace.main.name
}

output "ptu_alias" {
  value = var.ptu_alias
}

output "validation_only" {
  description = "True means a non-PTU deployment was allowed for infrastructure/API/auth validation."
  value       = var.validation_only
}

output "validation_only_warning" {
  description = "Operational warning for validation-only deployments."
  value       = var.validation_only ? "VALIDATION ONLY: keep submission admission paused. The referenced deployment may be non-PTU; no batch execution or model calls are authorized. Runtime PTU preflight remains strict." : null
}
