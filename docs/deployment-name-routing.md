# Actual deployment names in JSONL

## Client contract

`body.model` is the Azure OpenAI deployment name chosen by the user. For example:

```jsonl
{"custom_id":"task-0","method":"POST","url":"/v1/chat/completions","body":{"model":"accelerator-demo-global-ptu","messages":[{"role":"user","content":"When was Microsoft founded?"}]}}
{"custom_id":"task-1","method":"POST","url":"/v1/chat/completions","body":{"model":"accelerator-demo-global-ptu","messages":[{"role":"user","content":"When was the first XBOX released?"}]}}
{"custom_id":"task-2","method":"POST","url":"/v1/chat/completions","body":{"model":"accelerator-demo-global-ptu","messages":[{"role":"user","content":"What is Altair Basic?"}]}}
```

Set the name in the file; there is no alias registration call or per-deployment map update. The official SDK upload, create-batch, poll, cancel and download methods are unchanged. The examples use a replacement marker so customers do not accidentally call an assumed deployment.

## One-time operator account setup

Configure the trusted account ARM ID and matching HTTPS endpoint once. Terraform uses `openai_account_resource_id` and `openai_resource_endpoint`; direct runtime configuration uses `PTU_BATCH_DEPLOYMENT_ACCOUNT` JSON with `resource_id`, `resource_endpoint`, `allowed_apis` and optional `allowed_subjects`.

Azure deployment names are local to an account; a name alone cannot identify arbitrary resources across accounts. The client cannot choose an account endpoint, ARM scope or arbitrary URL inside JSONL. Managed identity and Reader/OpenAI User roles are scoped to that configured account.

## Validation and durability

The API snapshots the account settings at batch acceptance. Whole-file validation resolves the literal deployment name into that snapshot and requires unique custom IDs plus one deployment and normalized API endpoint per file. Unknown/missing names, URL/path-like names, unsupported APIs, unauthorized callers and mixed deployments fail validation. ARM preflight additionally requires an existing provisioned Regional/Global/Data Zone SKU, positive capacity, successful provisioning and no Standard spillover. Model-specific declared API incompatibility is rejected. Standard/GlobalStandard/DataZoneStandard are not PTUs.

Dispatch begins only after the whole file and deployment checks succeed. The resolved target is committed with the job, and preflight is repeated during execution. Changing server settings later does not redirect an accepted job. Successful ARM checks are cached for up to five minutes; pause and drain workloads before changing a selected deployment externally. Definite missing/non-PTU/spillover/API-incompatible targets fail the batch with `batch.errors` pointing to `body.model`. Transient ARM throttling, network or service errors remain retryable after the control lease expires. The existing outcome leases, retry rules, cancellation, finalization and ownership checks are unchanged.

## Compatibility

Old explicit `PTU_BATCH_DEPLOYMENTS` alias maps and already accepted alias-mode jobs remain supported. When an account snapshot is present, new jobs treat `body.model` as the real deployment name; they do not silently fall back to an alias. Update client files that previously used a label such as `ptu` to the actual name unless that really is the deployment's Azure name.

Local echo samples are explicitly named `local-chat.jsonl` and `local-responses.jsonl`. They are for offline contract testing and do not establish Azure PTU throughput or savings.
