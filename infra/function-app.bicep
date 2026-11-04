targetScope = 'resourceGroup'

@allowed(['control', 'inference'])
param serviceName string
param name string
param location string
param environmentName string
param environmentId string
param identityId string
param identityClientId string
param registryServer string
param hostStorageName string
param image string
param env array
param minReplicas int

resource app 'Microsoft.App/containerApps@2026-01-01' = {
  name: name
  kind: 'functionapp'
  location: location
  tags: { 'azd-service-name': serviceName, 'azd-env-name': environmentName }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [{ server: registryServer, identity: identityId }]
      ingress: { external: false, targetPort: 80, transport: 'auto', allowInsecure: false }
    }
    template: {
      containers: [{
        name: serviceName
        image: image
        resources: { cpu: 1, memory: '2Gi' }
        env: concat(env, [
          { name: 'AzureWebJobsStorage__accountName', value: hostStorageName }
          { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
          { name: 'AzureWebJobsStorage__clientId', value: identityClientId }
        ])
      }]
      // Native Functions generates trigger scalers. User scale rules require a
      // preview override and are deliberately omitted.
      scale: { minReplicas: minReplicas, maxReplicas: 1 }
    }
  }
}

output name string = app.name
