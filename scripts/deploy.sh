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

echo "Deploying stack: ${STACK_NAME}"
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
    EnableNatGateway="${ENABLE_NAT}"

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
echo "  1. Push the application code to GitHub (${GITHUB_OWNER}/${GITHUB_REPO}, branch ${GITHUB_BRANCH}) to trigger the first pipeline run."
echo "  2. Once the pipeline turns green, complete the DevOps Agent setup in scripts/setup-devops-agent.md."
echo "  3. Run scripts/run-chaos.sh to demo the failure flow."
