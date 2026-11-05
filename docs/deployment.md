# Deploy to Azure

This procedure provisions accelerator infrastructure and role assignments. It does **not** create or change an Azure OpenAI deployment. Run it in a customer-owned subscription with an existing PTU deployment after reviewing the [operations runbook](operations.md). Provisioning, deployment, and smoke jobs incur charges.

## Prerequisites

- Azure CLI, Azure Developer CLI (`azd`), Docker, Python 3.12, and `uv`.
- An existing Azure OpenAI **provisioned throughput** deployment with spillover disabled, reachable from the Container Apps environment. Private-only PTUs require VNet/private DNS configuration beyond this baseline. Record the trusted account ARM ID and matching Azure OpenAI account endpoint. Users select the actual deployment name in JSONL `body.model`; no deployment-name or alias map is required on the server. Whole-file validation checks the chosen deployment and rejects non-PTU or spillover configuration.
- Subscription-scope permission for `Microsoft.Resources/deployments/write` and `Microsoft.Resources/subscriptions/resourceGroups/write`: the template is a subscription deployment and creates the accelerator resource group. The provisioner also needs permission to create resources and role assignments in that group. Subscription **Contributor** plus **User Access Administrator** (or equivalent rights) covers the accelerator's first provision; resource-group-only permissions do not. On the existing Azure OpenAI account, the provisioner needs **Reader** and `Microsoft.Authorization/roleAssignments/write`; it also needs `Microsoft.Resources/deployments/write` on that account's resource group because Bicep deploys the role-assignment module there. This can span subscriptions in the same tenant. The template grants the runtime identity `Cognitive Services OpenAI User` and `Reader` on the existing account.
- `AcrPush` on the new registry for the principal running `azd deploy`, so azd can push the three images. This is separate from the runtime identity's `AcrPull` assignment. Grant it after `azd provision` if the deployer does not already have equivalent rights.
- An Entra API app/client ID in the same tenant. The API expects audience equal to this app ID and the `Batch.ReadWrite` delegated scope or `Batch.ReadWrite.Application` application role.

## Customer-owned Entra API registration

An administrator may create the API app manually or run `python scripts/create_entra_app.py --display-name "PTU Batch API" --apply`. The script is dry-run without `--apply`; it is never run by `azd`, CI, or the templates. It creates a delegated `Batch.ReadWrite` scope, a distinct `Batch.ReadWrite.Application` application role, and a service principal. Review the tenant changes before invoking it. Grant delegated consent or assign the application role to each caller separately; creating the app alone does not grant callers access. Clients request `api://<APP_ID>/.default` and send the resulting bearer token as the OpenAI SDK `api_key`.

## Configure azd

From the repository root, initialize/select a local azd environment. This repository already contains `azure.yaml`; `azd init` can be used to select the project when starting from a copy, then `azd env new <name>` creates a local environment. Use a lowercase name of at most 14 letters, digits, or interior hyphens, beginning and ending with a letter or digit, so all Container App names remain valid. The following variables feed [infra/main.parameters.json](../infra/main.parameters.json):

```bash
azd auth login
az login --tenant '<tenant-guid>'
az account set --subscription '<accelerator-subscription-guid>'
azd env new dev
azd env set AZURE_SUBSCRIPTION_ID '<accelerator-subscription-guid>'
azd env set AZURE_LOCATION eastus
azd env set PTU_BATCH_TENANT_ID '<tenant-guid>'
azd env set PTU_BATCH_AUDIENCE '<accelerator-api-app-guid>'
azd env set PTU_BATCH_PTU_ACCOUNT_RESOURCE_ID '/subscriptions/<subscription>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>'
azd env set PTU_BATCH_PTU_RESOURCE_ENDPOINT 'https://<account>.openai.azure.com/'
```

No keys or secrets are required for runtime connections. Keep `.azure/` and local credential files out of source control. The `PTU_BATCH_PTU_ACCOUNT_RESOURCE_ID` and `PTU_BATCH_PTU_RESOURCE_ENDPOINT` values identify one trusted account. Its endpoint must match the ARM account endpoint. The template permits Chat Completions and Responses, subject to runtime deployment capability checks. A user supplies the actual PTU name in the uploaded JSONL. Legacy explicit deployment/alias parameters remain supported in Bicep when the account parameter is omitted. Existing azd environments should set the new account variable for the supplied parameter file, or supply their own legacy parameter file.

## Provision and deploy

```bash
uv sync --locked --all-extras
python3 scripts/preflight_from_azd.py
azd provision
azd deploy inference
azd deploy control
azd deploy api
azd show
```

Azure admission starts **paused**; local mode starts unpaused. The first provision bypasses the gate only when all three apps are absent. Deploy **inference, control, then API**. While API and control still have their known placeholders, no batch can be admitted, so the first two deploy commands bypass the durable maintenance check. The API deploy requires the control app's `ptu-batch maintenance --check` to return `PTU_BATCH_DRAIN_SAFE` (a missing initial gate counts as paused with no work). The root `preprovision` and `predeploy` hooks, plus the inference service's `predeploy` hook, enforce these checks. On upgrades, they require a durable paused gate with no nonterminal jobs, pending outbox work, or active submission leases; both queues must have zero active and scheduled messages and the old inference revision must have zero replicas. The provision and inference-deploy hooks then **deactivate the old active inference revision** before a new revision can be created. The Azure CLI login must target the same subscription as azd.

After all three apps are healthy, explicitly resume admission:

```bash
CONTROL_NAME=$(azd env get-value CONTROL_NAME)
RESOURCE_GROUP=$(azd env get-value AZURE_RESOURCE_GROUP)
az containerapp exec --name "$CONTROL_NAME" --resource-group "$RESOURCE_GROUP" --container control --command 'ptu-batch maintenance --check'
az containerapp exec --name "$CONTROL_NAME" --resource-group "$RESOURCE_GROUP" --container control --command 'ptu-batch maintenance --resume'
```

No hook resumes automatically. The opt-in **billable** smoke job below must be run after resume; perform revision and read-only health checks first.

The Bicep app modules use azd's `SERVICE_{NAME}_RESOURCE_EXISTS` flags to retain each app's current image during later `azd provision` runs. Reprovisioning can still change app configuration and create a revision, so the root `preprovision` guard runs before `azd provision` and `azd up`. A successful reprovision may leave inference deactivated until a new image is deployed or the previous revision is deliberately reactivated. Use the [upgrade procedure](operations.md#inference-upgrade-procedure) before any revision change.

The templates create ACR, one user-assigned managed identity, Blob payload storage with a precreated `batch-files` container, separate Functions host storage accounts, Cosmos DB for NoSQL serverless with `ledger` and `leases` containers, Service Bus Standard with two queues, Log Analytics, Application Insights, and one shared Container Apps Consumption environment. API ingress is external; Functions ingress is internal. The inference app has one active revision and scales 0–1 replica through its native Service Bus trigger; API scales 0–2 replicas. Native Functions derives the trigger scaler, so the template sets only replica limits. Control stays at one replica because its Cosmos change-feed trigger uses managed identity and cannot reliably wake from zero in ACA. This warm replica incurs compute charges. All Functions use one Python worker process per replica.

The first deployment may need a short delay for managed identity role propagation before Functions become healthy. Check container app revision health, Functions logs, and scaler logs rather than replacing identity connections with keys. Application Insights uses Entra-authenticated telemetry ingestion.

## Opt-in smoke test

The script prints the selected sample in dry-run mode and makes no Azure calls. `--create-job` uses a refreshing Entra token provider, uploads one JSONL file, creates a **billable** batch, polls for completion (900 seconds by default), downloads its output, and verifies the expected `custom_id` values, HTTP 2xx responses, and zero failed requests. It exits nonzero on failure or timeout and requests cancellation of an unfinished job on timeout. The JSONL `body.model` is preserved by default. Edit the sample replacement marker or pass your own file with `--input-file`. An optional `--deployment-name` overrides the model in the client-side sample only; it is not server configuration:

```bash
uv run --locked --all-extras python scripts/smoke_test.py \
  --base-url 'https://<api-fqdn>/openai/v1/' \
  --app-id '<accelerator-api-app-guid>' --input-file ./chat.jsonl

uv run --locked --all-extras python scripts/smoke_test.py \
  --base-url 'https://<api-fqdn>/openai/v1/' \
  --app-id '<accelerator-api-app-guid>' --input-file ./chat.jsonl --create-job
```

The manually dispatched GitHub workflow uses OIDC and also requires an explicit `create_job` input. It never deploys resources. The GitHub OIDC identity must itself be granted the `Batch.ReadWrite.Application` application role; Azure subscription RBAC alone does not authorize the API.

## Network and data policy

This baseline uses public Azure service endpoints with Entra authorization and TLS. It does not configure private endpoints, a managed VNet, custom egress firewall, or customer data retention policy. Restrict outbound access and configure private connectivity according to the customer's policy before production use. Service Bus Standard in this template does not support private endpoints; use Premium for that topology. See [security notes](security.md).

## Sources

- [Azure Functions on Azure Container Apps overview](https://learn.microsoft.com/azure/container-apps/functions-overview)
- [Containerized Functions on Container Apps](https://learn.microsoft.com/azure/container-apps/functions-container-apps)
- [Identity-based Functions connections](https://learn.microsoft.com/azure/azure-functions/manage-connections)
- [Service Bus trigger scaling permissions](https://learn.microsoft.com/azure/azure-functions/functions-bindings-service-bus-trigger)
- [Azure Developer CLI schema](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-schema)
- [Azure Developer CLI container app deployment](https://learn.microsoft.com/azure/developer/azure-developer-cli/container-apps-workflows)
- [Azure Container Registry roles for image push](https://learn.microsoft.com/azure/container-registry/container-registry-rbac-built-in-roles-overview)
