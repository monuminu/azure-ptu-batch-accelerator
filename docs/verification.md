# Verification report — 0.1.0

Verified locally and in a customer-owned Azure subscription on 4 November 2026. Native Terraform provisioned the platform and deployed all three applications. No existing PTU deployment was available, so Azure acceptance used explicit **validation-only mode** with a read-only Standard reference. The runtime PTU guard stayed enabled, no role was granted on the reference account, and no model inference was invoked. Submission admission was left paused.

## Completed checks

| Check | Result |
| --- | --- |
| Full automated Python suite | **197 passed** |
| Ruff lint and format | Passed |
| Terraform runtime and providers | Terraform **1.15.7**; AzureRM **4.81.0**, AzAPI **2.13.0**, AzureAD **3.10.0**, Random **3.9.1** |
| Terraform format and validation | Passed |
| Native Terraform architecture tests | **10 passed** with mocked providers; no Azure changes from these tests |
| AzureRM backend override | Isolated `init -backend=false -lockfile=readonly` and `validate` passed; remote state was not provisioned or migrated during this validation |
| Live native Terraform apply | Passed, including keyless Storage, Cosmos, Service Bus, private endpoints/DNS, Entra registration, managed identity, images, API, and native `kind=functionapp` workers |
| Post-apply plan | **0 additions, 0 updates, 0 deletions** |
| Live authentication | Health 200; anonymous Files 401; valid accelerator-scoped Entra token accepted |
| Live official SDK Files workflow | Upload, retrieve, download, and delete passed |
| Live large ingress transfer | **209,715,200 bytes (200 MiB)** uploaded with a 4 MiB/s client throttle in **68.34 seconds**, downloaded as a stream in **34.49 seconds**, and matched by SHA-256; file deletion passed |
| Live asynchronous control workflow | A deliberately invalid `method="GET"` job failed validation in **11.29 seconds**, without a model call |
| Live Functions invocation evidence | Successful `outbox_change_feed`, `control_queue`, `maintenance_timer`, and `inference_queue` invocations confirmed in host logs |
| Live inference notification | A notification for an already failed job was consumed, with no request dispatched to a model |
| Native inference wake from zero | Observed **0 → 1 replicas**, then a new successful inference-function invocation and empty active/scheduled/dead-letter queues |
| IaC security scan | Trivy **0.76.1**: **53 supported checks passed, 0 findings**; native AzAPI settings are also covered by explicit architecture assertions |
| Source secret scan | **0 findings** |
| Container builds | API, control, and inference images built and pushed for `linux/amd64` |
| Functions extension bundle | Both workers declare bundle v4; the deployed host loaded **4.39.1** |
| Bicep v0.48.1 | Build and lint passed; Bicep was not the live deployment path |
| Azure Developer CLI project parsing | Passed locally |
| Python wheel and source distribution | Built successfully |
| OpenAPI export | Six paths / nine operations; all 87 local schema references resolve |
| Dependency lock | Public PyPI sources; 1,339 artifact hashes and sizes checked against public PyPI release metadata |

The SDK tests use OpenAI Python **3.42.1**, including synchronous calls over real loopback TCP and asynchronous calls through ASGI. Local container workflow tests cover Chat Completions and Responses upload → submit → poll → download using explicit echo mode. They establish API and packaging behavior, not Azure model quality or throughput. Live platform checks used Azure-backed storage/state and real Entra authentication, not echo mode.

The live validation used private local state for one operator. State, variable files, plan artifacts, manifests, tokens, and detailed cloud logs were kept outside the repository. Azure Storage remote state is the supported choice for shared/production operations; its authentication, network access, and migration must be validated in that target environment.

## Boundaries and failure cases exercised locally

- Streamed 200 MiB multipart input accepted; one byte over the configured limit rejected.
- 100,000 requests validated and admitted; 100,001 requests rejected before inference.
- Whole-file validation, Chat/Responses endpoint aliases, deployment aliases, input/output expiration, owner isolation, pagination, and standard SDK errors.
- Duplicate delivery, interrupted validation/finalization, cancellation during inference, partial results, and continuation beyond the 24-hour target.
- Lost upstream responses become unknown outcomes without automatic replay. Conditional writes prevent duplicate published outcomes.
- Ambiguous Cosmos writes and stale reads preserve input data; reservations provide positive proof before pin cleanup.
- Maintenance admission fencing and in-flight submission tracking. Live guarded image upgrades confirmed the paused/settled gate before revision changes; upgrades with active real PTU calls remain untested.
- Normal queued notifications remain stable during recovery. Missing/expired intents and both dead letter queues are reconciled. A 1,000-row recovery case uses one paged intent scan rather than 1,000 per-row point reads.
- Durable quarantine before acknowledging unresolved/invalid messages, failed-handoff retention, fair pending reconciliation, and recovery past 100 poison messages, including a large AMQP VALUE body.
- Saved-plan digest/provenance checks, changed-input rejection, private backend/variable paths, stateful deletion protection, private Docker credentials, and bounded maintenance-exec retries that fail immediately on explicit unsafe status.

## Fixes confirmed by live Azure validation

The final template uses ARM-only Storage creation to avoid key-authenticated creation polling, private networking compatible with subscription Storage/Cosmos policies, canonical Azure ingress/delegation values, and separate ownership of Entra scope/role fields to prevent repeat-plan drift. A Cosmos region override was exercised when the initial region lacked availability; the default remains colocated with compute.

Live testing also caught an unsupported Blob SDK `etag` argument and missing Functions binding extensions. The Blob fix was reproduced through the real SDK pipeline before correction. Actual Functions host startup and successful trigger invocations established the bundle fix; importing Python entry points alone would not have established registration.

## Reproduce local checks

```bash
uv sync --locked --all-extras
uv run --locked --all-extras pytest -q
uv run --locked --all-extras ruff check .
uv run --locked --all-extras ruff format --check src tests scripts examples
uv run --locked --all-extras python -m build
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform init -backend=false -lockfile=readonly
terraform -chdir=infra/terraform validate
terraform -chdir=infra/terraform test
trivy config --exit-code 1 infra/terraform
bicep build infra/main.bicep
```

See the [Terraform guide](terraform-deployment.md) for private saved-plan review/apply, remote state, controlled platform-only validation, and rollback. Image builds used the optional trusted package-index override because the local network blocked public wheel downloads; the checked-in default and dependency lock remain public PyPI. Docker authentication used a temporary private configuration.

## Azure acceptance still required with a real PTU

Real PTU inference, model/API capability, multimodal and structured outputs against that model, sustained effective concurrency, worker failure after remote dispatch, active-inference upgrades, long-running retention/24-hour continuation, and throughput/cost savings still require a configured PTU. The 100,000-request limit was exercised locally, not as a real Azure inference load test. Private-only PTUs also require the customer's existing DNS/routing connectivity.

Completed model token volume during this validation was **0**. No avoided Batch spend is claimed. The deployed support resources continue to incur Azure costs; compare completed token usage with operator-supplied native Batch comparison rates and actual Azure infrastructure charges. Billing totals were not established by this short platform-only run.
