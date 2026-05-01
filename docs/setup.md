# Setup

End-to-end deploy of the demo, from a fresh AWS account to a healthy ECS
service with Slack notifications and the chaos trigger ready to use.

## Prerequisites

- An AWS account with admin access. Free Plan accounts work but FIS will
  be unavailable; the chaos-trigger Lambda is the recommended path
  regardless.
- AWS CLI v2 configured (default region `ap-southeast-2`).
- A GitHub account that owns this repository.
- A Slack workspace where you can install a custom app.
- Docker is **not** required locally; CodeBuild handles image builds.

## 1. CodeStar GitHub connection

In the AWS Console: **Developer Tools → Settings → Connections → Create
connection**. Provider GitHub. Authorise via browser. Copy the resulting
ARN, you need it on every deploy.

## 2. Slack app

In the Slack workspace:

1. https://api.slack.com/apps → **Create New App → From scratch**. Name
   it whatever you like (e.g. `aiops-investigator`).
2. **Incoming Webhooks** → toggle on → **Add New Webhook to Workspace**
   → pick the channel for incident alerts. Copy the webhook URL.
3. **Interactivity & Shortcuts** → toggle on → leave the Request URL
   blank for now (you'll fill this after the first deploy).
4. **Basic Information → App Credentials → Signing Secret** → **Show**
   → copy the value.

## 3. GitHub PAT

Required only if you want the **Open fix PR** option to work (manual
button or auto-PR). Skip this if you only want to demo the rollback
path.

1. GitHub → your avatar → **Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token**.
2. Repository access: only this repo.
3. Permissions: `Contents: Read and write`, `Pull requests: Read and
   write`, `Metadata: Read-only`.
4. Copy the token. Short expiration is fine for a demo.

## 4. SecureString secrets in SSM

Three SecureString parameters. CloudFormation does not manage these
because their values are tokens that should not appear in stack state.

```bash
aws ssm put-parameter \
  --name /chaos-demo/investigator/slack-webhook-url \
  --value "$SLACK_INCOMING_WEBHOOK_URL" \
  --type SecureString --region ap-southeast-2

aws ssm put-parameter \
  --name /chaos-demo/investigator/slack-signing-secret \
  --value "$SLACK_SIGNING_SECRET" \
  --type SecureString --region ap-southeast-2

aws ssm put-parameter \
  --name /chaos-demo/investigator/github-token \
  --value "$GITHUB_PAT" \
  --type SecureString --region ap-southeast-2
```

If you skipped step 3, omit the third command. The investigator falls
back to unauthenticated GitHub calls (still works for public repos,
rate-limited).

## 5. Deploy

```bash
GITHUB_OWNER=<your-github-username> \
GITHUB_REPO=AIOps-AWS \
GITHUB_BRANCH=main \
GITHUB_CONNECTION_ARN="arn:aws:codeconnections:ap-southeast-2:<account>:connection/<id>" \
bash scripts/deploy.sh
```

`deploy.sh` runs in two phases because the ECS service can't pull an
image that doesn't exist yet:

1. Stack created with `DesiredCount=0`. Pipeline created and triggered.
   CodeBuild pushes the first image to ECR.
2. Stack updated with `DesiredCount=2`. ECS rolls out two healthy tasks.

Total time ~10-12 minutes on first deploy, ~3-4 minutes on subsequent
updates.

## 6. Wire Slack interactivity

Grab the API Gateway URL from stack outputs:

```bash
aws cloudformation describe-stacks --stack-name chaos-cicd-demo \
  --region ap-southeast-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`InvestigatorActionsApiUrl`].OutputValue' \
  --output text
```

Paste it into your Slack app under **Interactivity & Shortcuts → Request
URL** and **Save Changes**. Slack will verify the URL on save.

## 7. Smoke test

```bash
ALB=$(aws cloudformation describe-stacks --stack-name chaos-cicd-demo \
  --region ap-southeast-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`ALBDnsName`].OutputValue' \
  --output text)
curl http://$ALB/health
```

Expect `{"status":"healthy",...}`.

The chaos-trigger console URL is also in stack outputs. Open it, save
the four test events from `docs/usage.md`, and you're ready to demo.

## Teardown

```bash
bash scripts/destroy.sh
```

Manual cleanup the script doesn't touch:

- The CFN templates S3 bucket (`chaos-cicd-demo-cfn-<account>-<region>`)
- The three SSM SecureString parameters under `/chaos-demo/investigator/`
- The DynamoDB table contents (TTL expires them in 90 days; or
  `delete-table`)
