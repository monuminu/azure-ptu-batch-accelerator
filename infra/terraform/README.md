# Native Terraform deployment

This module deploys the accelerator with AzureRM 4.x and AzAPI 2.x resources. The API is a regular public Container App. The control and inference apps are native `Microsoft.App/containerApps` resources with `kind = functionapp`. No ARM/Bicep deployment wrapper, model deployment, or PTU SKU change is used.

## Prerequisites

- Terraform 1.15.x (tested with 1.15.7), Azure CLI, Docker, and Python 3.12.
- Azure CLI signed in to the target tenant and permission to create the support resources, role assignments, and (unless `api_app_id` is supplied) an Entra application and service principal.
- An account ARM resource ID and endpoint for an **existing** Azure OpenAI account. The actual deployment name is supplied in JSONL `body.model`, with no per-deployment alias configuration. Production mode requires a PTU SKU. The runtime also verifies PTU eligibility. In production mode, Terraform grants its managed identity OpenAI User and Reader at the existing account scope by default; validation-only mode makes no role change to that account.
- A private, persistent Terraform state path for single-operator validation, or a separately managed Azure Storage backend for shared/production operations. Terraform state can contain provider-returned sensitive values, including service keys, even though runtime services have local authentication disabled. Never commit state or plans.

Configure at least `subscription_id`, `openai_account_resource_id`, and `openai_resource_endpoint` through `TF_VAR_*` or a private `*.auto.tfvars` file. Set `tenant_id` when the target subscription belongs to a tenant other than the Azure CLI default. AzureRM, AzAPI, and AzureAD then all use that tenant, and the module checks it against the AzureRM identity. To reuse an existing Entra API registration, set `api_app_id`. Otherwise, the default creates one with a delegated `Batch.ReadWrite` scope, an application `Batch.ReadWrite.Application` role, and no secret. Azure CLI preauthorization is opt-in. Assign callers the appropriate permission and consent separately.

In primary account mode, a read-only ARM account GET verifies account identity, matching endpoint and readiness. No deployment name is required during deployment. Runtime validation resolves JSONL `body.model` within the snapshotted account and checks that specific deployment's PTU SKU, capacity, state, API capability and spillover before inference. New PTU deployments need no server mapping update.

Legacy deployment-name/alias inputs remain supported when the account inputs are omitted. That path retains its existing read-only deployment check, including the explicit validation-only Standard reference option. Account mode does not silently permit Standard inference. See [routing](../../docs/deployment-name-routing.md) for client and security details.

From this directory, initialize with a private state path:

```sh
export TF_DATA_DIR=/absolute/private/terraform/data
terraform init -reconfigure -backend-config=path=/absolute/private/terraform/state.tfstate
terraform validate
terraform plan -out=/absolute/private/terraform/accelerator.tfplan
```

Use the repository's `scripts/terraform_deploy.py` wrapper for **every** apply, including retries and updates. Plan first, review the private artifact, and pass its printed path to `--apply-plan`; the matching private manifest verifies the plan and all source/variable/backend inputs without replanning. The wrapper invokes the drain guard immediately before apply, even when Terraform reports no resource change. The HCL guard additionally runs before app revisions when their inputs change. A direct `terraform apply` can skip that freshness check.

For an Azure remote backend, use the example `backend_override.tf` and a private `--backend-config` file as described in the [deployment guide](../../docs/terraform-deployment.md#azure-remote-state). No state migration is automatic. The wrapper blocks destructive changes to data resources and allows only explicitly reviewed replacement of the stateless applications/environment. Keep independent state backups before migrations.

The apply graph creates a Basic ACR, grants the deployer AcrPush by default, builds and pushes the three Linux AMD64 images through `scripts/terraform_images.py`, runs `scripts/terraform_guard.py` to drain a previously deployed inference app, then creates or updates the apps with the real image tags. The two helpers receive settings through environment variables; no registry password or account key is written into Terraform configuration. `build_images = false` is intended only when those exact image tags already exist. Source changes create a new default image tag. With a custom `image_tag`, the source fingerprint environment setting still causes an app revision update.

The three storage accounts use native AzAPI control-plane resources with shared-key access disabled from creation. This avoids AzureRM storage-account creation polling through the key-authenticated data plane. The runtime payload Blob URL is built from the stable account name and the Azure public-cloud DNS suffix (`blob.core.windows.net`), so a storage policy update does not force an unrelated app revision. This module currently targets Azure public cloud. For a state created with the earlier AzureRM storage resources, back up state and migrate only those three addresses to the existing AzAPI resource IDs before retrying; this module does not remove state entries or import resources automatically.

The native apps use Azure's canonical `Auto` ingress transport value. The parent Entra application continues to manage access-token version 2; its identifier URI, delegated scope, and application role are owned by separate Terraform resources, with those three parent fields ignored for drift. Azure-generated service-management references are also preserved rather than cleared by unrelated deployments.

Set `cosmos_location` to place only Cosmos DB in another supported region; when null it follows `location`. The resource group, Container Apps, registry, storage, and monitoring retain `location`.

## Private data networking

`private_networking = true` is the default. It creates a VNet (`10.248.0.0/16` by default) with a delegated `/23` Container Apps infrastructure subnet and a separate `/24` private endpoint subnet. Set `vnet_address_space` to a non-overlapping `/16` before deployment if the default overlaps your network. The managed environment uses a distinct `-vnet` name and the three apps are replaced when its immutable environment ID changes. Review that replacement carefully for an existing deployment with live jobs.

The module creates eight private endpoints: payload Blob, control host Blob/Queue/Table, inference host Blob/Queue/Table, and Cosmos SQL. Four private DNS zones and VNet links let the apps keep using normal public-service hostnames while resolving Storage and Cosmos to private IPs inside the VNet. Storage and Cosmos public network access is disabled. The API ingress stays public with Entra authentication; the two Functions ingresses stay internal. ACR Basic and Service Bus Standard remain public endpoints with managed-identity authorization. Private endpoints and DNS incur additional Azure charges.

Set `private_networking = false` only when subscription policy permits public Storage and Cosmos data access. A policy that forces those accounts to disable public network access requires the private mode for file and ledger operations to work. Changing this flag after deployment changes the managed environment and network resources; use the guarded deployment wrapper and review the plan.

The API begins with submission admission paused. Resume it with the operator maintenance command only after preflight and smoke tests pass. Control stays at one replica for Cosmos change-feed polling. Inference scales from zero to one on its native Service Bus trigger. Both Functions apps have internal ingress and separate managed-identity host storage accounts.

## Local checks

Run `terraform fmt -check -recursive`, `terraform validate`, and `terraform test` after `terraform init -backend=false -lockfile=readonly`. Tests use mock providers and never provision Azure resources. CI also runs Trivy configuration scanning. Terraform provider versions are recorded in `.terraform.lock.hcl`: AzureRM 4.81.0, AzAPI 2.13.0, AzureAD 3.10.0, and Random 3.9.1. After real deployment, require a zero-change plan and real authenticated API/trigger checks.

For live production deployment, keep `assign_runtime_ptu_roles = true` unless an operator has already granted OpenAI User and Reader on the existing account. `assign_builder_acr_push = false` is available when the deployer already has AcrPush. The module requires an explicit `subscription_id` so its AzureRM and AzAPI resources target the same subscription.
