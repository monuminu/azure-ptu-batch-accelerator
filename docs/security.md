# Security and compliance notes

Azure mode validates Entra bearer tokens for the configured tenant and accelerator app audience and requires the delegated `Batch.ReadWrite` scope or the `Batch.ReadWrite.Application` application role. The legacy `Batch.ReadWrite` application role remains accepted for existing registrations. Customer administrators own consent, role assignment, and caller lifecycle. The standalone setup script is dry-run unless explicitly invoked with `--apply`; Terraform creates a registration only when explicitly configured to own it, while `azd provision` and CI do not register tenant applications.

One user-assigned managed identity is shared by the three services in v1. The template grants it Blob data access, Functions host storage access, Cosmos DB for NoSQL data-plane contributor, Service Bus Data Owner (needed for trigger scaling runtime properties), ACR pull, Application Insights Monitoring Metrics Publisher, and Cognitive Services OpenAI User plus Reader on the **existing** Azure OpenAI account. Scope each role to the corresponding resource. Review a separate identity per service before deployments with stricter separation requirements.

Payload and Functions storage accounts disable shared keys and public blobs. Cosmos DB and Service Bus disable local/key authentication. Application Insights disables local authentication. Internal workers use internal Container Apps ingress; the API uses external HTTPS ingress. At-rest encryption uses Azure service defaults. The baseline uses public service endpoints, so review network egress, regional residency, private connectivity, and firewall rules for your organization. Service Bus private endpoints require Premium, not the Standard namespace deployed here.

Files and outputs can contain sensitive customer data. Establish a retention/deletion policy for blobs, ledger records, and logs. Avoid recording prompt or response bodies in telemetry. Use customer-approved backup, incident response, and compliance controls. This repository does not certify a compliance standard or implement a data subject workflow for your organization.

## References

- [Azure Container Apps managed identities](https://learn.microsoft.com/azure/container-apps/managed-identity)
- [Azure Functions identity-based connections](https://learn.microsoft.com/azure/azure-functions/manage-connections)
- [Microsoft Entra authentication for Application Insights](https://learn.microsoft.com/azure/azure-monitor/app/azure-ad-authentication)
- [Cosmos DB for NoSQL role-based access](https://learn.microsoft.com/azure/cosmos-db/how-to-connect-role-based-access-control)
- [Service Bus network security](https://learn.microsoft.com/azure/service-bus-messaging/network-security)
