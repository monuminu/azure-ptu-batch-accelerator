targetScope = 'resourceGroup'

param name string
param location string
param environmentName string
param environmentId string
param identityId string
param registryServer string
param image string
param env array

resource app 'Microsoft.App/containerApps@2026-01-01' = {
  name: name
  location: location
  tags: { 'azd-service-name': 'api', 'azd-env-name': environmentName }
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
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
    }
    template: {
      containers: [{
        name: 'api'
        image: image
        resources: { cpu: 1, memory: '2Gi' }
        env: env
        // This command serves a non-browsing placeholder at first provision and
        // launches uvicorn once azd replaces the image with Dockerfile.api.
        command: ['python', '-c']
        args: ['''
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

if os.path.isfile('/app/src/ptu_batch/api.py'):
    os.execvp('uvicorn', ['uvicorn', 'ptu_batch.api:app', '--host', '0.0.0.0', '--port', '8000'])

class PendingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == '/health' else 503)
        self.end_headers()
        self.wfile.write(b'deployment pending')

HTTPServer(('0.0.0.0', 8000), PendingHandler).serve_forever()
''']
      }]
      scale: { minReplicas: 0, maxReplicas: 2 }
    }
  }
}

output name string = app.name
output fqdn string = app.properties.configuration.ingress.fqdn
