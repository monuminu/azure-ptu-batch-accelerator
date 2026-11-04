targetScope = 'subscription'

@description('Short azd environment name, used in resource names and tags.')
@minLength(1)
@maxLength(14)
param environmentName string

@description('Azure region for the accelerator resources.')
param location string

@description('Tenant ID expected in API access tokens.')
param tenantId string

@description('Application/client ID of the customer-owned accelerator API registration.')
param acceleratorAppId string

@description('Full resource ID of one existing Azure OpenAI PTU deployment. No deployment is created or modified.')
param ptuDeploymentResourceId string

@description('HTTPS endpoint of the existing Azure OpenAI account.')
param ptuResourceEndpoint string

@description('Name of the existing PTU deployment.')
param ptuDeploymentName string

@description('Public alias exposed by the accelerator API.')
param ptuAlias string = 'ptu'

@description('Allowed API paths for this deployment. The runtime validates these paths.')
param allowedApis array = ['/chat/completions', '/responses']

@description('Optional subject allowlist. Empty means the API permission decides access.')
param allowedSubjects array = []

@description('Provided by azd: whether API exists before this provision.')
param apiExists bool = false

@description('Provided by azd: whether control Functions exists before this provision.')
param controlExists bool = false

@description('Provided by azd: whether inference Functions exists before this provision.')
param inferenceExists bool = false

var resourceGroupName = 'rg-${environmentName}-ptu-batch'
var resourceIdParts = split(ptuDeploymentResourceId, '/')
var aoaiSubscriptionId = resourceIdParts[2]
var aoaiResourceGroupName = resourceIdParts[4]
var aoaiAccountName = resourceIdParts[8]

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: {
    'azd-env-name': environmentName
    project: 'azure-ptu-batch-accelerator'
  }
}

module resources './resources.bicep' = {
  name: 'ptu-batch-resources'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
    tenantId: tenantId
    acceleratorAppId: acceleratorAppId
    ptuDeploymentResourceId: ptuDeploymentResourceId
    ptuResourceEndpoint: ptuResourceEndpoint
    ptuDeploymentName: ptuDeploymentName
    ptuAlias: ptuAlias
    allowedApis: allowedApis
    allowedSubjects: allowedSubjects
    apiExists: apiExists
    controlExists: controlExists
    inferenceExists: inferenceExists
  }
}

// Role assignments on the existing account are isolated in its resource group.
module aoaiRole './aoai-role.bicep' = {
  name: 'ptu-batch-existing-aoai-access'
  scope: resourceGroup(aoaiSubscriptionId, aoaiResourceGroupName)
  params: {
    accountName: aoaiAccountName
    identityPrincipalId: resources.outputs.identityPrincipalId
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.registryEndpoint
output API_ENDPOINT_URL string = resources.outputs.apiEndpoint
output API_NAME string = resources.outputs.apiName
output CONTROL_NAME string = resources.outputs.controlName
output INFERENCE_NAME string = resources.outputs.inferenceName
output SERVICEBUS_NAMESPACE string = resources.outputs.serviceBusNamespace
output INFERENCE_QUEUE string = 'batch-inference'
