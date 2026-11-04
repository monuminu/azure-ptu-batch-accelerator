#!/bin/sh
# azd hooks: check a durable pause and drain, then stop old inference consumers
# before an inference revision can be created. Initial placeholders cannot work.
set -eu

mode=${1:-}
case "$mode" in
  --provision|--predeploy|--inference-deploy) ;;
  *) echo "Guard requires --provision, --predeploy, or --inference-deploy."; exit 2 ;;
esac

if [ -z "${AZURE_ENV_NAME:-}" ]; then
  echo "Deployment blocked: AZURE_ENV_NAME is missing."
  exit 1
fi
if [ -z "${AZURE_SUBSCRIPTION_ID:-}" ]; then
  echo "Deployment blocked: AZURE_SUBSCRIPTION_ID is missing."
  exit 1
fi
active_subscription=$(az account show --query id --output tsv --only-show-errors | tr '[:upper:]' '[:lower:]')
expected_subscription=$(printf '%s' "$AZURE_SUBSCRIPTION_ID" | tr '[:upper:]' '[:lower:]')
if [ "$active_subscription" != "$expected_subscription" ]; then
  echo "Deployment blocked: Azure CLI subscription differs from azd environment."
  exit 1
fi
AZURE_RESOURCE_GROUP=${AZURE_RESOURCE_GROUP:-rg-${AZURE_ENV_NAME}-ptu-batch}
API_NAME=${API_NAME:-ca-${AZURE_ENV_NAME}-ptu-api}
CONTROL_NAME=${CONTROL_NAME:-ca-${AZURE_ENV_NAME}-ptu-control}
INFERENCE_NAME=${INFERENCE_NAME:-ca-${AZURE_ENV_NAME}-ptu-inference}

error_file=$(mktemp)
trap 'rm -f "$error_file"' EXIT HUP INT TERM

read_image() {
  if image=$(az containerapp show --name "$1" --resource-group "$AZURE_RESOURCE_GROUP" --query 'properties.template.containers[0].image' --output tsv --only-show-errors 2>"$error_file"); then
    printf '%s\n' "$image"
    return
  fi
  case "$(cat "$error_file")" in
    *ResourceNotFound*|*ResourceGroupNotFound*) printf '%s\n' '__MISSING__' ;;
    *) echo "Deployment blocked: cannot read container app $1." >&2; exit 1 ;;
  esac
}

api_image=$(read_image "$API_NAME")
control_image=$(read_image "$CONTROL_NAME")
inference_image=$(read_image "$INFERENCE_NAME")

if [ "$api_image" = '__MISSING__' ] && [ "$control_image" = '__MISSING__' ] && [ "$inference_image" = '__MISSING__' ]; then
  if [ "$mode" = '--provision' ]; then
    echo "First provision: no accelerator apps exist."
    exit 0
  fi
  echo "Deployment blocked: provision the accelerator before deploying code."
  exit 1
fi

# During bootstrap the public API is a 503-only placeholder and control has no
# code. Azure's durable admission state starts paused. Deploy inference, then
# control, then API. Any real API or control image requires the durable check.
if [ "$api_image" = 'python:3.12-slim' ] && [ "$control_image" = 'mcr.microsoft.com/azure-functions/python:4-python3.12' ]; then
  echo "Bootstrap: API and control are placeholders; no batch admission is possible."
  exit 0
fi

if [ "$control_image" = '__MISSING__' ] || [ "$control_image" = 'mcr.microsoft.com/azure-functions/python:4-python3.12' ]; then
  echo "Deployment blocked: control runtime is unavailable for durable maintenance check."
  exit 1
fi

check_output=$(python3 scripts/check_control_maintenance.py --name "$CONTROL_NAME" --resource-group "$AZURE_RESOURCE_GROUP" 2>&1) || {
  echo "Deployment blocked: control maintenance check failed."
  exit 1
}
case "$check_output" in
  *PTU_BATCH_DRAIN_SAFE*) echo "Durable maintenance gate confirms paused admission and settled work." ;;
  *) echo "Deployment blocked: control did not return PTU_BATCH_DRAIN_SAFE."; exit 1 ;;
esac

if [ -z "${SERVICEBUS_NAMESPACE:-}" ]; then
  echo "Deployment blocked: SERVICEBUS_NAMESPACE provision output is missing."
  exit 1
fi

read_count() {
  az servicebus queue show --resource-group "$AZURE_RESOURCE_GROUP" --namespace-name "$SERVICEBUS_NAMESPACE" --name "$1" --query "countDetails.$2" --output tsv --only-show-errors
}

# Repeat samples to catch a late lock release or a scheduled retry becoming active.
echo "Waiting for both queues and all old inference replicas to drain (up to 30 minutes)."
attempt=0
zero_samples=0
while [ "$attempt" -lt 60 ]; do
  attempt=$((attempt + 1))
  inference_active=$(read_count batch-inference activeMessageCount)
  inference_scheduled=$(read_count batch-inference scheduledMessageCount)
  control_active=$(read_count batch-control activeMessageCount)
  control_scheduled=$(read_count batch-control scheduledMessageCount)
  for count in "$inference_active" "$inference_scheduled" "$control_active" "$control_scheduled"; do
    case "$count" in
      ''|*[!0-9]*) echo "Deployment blocked: queue count unavailable."; exit 1 ;;
    esac
  done

  replicas=0
  revisions=$(az containerapp revision list --resource-group "$AZURE_RESOURCE_GROUP" --name "$INFERENCE_NAME" --query '[?properties.active].name' --output tsv --only-show-errors)
  for revision in $revisions; do
    count=$(az containerapp replica list --resource-group "$AZURE_RESOURCE_GROUP" --name "$INFERENCE_NAME" --revision "$revision" --query 'length(@)' --output tsv --only-show-errors)
    case "$count" in
      ''|*[!0-9]*) echo "Deployment blocked: replica count unavailable."; exit 1 ;;
    esac
    replicas=$((replicas + count))
  done

  echo "Drain: inference active=$inference_active scheduled=$inference_scheduled; control active=$control_active scheduled=$control_scheduled; old inference replicas=$replicas"
  if [ "$inference_active" -eq 0 ] && [ "$inference_scheduled" -eq 0 ] && [ "$control_active" -eq 0 ] && [ "$control_scheduled" -eq 0 ] && [ "$replicas" -eq 0 ]; then
    zero_samples=$((zero_samples + 1))
    if [ "$zero_samples" -ge 2 ]; then
      break
    fi
  else
    zero_samples=0
  fi
  sleep 30
done

if [ "$zero_samples" -lt 2 ]; then
  echo "Deployment blocked: drain timed out."
  exit 1
fi

if [ "$mode" = '--provision' ] || [ "$mode" = '--inference-deploy' ]; then
  for revision in $revisions; do
    echo "Deactivating drained inference revision $revision; record this name for rollback."
    az containerapp revision deactivate --resource-group "$AZURE_RESOURCE_GROUP" --name "$INFERENCE_NAME" --revision "$revision" --only-show-errors --output none
  done
  still_active=$(az containerapp revision list --resource-group "$AZURE_RESOURCE_GROUP" --name "$INFERENCE_NAME" --query '[?properties.active].name' --output tsv --only-show-errors)
  if [ -n "$still_active" ]; then
    echo "Deployment blocked: an inference revision remains active."
    exit 1
  fi
fi

echo "Deployment gate passed. Keep the durable admission pause until deployment and health checks finish."
