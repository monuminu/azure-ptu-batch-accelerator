targetScope = 'resourceGroup'

param environmentName string
param location string
param tenantId string
param acceleratorAppId string
param ptuAccountResourceId string
param ptuDeploymentResourceId string
param ptuResourceEndpoint string
param ptuDeploymentName string
param ptuAlias string
param allowedApis array
param allowedSubjects array
param apiExists bool
param controlExists bool
param inferenceExists bool

var suffix = uniqueString(subscription().id, resourceGroup().id, environmentName)
var prefix = take(replace(toLower(environmentName), '-', ''), 8)
var deployments = {
  '${ptuAlias}': {
    resource_endpoint: ptuResourceEndpoint
    deployment_name: ptuDeploymentName
    resource_id: ptuDeploymentResourceId
    allowed_apis: allowedApis
    allowed_subjects: allowedSubjects
  }
}
var deploymentAccount = {
  resource_endpoint: ptuResourceEndpoint
  resource_id: ptuAccountResourceId
  allowed_apis: allowedApis
  allowed_subjects: allowedSubjects
}
var routingEnv = !empty(ptuAccountResourceId) ? [
  { name: 'PTU_BATCH_DEPLOYMENT_ACCOUNT', value: string(deploymentAccount) }
] : [
  { name: 'PTU_BATCH_DEPLOYMENTS', value: string(deployments) }
]
var appName = 'ca-${environmentName}-ptu-api'
var controlName = 'ca-${environmentName}-ptu-control'
var inferenceName = 'ca-${environmentName}-ptu-inference'
var rolePrefix = '/subscriptions/${subscription().subscriptionId}/providers/Microsoft.Authorization/roleDefinitions/'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${environmentName}-ptu-batch'
  location: location
}

resource log 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${environmentName}-ptu-batch'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${environmentName}-ptu-batch'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: log.id
    DisableLocalAuth: true
  }
}

resource insightsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, identity.id, 'monitoring-metrics-publisher')
  scope: insights
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: '${rolePrefix}3913510d-42f4-4e42-8a64-420c390055eb'
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'acr${prefix}${suffix}'
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, 'acr-pull')
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: '${rolePrefix}7f951dda-4ed3-4680-a7ca-43fe172d538d'
  }
}

resource payload 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'st${prefix}${suffix}'
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
  }
}

resource payloadBlob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: payload
  name: 'default'
}

resource files 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: payloadBlob
  name: 'batch-files'
  properties: { publicAccess: 'None' }
}

resource payloadWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(payload.id, identity.id, 'blob-data-contributor')
  scope: payload
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: '${rolePrefix}ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  }
}

resource controlStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'sc${prefix}${suffix}'
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
  }
}

resource inferenceStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'si${prefix}${suffix}'
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
  }
}

// Functions host storage needs blob, queue and account management permissions.
resource controlHostBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(controlStorage.id, identity.id, 'host-blob-owner')
  scope: controlStorage
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: '${rolePrefix}b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  }
}
resource controlHostQueue 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(controlStorage.id, identity.id, 'host-queue-contributor')
  scope: controlStorage
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: '${rolePrefix}974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  }
}
resource controlHostAccount 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(controlStorage.id, identity.id, 'host-account-contributor')
  scope: controlStorage
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: '${rolePrefix}17d1049b-9a84-46fb-8f53-869881c3d3ab'
  }
}
resource inferenceHostBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(inferenceStorage.id, identity.id, 'host-blob-owner')
  scope: inferenceStorage
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: '${rolePrefix}b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  }
}
resource inferenceHostQueue 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(inferenceStorage.id, identity.id, 'host-queue-contributor')
  scope: inferenceStorage
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: '${rolePrefix}974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  }
}
resource inferenceHostAccount 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(inferenceStorage.id, identity.id, 'host-account-contributor')
  scope: inferenceStorage
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: '${rolePrefix}17d1049b-9a84-46fb-8f53-869881c3d3ab'
  }
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: 'cosmos-${environmentName}-${suffix}'
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [{ locationName: location, failoverPriority: 0 }]
    capabilities: [{ name: 'EnableServerless' }]
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmos
  name: 'ptu-batch'
  properties: { resource: { id: 'ptu-batch' } }
}

resource ledger 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'ledger'
  properties: {
    resource: {
      id: 'ledger'
      partitionKey: { paths: ['/pk'], kind: 'Hash' }
    }
  }
}

resource leases 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'leases'
  properties: {
    resource: {
      id: 'leases'
      partitionKey: { paths: ['/id'], kind: 'Hash' }
    }
  }
}

resource cosmosData 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmos
  name: guid(cosmos.id, identity.id, 'sql-data-contributor')
  properties: {
    principalId: identity.properties.principalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: cosmos.id
  }
}

resource bus 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: 'sb-${environmentName}-${suffix}'
  location: location
  sku: { name: 'Standard', tier: 'Standard' }
  properties: {
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource controlQueue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = {
  parent: bus
  name: 'batch-control'
  properties: {
    lockDuration: 'PT5M'
    maxDeliveryCount: 10
    defaultMessageTimeToLive: 'P14D'
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'P7D'
    deadLetteringOnMessageExpiration: true
  }
}

resource inferenceQueue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = {
  parent: bus
  name: 'batch-inference'
  properties: {
    lockDuration: 'PT5M'
    maxDeliveryCount: 10
    defaultMessageTimeToLive: 'P14D'
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'P7D'
    deadLetteringOnMessageExpiration: true
  }
}

// The Service Bus trigger scaler reads queue runtime properties; Data Owner is required.
resource busOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(bus.id, identity.id, 'servicebus-data-owner')
  scope: bus
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: '${rolePrefix}090c5cfd-751d-490a-894a-3ce6f1109419'
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${environmentName}-ptu-batch'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: log.properties.customerId
        sharedKey: log.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}

var commonEnv = concat([
  { name: 'PTU_BATCH_MODE', value: 'azure' }
  { name: 'PTU_BATCH_TENANT_ID', value: tenantId }
  { name: 'PTU_BATCH_AUDIENCE', value: acceleratorAppId }
  { name: 'PTU_BATCH_REQUIRED_PERMISSION', value: 'Batch.ReadWrite' }
  { name: 'PTU_BATCH_APPLICATION_ROLE', value: 'Batch.ReadWrite.Application' }
  { name: 'PTU_BATCH_COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
  { name: 'PTU_BATCH_COSMOS_DATABASE', value: 'ptu-batch' }
  { name: 'PTU_BATCH_COSMOS_CONTAINER', value: 'ledger' }
  { name: 'PTU_BATCH_COSMOS_LEASE_CONTAINER', value: 'leases' }
  { name: 'PTU_BATCH_STORAGE_ACCOUNT_URL', value: 'https://${payload.name}.blob.${az.environment().suffixes.storage}' }
  { name: 'PTU_BATCH_BLOB_CONTAINER', value: 'batch-files' }
  { name: 'PTU_BATCH_SERVICEBUS_FQDN', value: '${bus.name}.servicebus.windows.net' }
  { name: 'PTU_BATCH_CONTROL_QUEUE', value: controlQueue.name }
  { name: 'PTU_BATCH_INFERENCE_QUEUE', value: inferenceQueue.name }
  { name: 'PTU_BATCH_MANAGED_IDENTITY_CLIENT_ID', value: identity.properties.clientId }
  { name: 'PTU_BATCH_INFERENCE_CONCURRENCY', value: '8' }
  { name: 'PTU_BATCH_INFERENCE_TIMEOUT_SECONDS', value: '300' }
  { name: 'PTU_BATCH_REQUEST_LEASE_SECONDS', value: '660' }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
  { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'Authorization=AAD;ClientId=${identity.properties.clientId}' }
], routingEnv)

var functionEnv = [
  { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
  { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
  { name: 'FUNCTIONS_WORKER_PROCESS_COUNT', value: '1' }
  { name: 'ServiceBusConnection__fullyQualifiedNamespace', value: '${bus.name}.servicebus.windows.net' }
  { name: 'ServiceBusConnection__credential', value: 'managedidentity' }
  { name: 'ServiceBusConnection__clientId', value: identity.properties.clientId }
  { name: 'CosmosConnection__accountEndpoint', value: cosmos.properties.documentEndpoint }
  { name: 'CosmosConnection__credential', value: 'managedidentity' }
  { name: 'CosmosConnection__clientId', value: identity.properties.clientId }
]

// azd's existence flags prevent a later azd provision/up from reverting deployed images.
// Fetch the current image before invoking the nested deployment module, as in the
// official container-app-upsert pattern.
resource existingApi 'Microsoft.App/containerApps@2026-01-01' existing = if (apiExists) {
  name: appName
}
resource existingControl 'Microsoft.App/containerApps@2026-01-01' existing = if (controlExists) {
  name: controlName
}
resource existingInference 'Microsoft.App/containerApps@2026-01-01' existing = if (inferenceExists) {
  name: inferenceName
}

module api './api-app.bicep' = {
  name: 'ptu-batch-api-app'
  params: {
    name: appName
    location: location
    environmentName: environmentName
    environmentId: environment.id
    identityId: identity.id
    registryServer: registry.properties.loginServer
    image: apiExists ? existingApi!.properties.template.containers[0].image : 'python:3.12-slim'
    env: commonEnv
  }
  dependsOn: [acrPull, payloadWriter, cosmosData, busOwner, insightsPublisher]
}

module control './function-app.bicep' = {
  name: 'ptu-batch-control-app'
  params: {
    serviceName: 'control'
    name: controlName
    location: location
    environmentName: environmentName
    environmentId: environment.id
    identityId: identity.id
    identityClientId: identity.properties.clientId
    registryServer: registry.properties.loginServer
    hostStorageName: controlStorage.name
    image: controlExists ? existingControl!.properties.template.containers[0].image : 'mcr.microsoft.com/azure-functions/python:4-python3.12'
    env: concat(commonEnv, functionEnv)
    // Cosmos change feed with managed identity cannot wake from zero on ACA.
    minReplicas: 1
  }
  dependsOn: [acrPull, controlHostBlob, controlHostQueue, controlHostAccount, cosmosData, busOwner, insightsPublisher]
}

module inference './function-app.bicep' = {
  name: 'ptu-batch-inference-app'
  params: {
    serviceName: 'inference'
    name: inferenceName
    location: location
    environmentName: environmentName
    environmentId: environment.id
    identityId: identity.id
    identityClientId: identity.properties.clientId
    registryServer: registry.properties.loginServer
    hostStorageName: inferenceStorage.name
    image: inferenceExists ? existingInference!.properties.template.containers[0].image : 'mcr.microsoft.com/azure-functions/python:4-python3.12'
    env: concat(commonEnv, functionEnv)
    minReplicas: 0
  }
  dependsOn: [acrPull, inferenceHostBlob, inferenceHostQueue, inferenceHostAccount, cosmosData, busOwner, insightsPublisher]
}

output identityPrincipalId string = identity.properties.principalId
output registryEndpoint string = registry.properties.loginServer
output apiEndpoint string = 'https://${api.outputs.fqdn}'
output apiName string = api.outputs.name
output controlName string = control.outputs.name
output inferenceName string = inference.outputs.name
output serviceBusNamespace string = bus.name
