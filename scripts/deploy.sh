#!/usr/bin/env bash
# Deploy the Chaos CICD Demo stack to AWS.
# Usage:
#   GITHUB_OWNER=your-username \
#   GITHUB_REPO=AIOps-AWS \
#   GITHUB_BRANCH=main \
#   GITHUB_CONNECTION_ARN=arn:aws:codeconnections:... \
#   bash scripts/deploy.sh

set -euo pipefail

STACK_NAME="${STACK_NAME:-chaos-cicd-demo}"
REGION="${AWS_REGION:-ap-southeast-2}"
ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-chaos-demo}"
ENABLE_NAT="${ENABLE_NAT_GATEWAY:-false}"
ENABLE_CHAOS="${ENABLE_CHAOS:-false}"

: "${GITHUB_OWNER:?GITHUB_OWNER is required (e.g. adithyasubas)}"
GITHUB_REPO="${GITHUB_REPO:-AIOps-AWS}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
: "${GITHUB_CONNECTION_ARN:?GITHUB_CONNECTION_ARN is required (create one in console first)}"

# Resolve repo root (the directory containing this script's parent)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

command -v aws >/dev/null 2>&1 || { echo "ERROR: aws CLI not found"; exit 1; }
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Deploying to account ${ACCOUNT_ID} in ${REGION}"

CFN_BUCKET="chaos-cicd-demo-cfn-${ACCOUNT_ID}-${REGION}"

if ! aws s3api head-bucket --bucket "${CFN_BUCKET}" --region "${REGION}" 2>/dev/null; then
  echo "Creating CFN templates bucket: ${CFN_BUCKET}"
  if [ "${REGION}" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "${CFN_BUCKET}" --region "${REGION}"
  else
    aws s3api create-bucket --bucket "${CFN_BUCKET}" --region "${REGION}" \
      --create-bucket-configuration LocationConstraint="${REGION}"
  fi
  aws s3api put-bucket-encryption --bucket "${CFN_BUCKET}" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  aws s3api put-public-access-block --bucket "${CFN_BUCKET}" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

echo "Packaging templates -> ${CFN_BUCKET}"
aws cloudformation package \
  --template-file cloudformation/main.yaml \
  --s3-bucket "${CFN_BUCKET}" \
  --s3-prefix "${STACK_NAME}" \
  --output-template-file packaged.yaml \
  --region "${REGION}"

deploy_with_count() {
  local count="$1"
  echo "==> Deploying stack with DesiredCount=${count}"
  aws cloudformation deploy \
    --template-file packaged.yaml \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --no-fail-on-empty-changeset \
    --parameter-overrides \
      EnvironmentName="${ENVIRONMENT_NAME}" \
      GitHubOwner="${GITHUB_OWNER}" \
      GitHubRepo="${GITHUB_REPO}" \
      GitHubBranch="${GITHUB_BRANCH}" \
      GitHubConnectionArn="${GITHUB_CONNECTION_ARN}" \
      EnableNatGateway="${ENABLE_NAT}" \
      EnableChaos="${ENABLE_CHAOS}" \
      DesiredCount="${count}"
}

# Phase 1: create the stack with DesiredCount=0 so the empty ECR doesn't trip the
# deployment circuit breaker on initial create.
deploy_with_count 0

PIPELINE_NAME="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`PipelineName`].OutputValue' --output text)"
echo "Pipeline name: ${PIPELINE_NAME}"

# Wait for the pipeline to push the first image to ECR. CodeStar connections
# auto-trigger on stack creation, but if no execution exists yet, kick one off.
sleep 10
EXEC_COUNT=$(aws codepipeline list-pipeline-executions --pipeline-name "${PIPELINE_NAME}" --region "${REGION}" --query 'length(pipelineExecutionSummaries)' --output text 2>/dev/null || echo 0)
if [ "${EXEC_COUNT}" = "0" ] || [ -z "${EXEC_COUNT}" ]; then
  echo "No pipeline execution found, starting one manually..."
  aws codepipeline start-pipeline-execution --name "${PIPELINE_NAME}" --region "${REGION}" >/dev/null
fi

echo "Waiting for first pipeline execution to succeed (this builds and pushes the Docker image)..."
ECR_NAME="${ENVIRONMENT_NAME}-app"
DEADLINE=$(( $(date +%s) + 1500 ))   # 25 min budget
while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
  IMAGE_COUNT=$(aws ecr list-images --repository-name "${ECR_NAME}" --region "${REGION}" --query 'length(imageIds[?imageTag==`latest`])' --output text 2>/dev/null || echo 0)
  PIPE_STATUS=$(aws codepipeline list-pipeline-executions --pipeline-name "${PIPELINE_NAME}" --region "${REGION}" --max-items 1 --query 'pipelineExecutionSummaries[0].status' --output text 2>/dev/null || echo "Unknown")
  echo "  pipeline=${PIPE_STATUS}  ecr_latest_count=${IMAGE_COUNT}"
  if [ "${IMAGE_COUNT}" -ge 1 ]; then
    echo "ECR has the :latest tag — image is ready."
    break
  fi
  if [ "${PIPE_STATUS}" = "Failed" ] || [ "${PIPE_STATUS}" = "Stopped" ]; then
    echo "ERROR: pipeline ${PIPE_STATUS}. Check the AWS console: https://${REGION}.console.aws.amazon.com/codesuite/codepipeline/pipelines/${PIPELINE_NAME}/view?region=${REGION}"
    exit 1
  fi
  sleep 30
done

# Phase 2: scale up to 2 tasks now that the image exists.
deploy_with_count 2

echo
echo "Stack deployed. Outputs:"
aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' --output table

ALB_DNS="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`ALBDnsName`].OutputValue' --output text)"
echo
echo "ALB: http://${ALB_DNS}/"
echo "Health: http://${ALB_DNS}/health"
echo
echo "Next steps:"
echo "  1. Complete the DevOps Agent setup in scripts/setup-devops-agent.md (us-east-1 console)."
echo "  2. Run scripts/run-chaos.sh stop to demo the failure flow."
