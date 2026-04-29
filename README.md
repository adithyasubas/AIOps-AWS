# Chaos CICD Demo — Self-healing Pipeline with AWS DevOps Agent

A reference deployment demonstrating a self-healing CI/CD pipeline on AWS:
CodePipeline V2 builds and deploys a containerised Node.js API to ECS Fargate.
AWS Fault Injection Service simulates application failures, CloudWatch alarms
trigger an autonomous investigation by AWS DevOps Agent, and a Slack message
presents three remediation options to the human.

## Architecture

```
   GitHub                  CodePipeline V2                       ECR
   ──────                  ───────────────                       ───
   adithyasubas/      Source → Build → Deploy ──────► chaos-demo-app
   AIOps-AWS         (CodeStar)(Build)(ECS rollback           │
                                       on failure)            │ pull
                                                              ▼
                                                       ┌──────────────┐
                                                       │ ECS Fargate  │
                                                       │ chaos-demo   │   ALB
                                                       │ desired = 2  │ ◄────  internet
                                                       └──────┬───────┘  (public ALB)
                                                              │
                            FIS (chaos)                       │ targets
                            ───────────                       │ tagged
                            stop-task ─► tasks ───────────────┘ ChaosReady=true
                            cpu-stress

                                                              │
                                              CloudWatch alarms (4)
                                                              │
                                                             SNS
                                                              │
                                                             ▼
                                                  webhook-bridge Lambda
                                                  (HMAC-signs, POSTs)
                                                              │
                                                              ▼
                                       AWS DevOps Agent (us-east-1)
                                                              │
                                                              ▼
                                                  Slack: 3 remediation options
                                                  (A) auto-rollback only
                                                  (B) rollback + fix
                                                  (C) rollback + delegate to Kiro
```

## Prerequisites

- AWS account with admin access
- AWS CLI v2 configured (default region `ap-southeast-2`)
- Docker (only needed if you want to build images locally — CodeBuild handles it in the pipeline)
- A GitHub account that owns this repo
- A Slack workspace and channel (for DevOps Agent notifications)

## Deploy

### 1. Create a GitHub CodeStar Connection (manual, browser OAuth)

```
AWS Console → Developer Tools → Settings → Connections → Create connection
  Provider: GitHub
  Name: aiops-github
```

Authorize via browser. Copy the resulting Connection ARN — you'll need it next.

### 2. Run the deploy script

```bash
GITHUB_OWNER=adithyasubas \
GITHUB_REPO=AIOps-AWS \
GITHUB_BRANCH=main \
GITHUB_CONNECTION_ARN=arn:aws:codeconnections:ap-southeast-2:235864149303:connection/<id> \
bash scripts/deploy.sh
```

The script will:
- Create a CFN templates S3 bucket if needed.
- Run `aws cloudformation package` and `deploy`.
- Print the ALB DNS and stack outputs.

### 3. Push code to GitHub to trigger the pipeline

The pipeline's Source stage watches `main`. The first deploy waits for any
push (the ECR repo is created empty, so the ECS service will sit at
`PENDING` until the first image lands).

```bash
git push origin main
```

CodePipeline runs Source → Build → Deploy. ECS rolls out 2 tasks.

### 4. Set up DevOps Agent (manual)

Follow `scripts/setup-devops-agent.md`. Roughly:

1. Create the Agent Space in **us-east-1**.
2. Connect GitHub + Slack.
3. Generate a webhook (HMAC).
4. Run two `aws ssm put-parameter` commands to store the URL and secret.

### 5. Run a chaos experiment

```bash
bash scripts/run-chaos.sh stop    # stop all ECS tasks
bash scripts/run-chaos.sh cpu     # 90% CPU stress for 5 min
```

Watch the `UnhealthyHostCount` and `RunningTaskCount` alarms breach, the
Lambda fire, and DevOps Agent post a Slack message.

## Cost estimate (per month, idle demo)

| Resource | Cost |
|---|---|
| ECS Fargate (2 × 0.25vCPU / 0.5GB, 24/7) | ~$10 |
| Application Load Balancer | ~$16 |
| CloudWatch alarms + logs (7d retention) | <$2 |
| ECR (5 images, ~200MB) | <$0.05 |
| S3 artifact bucket (30d expiry) | <$0.10 |
| NAT Gateway (off by default) | ~$35 if enabled |
| CodePipeline V2 (per active pipeline) | ~$1 |
| **Total** (NAT off) | **~$30/month** |

## Well-Architected alignment

- **Operational excellence:** every resource tagged, CloudWatch dashboard,
  Container Insights, deployment circuit breaker with rollback.
- **Security:** least-priv IAM throughout, ECR image scan-on-push, AES256
  encryption (S3 + ECR), VPC Flow Logs (REJECTs), security groups scoped to
  source SG (not 0.0.0.0/0) for app traffic, SSM `SecureString` for webhook
  secrets.
- **Reliability:** Multi-AZ subnets, ECS desired count 2, ALB health checks,
  pipeline `OnFailure: ROLLBACK`, ECS deployment circuit breaker.
- **Performance efficiency:** Fargate auto-managed capacity, Container Insights
  metrics, ALB request distribution.
- **Cost optimisation:** NAT off by default, FARGATE + FARGATE_SPOT capacity
  providers, 7-day log retention, ECR keep-last-5 lifecycle, 30-day artifact
  bucket expiry.
- **Sustainability:** minimum sized tasks (256 CPU / 512 MB), log retention
  capped, single NAT gateway when enabled.

## Cleanup

```bash
bash scripts/destroy.sh
```

The script empties the artifact bucket and ECR before deleting the stack.
Manual cleanup still required:

- DevOps Agent Space (us-east-1 console)
- CFN templates S3 bucket if you want to remove it
- Any orphaned FIS experiment runs

## Troubleshooting

- **Pipeline stuck on Source:** the CodeStar Connection is in `PENDING`.
  Open it in the console and click "Update pending connection" to finish OAuth.
- **CodeBuild fails on `App/` directory:** confirm `buildspec.yml` references
  `App/` (capital A) — Linux is case-sensitive.
- **ECS tasks fail healthcheck:** check `/aws/ecs/chaos-demo-app` log group,
  confirm the container is listening on port 3000.
- **DevOps Agent doesn't post to Slack:** check Lambda logs, verify the SSM
  parameters were overwritten with the real webhook URL/secret.
- **`CREATE_FAILED` on AlarmTopic / Subscription:** confirm the SNS topic ARN
  is being passed correctly through nested stack outputs.
