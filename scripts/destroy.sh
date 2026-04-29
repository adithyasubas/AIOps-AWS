#!/usr/bin/env bash
# Tear down the Chaos CICD Demo stack.
# Empties the artifact bucket and ECR repo first (CloudFormation can't delete non-empty).
# Usage: bash scripts/destroy.sh

set -euo pipefail

STACK_NAME="${STACK_NAME:-chaos-cicd-demo}"
REGION="${AWS_REGION:-ap-southeast-2}"
ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-chaos-demo}"

command -v aws >/dev/null 2>&1 || { echo "ERROR: aws CLI not found"; exit 1; }
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Destroying stack ${STACK_NAME} in account ${ACCOUNT_ID} (${REGION})"

# Empty the pipeline artifact bucket
ARTIFACT_BUCKET="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`ArtifactBucketName`].OutputValue' --output text 2>/dev/null || echo "")"
if [ -n "${ARTIFACT_BUCKET}" ] && [ "${ARTIFACT_BUCKET}" != "None" ]; then
  echo "Emptying artifact bucket: ${ARTIFACT_BUCKET}"
  aws s3 rm "s3://${ARTIFACT_BUCKET}" --recursive --region "${REGION}" || true
  # If versioning was enabled, also delete versions/markers
  aws s3api list-object-versions --bucket "${ARTIFACT_BUCKET}" --region "${REGION}" \
    --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null \
    | jq -r '.Objects // [] | .[] | "\(.Key)\t\(.VersionId)"' 2>/dev/null \
    | while IFS=$'\t' read -r KEY VID; do
        [ -n "${KEY}" ] && aws s3api delete-object --bucket "${ARTIFACT_BUCKET}" --region "${REGION}" --key "${KEY}" --version-id "${VID}" >/dev/null || true
      done || true
fi

# Delete all images from ECR repo (CloudFormation refuses to delete non-empty repos)
ECR_REPO_NAME="${ENVIRONMENT_NAME}-app"
if aws ecr describe-repositories --repository-names "${ECR_REPO_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  echo "Deleting images from ECR repo: ${ECR_REPO_NAME}"
  IMAGE_IDS="$(aws ecr list-images --repository-name "${ECR_REPO_NAME}" --region "${REGION}" --query 'imageIds[*]' --output json)"
  if [ "${IMAGE_IDS}" != "[]" ]; then
    aws ecr batch-delete-image --repository-name "${ECR_REPO_NAME}" --region "${REGION}" --image-ids "${IMAGE_IDS}" >/dev/null || true
  fi
fi

echo "Deleting CloudFormation stack: ${STACK_NAME}"
aws cloudformation delete-stack --stack-name "${STACK_NAME}" --region "${REGION}"
echo "Waiting for stack deletion (this can take 10-15 minutes)..."
aws cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}" --region "${REGION}"
echo "Stack deleted."

echo
echo "Manual cleanup still required:"
echo "  - DevOps Agent Space in us-east-1 (console)"
echo "  - Any orphaned FIS experiments"
echo "  - CFN templates bucket: chaos-cicd-demo-cfn-${ACCOUNT_ID}-${REGION} (delete if no longer needed)"
echo "  - SSM parameters /chaos-demo/devops-agent/* (managed by stack but verify)"
