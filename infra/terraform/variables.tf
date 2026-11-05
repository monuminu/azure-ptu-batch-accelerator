variable "subscription_id" {
  description = "Subscription in which the accelerator is deployed."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "subscription_id must be a subscription GUID."
  }
}

variable "tenant_id" {
  description = "Expected Entra tenant; null uses the signed-in Azure CLI tenant."
  type        = string
  default     = null
  nullable    = true
}

variable "location" {
  type    = string
  default = "eastus"
}

variable "cosmos_location" {
  description = "Optional Cosmos DB region; null uses location without moving compute or other resources."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition     = var.cosmos_location == null || can(regex("^[a-z][a-z0-9]+$", var.cosmos_location))
    error_message = "cosmos_location must be an Azure region name such as swedencentral."
  }
}

variable "private_networking" {
  description = "Create ACA VNet integration and private endpoints/DNS for Storage and Cosmos. Public opt-out requires policy permission."
  type        = bool
  default     = true
}

variable "vnet_address_space" {
  description = "Non-overlapping /16 Azure VNet CIDR. App infrastructure uses its first /23; private endpoints use its third /24."
  type        = string
  default     = "10.248.0.0/16"
  validation {
    condition     = endswith(var.vnet_address_space, "/16") && can(cidrhost(var.vnet_address_space, 0))
    error_message = "vnet_address_space must be a valid, non-overlapping IPv4 /16 CIDR."
  }
}

variable "environment_name" {
  description = "Short name used in resource names."
  type        = string
  default     = "ptubatchtf"
  validation {
    condition     = length(var.environment_name) <= 14 && can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.environment_name))
    error_message = "Use 2–14 lowercase letters, digits, or hyphens; start with a letter and end with a letter or digit."
  }
}

variable "ptu_deployment_resource_id" {
  description = "Legacy mode: full ARM ID of an EXISTING Azure OpenAI deployment. Omit for JSONL model routing."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition     = var.ptu_deployment_resource_id == null || can(regex("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\\.CognitiveServices/accounts/[^/]+/deployments/[^/]+$", var.ptu_deployment_resource_id))
    error_message = "Provide a full Microsoft.CognitiveServices/accounts/.../deployments/... resource ID."
  }
}

variable "ptu_resource_endpoint" {
  description = "Legacy mode: HTTPS endpoint of the existing Azure OpenAI account. Omit for JSONL model routing."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition     = var.ptu_resource_endpoint == null || can(regex("^https://[^/]+/?$", var.ptu_resource_endpoint))
    error_message = "Provide the account's HTTPS origin, without a path."
  }
}

variable "ptu_deployment_name" {
  description = "Legacy mode: name of the existing PTU deployment (must match its resource ID). Omit for JSONL model routing."
  type        = string
  default     = null
  nullable    = true
}

variable "openai_account_resource_id" {
  description = "JSONL model routing: full ARM ID of an EXISTING Azure OpenAI account. Terraform does not create or alter the account or its deployments."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition     = var.openai_account_resource_id == null || can(regex("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\\.CognitiveServices/accounts/[^/]+$", var.openai_account_resource_id))
    error_message = "Provide an existing Microsoft.CognitiveServices/accounts/... ARM ID without a deployment segment."
  }
  validation {
    condition = (
      var.openai_account_resource_id != null && var.openai_resource_endpoint != null &&
      var.ptu_deployment_resource_id == null && var.ptu_deployment_name == null && var.ptu_resource_endpoint == null
      ) || (
      var.openai_account_resource_id == null && var.openai_resource_endpoint == null &&
      var.ptu_deployment_resource_id != null && var.ptu_deployment_name != null && var.ptu_resource_endpoint != null
    )
    error_message = "Specify either the account ID and endpoint for JSONL model routing or all three legacy deployment fields; do not mix or omit them."
  }
}

variable "openai_resource_endpoint" {
  description = "JSONL model routing: HTTPS origin returned by the existing Azure OpenAI account."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition     = var.openai_resource_endpoint == null || can(regex("^https://[A-Za-z0-9][A-Za-z0-9.-]*/?$", var.openai_resource_endpoint))
    error_message = "Provide the account's plain HTTPS origin without credentials, path, query, or fragment."
  }
}

variable "ptu_alias" {
  type    = string
  default = "ptu"
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_-]*$", var.ptu_alias))
    error_message = "ptu_alias must be a URL-safe path segment."
  }
}

variable "allowed_apis" {
  description = "Stateless Azure OpenAI routes exposed by this PTU deployment."
  type        = list(string)
  default     = ["/chat/completions", "/responses"]
  validation {
    condition     = length(var.allowed_apis) > 0 && length(setsubtract(toset(var.allowed_apis), toset(["/chat/completions", "/responses"]))) == 0
    error_message = "Only /chat/completions and /responses are supported."
  }
}

variable "allowed_subjects" {
  description = "Optional caller subject allowlist; empty relies on the API permission."
  type        = list(string)
  default     = []
}

variable "api_app_id" {
  description = "Existing accelerator API application/client ID. Leave null to create an owned Entra registration."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition     = var.api_app_id == null || can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.api_app_id))
    error_message = "api_app_id must be an application GUID or null."
  }
}

variable "create_api_registration" {
  description = "Create an Entra app, service principal, delegated scope and application role when api_app_id is null."
  type        = bool
  default     = true
}

variable "preauthorize_azure_cli" {
  description = "Opt in to preauthorizing Azure CLI for the owned delegated API scope."
  type        = bool
  default     = false
}

variable "assign_runtime_ptu_roles" {
  description = "Grant the runtime OpenAI User and Reader on the existing account; no model resource is changed."
  type        = bool
  default     = true
}

variable "validation_only" {
  description = "Permit a readable, succeeded non-PTU deployment solely for paused infrastructure/API/auth smoke checks. Runtime PTU preflight remains strict."
  type        = bool
  default     = false
}

variable "assign_builder_acr_push" {
  description = "Grant the signed-in deployer AcrPush for the image build step."
  type        = bool
  default     = true
}

variable "build_images" {
  description = "Build and push all three real images inside the Terraform apply graph."
  type        = bool
  default     = true
}

variable "image_tag" {
  description = "Optional immutable image tag; null uses the first 12 characters of the source fingerprint."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition     = var.image_tag == null || can(regex("^(?:[0-9a-f]{12}|v[A-Za-z0-9][A-Za-z0-9._-]{0,126})$", var.image_tag))
    error_message = "image_tag must be a 12-character lowercase source hash or a v-prefixed version tag."
  }
}

variable "pip_index_url" {
  description = "Package index passed to the image build helper."
  type        = string
  default     = "https://pypi.org/simple"
  validation {
    condition     = startswith(var.pip_index_url, "https://") && !strcontains(var.pip_index_url, "@") && !strcontains(var.pip_index_url, "?") && !strcontains(var.pip_index_url, "#")
    error_message = "pip_index_url must be an unauthenticated HTTPS URL without query or fragment."
  }
}

variable "container_apps_api_version" {
  description = "Microsoft.App/containerApps API version used by native AzAPI resources."
  type        = string
  default     = "2026-01-01"
}
