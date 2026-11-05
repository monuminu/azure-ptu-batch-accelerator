# Global PTU showcase

The accelerator's default runtime accepts Regional, Data Zone, and Global provisioned deployments. A Global PTU uses `GlobalProvisionedManaged`; `GlobalStandard` is a separate pay-per-token deployment type.

## Prepare the target

Use an existing provisioned deployment whenever possible. If the operator creates a short-lived demo deployment, first verify the live model catalog, minimum/step, subscription quota, regional capacity, deployment permissions, and current PTU-hour price. The demo workflow does not purchase reservations or configure Standard spillover automatically. The accelerator Terraform stack still does not create or modify the model deployment itself.

Configure the private accelerator variables with the existing account ID and matching endpoint (`openai_account_resource_id` and `openai_resource_endpoint` in Terraform). No deployment name or alias is required there. Supply the actual PTU deployment name in JSONL `body.model`; each file uses one deployment. Pause submission admission, review the saved Terraform plan, and apply it through the durable drain guard. Grant its managed identity Cognitive Services OpenAI User and Reader at the existing account scope through the template's scoped role assignments. Keep file/job ownership and the caller allowlist intact.

Before regular submissions, run `ptu-batch preflight --deployment <actual-ptu-name>` inside the configured control app and verify success using its managed identity. The CLI passes the configured managed identity client ID explicitly. Account-only `ptu-batch preflight` verifies connectivity and account readiness; it reports `ptu_eligibility_checked=false` because the deployment is selected later from JSONL. Confirm the model supports the chosen Chat Completions or stateless Responses API and has no Standard spillover. Then explicitly resume admission.

## Run the notebook

Open [global_ptu_demo.ipynb](../examples/global_ptu_demo.ipynb), set the accelerator URL, tenant and API application ID, sign in with a consented identity, and use a Python 3.12 kernel. Edit `body.model` in the sample or set `INPUT_JSONL_PATH` to an existing file, which will be uploaded unchanged. After the operator has completed target/preflight/admission checks, set:

```python
ENVIRONMENT_IS_VALIDATION_ONLY = False
RUN_REAL_PTU_BATCH = True
```

Run upload → batch create → status/count polling → output/error download → custom-ID correlation. The sample has three generic support tickets. It calls ordinary PTU inference through the real Azure-backed pipeline; it does not use local echo or Standard inference. Failed or unknown results must be investigated rather than hidden or automatically replayed.

The local customer-specific copy may be preconfigured by the operator; the shared repository notebook contains placeholders and no saved outputs. Do not commit identity tokens, private deployment variables, Terraform state, or notebooks containing sensitive results.

## Short-lived PTU cost and cleanup

Provisioned deployments are charged for PTUs deployed and deployed hours, including idle time, with partial hours prorated. Azure reservations can change that price, but a short demonstration must not assume one exists. The caller should verify current [PTU billing](https://learn.microsoft.com/azure/foundry/openai/concepts/provisioned-throughput-billing) and [Azure OpenAI pricing](https://azure.microsoft.com/pricing/details/cognitive-services/openai-service/) before creating capacity.

The notebook's file cleanup does not delete or resize model deployments. Keep a demo deployment only for the required presentation window; when the operator is finished, pause admission, settle accepted work, preserve its outcomes, and explicitly delete the short-lived PTU deployment to stop its hourly charge. No automatic teardown is configured by this guide.
