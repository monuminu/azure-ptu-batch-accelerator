# Deploy with Terraform

The native template in `infra/terraform` uses AzureRM for supporting resources and AzAPI for the three Container Apps. Both Functions applications use `Microsoft.App/containerApps` with `kind=functionapp`; this does not wrap Bicep or deploy a different App Service architecture.

## Prerequisites and values

Use Terraform **1.15.x** (tested with **1.15.7**), Azure CLI signed into the target tenant, Docker with Linux AMD64 build support, and Python 3.12. The committed provider lock uses AzureRM 4.81.0, AzAPI 2.13.0, AzureAD 3.10.0, and Random 3.9.1. The deployer needs resource creation and role-assignment rights in the target subscription, AcrPush, read access to the existing Azure OpenAI account, and permissions for any optional Entra registration/preauthorization. An existing accelerator `api_app_id` can be supplied instead of creating one.

Copy `infra/terraform/variables.example.json` to a **private location outside this repository** and fill its JSON variables for the wrapper. Set explicit `subscription_id` and `tenant_id`, the existing `openai_account_resource_id` and `openai_resource_endpoint`, and a short environment name. No deployment name or alias is required in the primary account mode: the user supplies the actual deployment name in each JSONL `body.model`. Use `api_app_id` if your administrator already registered the API. Optional Azure CLI preauthorization is disabled by default; enable it only when that client should be allowed to request the API's delegated scope.

Delegated callers receive `Batch.ReadWrite`. Application callers receive the distinct `Batch.ReadWrite.Application` role. Both use `api://<api-app-id>/.default` when obtaining tokens; the original legacy application role remains accepted for existing registrations. No application password is created.

## Account configuration and runtime deployment selection

Primary account mode requires only `openai_account_resource_id` (an account ARM ID ending in `/accounts/<name>`) and its matching HTTPS `openai_resource_endpoint`. Terraform checks that the account exists and is ready, and grants the runtime scoped Reader and OpenAI User access. It does not enumerate or register deployment aliases and does not create/change model deployments.

The JSONL `body.model` is the actual deployment name. The service snapshots the trusted account at batch acceptance, resolves a literal name during whole-file validation, and performs strict deployment-level PTU, capacity, state, API and spillover checks before dispatch. One file must use one deployment and normalized endpoint. The resolved endpoint/deployment target is committed in the job; subsequent account-config changes cannot redirect an accepted job.

The original `ptu_deployment_resource_id`/`ptu_resource_endpoint`/`ptu_deployment_name`/`ptu_alias` inputs remain a compatibility path. Omit those fields when using the account inputs. `validation_only` belongs to the legacy known-deployment platform-validation path; it is not needed for account mode.

## Network topology

Terraform defaults to `private_networking=true`: a delegated Container Apps subnet, private endpoints for payload/host Storage and Cosmos DB, private DNS links, and disabled public data access. This works with subscription policies that require private storage. ACR Basic and Service Bus Standard still use public endpoints with managed identity; their private endpoint variants require higher service tiers. Set `private_networking=false` only where policy permits public data endpoints. Changing this flag replaces the managed environment and its stateless apps, so drain work first.

`cosmos_location` can select an independently available Cosmos region; it otherwise follows `location`. Keep it close to compute for production latency. Private endpoints can connect to a Cosmos account in another region.

## Private plan and apply

Use the wrapper for **every** apply and retry, including updates. First create a private plan. Review the plan summary and the full plan locally, then apply that exact artifact. The wrapper records digests of the plan, source, variables, and backend configuration in a private manifest. It rejects changed inputs and runs a fresh read-only account/deployment check and durable drain guard before apply, even when Terraform's guard resource has no changed fingerprint.

```bash
python3 scripts/terraform_deploy.py \
  --variables /private/location/deployment.tfvars.json \
  --state-dir /private/location/ptu-batch-state

# Review the saved plan path printed by the command. Detailed plan output is private.
# Use the same TF_DATA_DIR when running terraform show for that backend.
TF_DATA_DIR=/private/location/ptu-batch-state/data \
  terraform -chdir=infra/terraform show /private/location/ptu-batch-state/accelerator.tfplan

python3 scripts/terraform_deploy.py \
  --variables /private/location/deployment.tfvars.json \
  --state-dir /private/location/ptu-batch-state \
  --apply-plan /private/location/ptu-batch-state/accelerator.tfplan
```

Planning does not deactivate revisions or create resources. Each new plan preserves earlier artifacts and prints its actual path; use that path with `--apply-plan`. The adjacent `.manifest.json` must stay with it. Both files must have mode `600`. The immediate `--apply` option creates and applies a new plan in one invocation and is intended for explicitly authorized single-operator validation. It does not apply an earlier reviewed plan.

State, plans, manifests, outputs, provider data, and detailed logs stay in the private directory. Terraform state can contain resource secrets even though the application uses managed identity. Local state is for isolated single-operator validation; use the Azure backend below for shared or production deployments. State, real variable files, backend overrides, and plans are excluded from Git and Docker build contexts.

Plans that delete cloud resources are blocked. `--allow-replace-owned` permits only reviewed **replacements** of the three named stateless applications and their Container Apps environment. It never permits deleting a resource group, Cosmos account, Storage, Service Bus, or another data resource. A data migration or complete teardown needs a separately reviewed procedure.

## Azure remote state

Provision a separate Azure Storage state account and private Blob container outside the accelerator stack. Enable Blob versioning, soft delete, encryption, and logging, disable shared keys and public Blob access, and grant the operator or CI identity Storage Blob Data Contributor at the container scope. The runner needs network access to this account; a private-only account requires a connected runner or VPN. The state backend must outlive the accelerator resources. The [AzureRM backend](https://developer.hashicorp.com/terraform/language/backend/azurerm) uses Azure Blob leases for state locking.

For a **new environment**, copy `infra/terraform/examples/backend-azurerm.tf.example` to `infra/terraform/backend_override.tf`. Terraform's [override rule](https://developer.hashicorp.com/terraform/language/files/override) replaces the default local backend with this AzureRM backend. Copy `examples/azure.tfbackend.example` to a private location outside the repository, fill the nonsecret names and IDs, and set its permissions to `600`. Keep Entra authentication enabled; use the runner's Azure CLI, managed identity, or federated identity and keep credentials out of backend files.

Pass the same private backend file to both commands:

```bash
python3 scripts/terraform_deploy.py \
  --variables /private/location/deployment.tfvars.json \
  --state-dir /private/location/ptu-batch-remote \
  --backend-config /private/location/azure.tfbackend

python3 scripts/terraform_deploy.py \
  --variables /private/location/deployment.tfvars.json \
  --state-dir /private/location/ptu-batch-remote \
  --backend-config /private/location/azure.tfbackend \
  --apply-plan /private/location/ptu-batch-remote/accelerator.tfplan
```

`--state-dir` still holds private provider metadata, plan artifacts, and logs; authoritative state is in the remote container. Initialization uses `-lockfile=readonly`, so upgrades to locked provider versions require a deliberate separate change.

For an **existing local state**, do not simply choose a new state directory or run `init -reconfigure` against an empty backend. Freeze deployments, back up the state with mode `600`, record its lineage, serial, and `terraform state list`, add the backend override, and explicitly run `terraform init -migrate-state` using the existing `TF_DATA_DIR` and the private backend file. Retain the backup and migration logs, verify that the same resource addresses exist remotely, and require a zero-change plan before applying anything. The wrapper refuses a remote switch while its local state file remains present; archive that local file only after migration is verified. For rollback, freeze all writers, preserve both state versions, and restore the verified state through a deliberate reverse migration. Never delete or overwrite remote state to make a plan pass.

Image builds and ACR pushes execute inside the Terraform graph. Registry admin credentials are disabled; the helper obtains a short-lived push token, passes it through Docker stdin, and never prints it. Images use a source-content tag and `linux/amd64`. The public package index is the default; `pip_index_url` may select a trusted unauthenticated mirror. Do not put credentials in build arguments.

On an existing deployment, pause batch admission through the control app and wait for accepted work to finish. The guard checks durable job/reservation/outbox state, both queues' active/scheduled counts, and old inference replicas, then deactivates old consumers before Terraform changes the applications. Its read-only maintenance exec makes up to three attempts for invocation failures or a missing marker; an explicit unsafe status stops immediately. Admission remains paused after apply until explicitly resumed. See [operations](operations.md) for pause/check/resume and rollback commands. With Terraform, read the resource names from private `outputs.json` or `terraform output` rather than `azd env`.

## Validate infrastructure when no PTU is available

In legacy known-deployment mode, production rejects Standard deployments before infrastructure creation. In primary account mode, each submitted JSONL deployment is checked at runtime before model calls. For an explicit legacy **platform-only validation**, set `validation_only=true` in the private JSON and pass `--validation-only` to both plan and apply-plan wrapper invocations. The existing reference deployment must still be readable, succeeded, and have no spillover.

This mode keeps the application's Azure backend configuration and PTU-only runtime guard intact. It does not create PTUs, grant access to the Standard account, enable Standard inference, or switch to synthetic inference. Keep customer admission paused. Health, authentication, Files operations, and a malformed-input asynchronous job can verify the deployed platform without a model call. For that controlled validation test, use a single JSONL row with `method="GET"`, briefly resume to submit it, and immediately pause again before polling. Confirm its validation error identifies `method`; do not submit valid inference input. A notification referring to that terminal job can exercise the inference trigger without dispatching a request. Real PTU execution remains unverified until a valid provisioned target is configured and `validation_only` is disabled.

Both Functions images declare `Microsoft.Azure.Functions.ExtensionBundle` v4 in `host.json`. Importing the Python functions alone does not establish that the host registered Service Bus and Cosmos bindings. Verify the actual host's trigger registration and successful invocation logs during live acceptance, and check that inference wakes from zero on a terminal-job notification.

## Caller authentication across tenants

The deployment providers use the explicit target tenant without changing your Azure CLI default. If that default belongs to another tenant, construct the local delegated token provider with `AzureCliCredential(tenant_id=<tenant_id output>)` and request `api://<api_app_id output>/.default`. Do not pass both tenant and subscription to Azure CLI token acquisition. Deployed workloads can use `DefaultAzureCredential` with their correctly configured service identity.

## Local verification and lifecycle

```bash
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform init -backend=false -lockfile=readonly
terraform -chdir=infra/terraform validate
terraform -chdir=infra/terraform test
trivy config --exit-code 1 infra/terraform
```

The tests use mocked providers and make no Azure resource changes. Trivy covers the resource types it understands; explicit architecture assertions also check native AzAPI storage and Functions settings. These checks do not establish that Azure has accepted the template. After a successful live apply, run the wrapper in plan-only mode again and require `create=0 update=0 delete=0`, then test the authenticated Files and asynchronous job workflow through real ingress. Keep model execution separate from platform-only validation.

Do not use the same Azure resources from Bicep and Terraform states; import/migrate deliberately if switching an existing deployment. No cleanup or destroy is run automatically. Pause admission and retain private state backups before updates. For application rollback, keep admission paused, restore the previous tested source/image configuration, review its plan, and apply it through the same fresh drain guard. The control application keeps one warm replica and resources continue to incur charges until deliberately removed.
