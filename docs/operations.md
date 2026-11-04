# Operations and recovery

The `24h` completion window is a scheduling target and API compatibility value, not a promise that every batch finishes in 24 hours. Existing PTU utilization, Azure OpenAI throttling, row mix, and worker availability affect elapsed time. The accelerator does not schedule around PTU idle windows. Cancelling a batch prevents new work from being taken where possible; an in-flight Azure OpenAI request is not preempted.

## Observe a batch

Use the API's batch status, request counts, output file, and error file. Then correlate the batch ID and row `custom_id` with structured logs in Application Insights. Review Service Bus `batch-control` and `batch-inference` active, dead-letter, and scheduled message counts; Function invocation failures; Cosmos DB throttling; and Azure OpenAI PTU utilization/throttling. Service Bus lock duration is five minutes. Request leases are eleven minutes; inference timeout is five minutes. A lease is a recovery guard, not a guarantee of exactly-once calls.

## Recovery decisions

1. **A row has a definitive response or error:** preserve its recorded outcome. Reconcile output/error files from ledger state if finalization was interrupted.
2. **An outbox dispatch is pending:** retry the dispatch using its stable message identity after verifying the ledger state. Duplicate detection is enabled on the queues for seven days, but ledger checks remain necessary.
3. **A lease expired with no definitive Azure OpenAI outcome:** mark it unknown and investigate request logs. The default policy does not replay unknown requests, because retrying could charge twice or repeat effects. A customer operator can choose an explicit retry only after evaluating this risk.
4. **Dead-lettered messages:** maintenance redrives notifications only after checking ledger state. Unresolved notifications are durably handed to Cosmos quarantine before their original message is acknowledged. Pending quarantine is retried fairly; malformed bodies require manual triage. Do not bulk replay unknown model outcomes.
5. **Control or finalization failure:** recover from the ledger and blob state; avoid deleting source or result blobs until the batch and retention obligations are resolved.

The payload container, ledger, and logs have different retention needs. The runtime enforces requested input/output file expirations after active pins clear. Without a requested expiration, the template imposes no payload deletion policy. Define the remaining customer retention, backup, access, and legal-hold policies before production. Log Analytics is configured for 30 days; ensure this meets the customer's investigation period and avoid logging prompts or response bodies unless explicitly approved.

## Inference upgrade procedure

Pause admission durably in Cosmos DB, then let already admitted jobs finish. The API returns 503 for batch creation while paused; read and cancel operations remain available. The maintenance check prints `PTU_BATCH_DRAIN_SAFE` only when admission is paused and there are no nonterminal jobs, pending outbox records, pending batch reservations, or active submission leases. It exits nonzero while work remains. Record the current active inference revision name for rollback.

```bash
CONTROL_NAME=$(azd env get-value CONTROL_NAME)
INFERENCE_NAME=$(azd env get-value INFERENCE_NAME)
RESOURCE_GROUP=$(azd env get-value AZURE_RESOURCE_GROUP)
az containerapp revision list --name "$INFERENCE_NAME" --resource-group "$RESOURCE_GROUP" --query '[?properties.active].name' --output table
az containerapp exec --name "$CONTROL_NAME" --resource-group "$RESOURCE_GROUP" --container control --command 'ptu-batch maintenance --pause'
az containerapp exec --name "$CONTROL_NAME" --resource-group "$RESOURCE_GROUP" --container control --command 'ptu-batch maintenance --check'
azd deploy inference
```

The root `preprovision` and `predeploy` hooks check the durable gate before changes. The inference service's `predeploy` hook repeats the check, waits for zero active **and scheduled** messages in both queues and zero old inference replicas twice, then deactivates the old active inference revision. A `preprovision` hook also deactivates it before Bicep can create a configuration revision. If a check fails or the 30-minute drain times out, the operation stops and admission remains paused. Do not force a revision change around the hooks.

After the new revision is healthy, run read-only health/status checks. Then explicitly resume admission and, if approved, run a billable smoke batch:

```bash
az containerapp exec --name "$CONTROL_NAME" --resource-group "$RESOURCE_GROUP" --container control --command 'ptu-batch maintenance --resume'
```

No hook resumes automatically. If deployment fails after the old revision was deactivated, keep admission paused. Inspect the failure, then reactivate the recorded revision and verify it is healthy before resuming:

```bash
az containerapp revision activate --name "$INFERENCE_NAME" --resource-group "$RESOURCE_GROUP" --revision '<recorded-old-revision>'
az containerapp exec --name "$CONTROL_NAME" --resource-group "$RESOURCE_GROUP" --container control --command 'ptu-batch maintenance --check'
az containerapp exec --name "$CONTROL_NAME" --resource-group "$RESOURCE_GROUP" --container control --command 'ptu-batch maintenance --resume'
```

The gate covers work admitted through this API. Direct writes to its queues or ledger by another privileged producer bypass the admission protocol; coordinate those producers before upgrades. `activeRevisionsMode: Single` limits inference to one active revision. A previously unknown Azure OpenAI outcome remains unknown after rollback and is not replayed automatically.

## Usage report

The operator CLI reports known input/output tokens and completed/failed request counts from the ledger. It can estimate an alternative Batch cost only when **you supply** input and output rates per million tokens, and it can subtract actual infrastructure spend only when **you supply** that spend for the same reporting period. It does not fetch prices, estimate PTU opportunity cost, or include unknown inference outcomes in known usage.

```bash
az containerapp exec --name "$CONTROL_NAME" --resource-group "$RESOURCE_GROUP" --container control --command 'ptu-batch usage'
az containerapp exec --name "$CONTROL_NAME" --resource-group "$RESOURCE_GROUP" --container control --command 'ptu-batch usage --input-rate <your-input-rate-per-million> --output-rate <your-output-rate-per-million> --infrastructure-cost <your-actual-spend>'
```

Check your billing source and contract for current rates. Treat the calculated net figure as a comparison using operator-supplied assumptions, not as a guaranteed saving.

## Cost and quota

The PTU deployment continues billing according to its Azure OpenAI terms. The accelerator also consumes Container Apps, Service Bus Standard, Cosmos DB serverless RUs/storage, Blob and Functions host storage, ACR, Log Analytics, and Application Insights. Control keeps one warm 1-vCPU/2-GiB replica so its managed-identity Cosmos change feed and recovery timer keep running. API and inference may scale to zero, but this is not a zero-cost deployment. Monitor queue depth, oldest-message age, throttling, Cosmos RU use, storage growth, and telemetry ingestion. Increase concurrency only after measuring PTU capacity and endpoint latency; max replica count is intentionally one for inference.

## Limits

- Maximum input: 200 MB and 100,000 JSONL lines. Client upload and API processing windows may require a lower practical size in a particular environment; verify with an opt-in large-upload test.
- Chat Completions and Responses are the supported row APIs. Embeddings, streaming, background mode, stateful tools, remote file IDs, and BYOS are unsupported.
- No exactly-once guarantee for a network request whose response was lost. Unknown outcomes are not replayed automatically.
- No native Azure OpenAI Batch management experience or SLA.

## Ambiguous storage commits

A lost Cosmos write response is not proof that a write failed. The service retains an input Blob after an uncertain File-metadata write and logs its generated `file_id` without logging file contents. Such an upload can require operator reconciliation if its metadata was never committed. Do not remove it based on a single missing read. Apply the customer's retention policy after checking the ledger, pins, and upload audit records.

Batch creation uses a reservation in the batch partition before registering its input pin. Recovery aborts abandoned reservations with an ETag condition and releases pins only after observing an aborted reservation or terminal batch. Missing owner records are retained for subsequent reconciliation. The maintenance fence includes pending reservations.

Intermediate per-attempt result Blobs are reclaimed in bounded maintenance passes after terminal-job grace; published output and error files retain their own expiration policy.

## Container builds

Images target `linux/amd64`, including when built from an ARM developer machine. The Functions base image installs Python console scripts in `/opt/python/3/bin`; the worker Dockerfiles include that directory in `PATH`.

The Dockerfiles use public PyPI by default. A trusted **unauthenticated** corporate package mirror can be supplied with `--build-arg PIP_INDEX_URL=https://<trusted-index>/simple`. Do not pass credentials through build arguments; use a BuildKit secret for an authenticated index. The checked-in lock uses public PyPI artifacts and hashes.

## Durable queues and dead letter quarantine

Maintenance trusts healthy published notifications rather than changing the generation of every queued request. It restores missing intents and checks published notifications against the queues' 14-day lifetime. The recovery scan uses one paged latest-intent read per active batch, rather than one point read per pending row. If queue TTL is changed, update the matching recovery lifetime.

Control and inference dead letters are processed in bounded batches. A recoverable notification is acknowledged only after a durable recovery decision. Unresolved or malformed notifications are content-addressed in the ledger under partition `system:deadletters`, kind `dead_letter_quarantine`, before their original message is acknowledged. This prevents poison messages from blocking valid work behind them. Pending records rotate fairly through reconciliation; manual records remain for operator investigation.

Quarantine can contain sensitive malformed message content and is never exposed by the public Files/Batch API or emitted in logs. Apply a customer-approved retention and access policy to it. `raw_body_b64` plus `raw_encoding` preserves the body: DATA bytes use the default raw format; AMQP VALUE strings use `value-utf8`, byte values use `value-bytes`, and structured values use `value-json-zlib` (base64 decode, then zlib decompress and parse JSON). Reconcile ledger state before any manual replay.
