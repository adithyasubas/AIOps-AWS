#!/usr/bin/env bash
# Start the FIS "stop tasks" chaos experiment.
# Usage: bash scripts/run-chaos.sh [stop|cpu]   (default: stop)

set -euo pipefail

STACK_NAME="${STACK_NAME:-chaos-cicd-demo}"
REGION="${AWS_REGION:-ap-southeast-2}"
EXPERIMENT="${1:-stop}"

case "${EXPERIMENT}" in
  stop) OUTPUT_KEY="StopTasksExperimentId" ;;
  cpu)  OUTPUT_KEY="CpuStressExperimentId" ;;
  *)    echo "Unknown experiment '${EXPERIMENT}'. Use: stop | cpu"; exit 1 ;;
esac

TEMPLATE_ID="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey==\`${OUTPUT_KEY}\`].OutputValue" --output text)"

if [ -z "${TEMPLATE_ID}" ] || [ "${TEMPLATE_ID}" = "None" ]; then
  echo "ERROR: could not find experiment template (${OUTPUT_KEY}) in stack ${STACK_NAME}"
  exit 1
fi

echo "Starting FIS experiment (${EXPERIMENT}) against template ${TEMPLATE_ID}"
EXPERIMENT_ID="$(aws fis start-experiment \
  --experiment-template-id "${TEMPLATE_ID}" \
  --region "${REGION}" \
  --query 'experiment.id' --output text)"

echo "Experiment ID: ${EXPERIMENT_ID}"
echo
echo "Monitor:"
echo "  - CloudWatch alarms: ${REGION} console > CloudWatch > Alarms"
echo "  - DevOps Agent in us-east-1 will receive the alarm via the webhook bridge."
echo "  - ECS service should self-heal as Fargate replaces stopped tasks."
echo
echo "Status:"
echo "  aws fis get-experiment --id ${EXPERIMENT_ID} --region ${REGION}"
