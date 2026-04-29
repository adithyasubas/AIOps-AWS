# Architecture

## Components

### Application
A minimal Express.js API (`App/server.js`) with three endpoints:
- `GET /` — service identity
- `GET /health` — used by ALB target group + ECS task healthchecks
- `GET /info` — host/platform/Node version

Containerised with `node:18-alpine`, runs as the non-root `node` user, and
handles `SIGTERM` for graceful Fargate shutdowns.

### Build & deploy
- **CodePipeline V2** (`pipeline.yaml`) with three stages:
  - **Source** — CodeStar Connection to GitHub.
  - **Build** — CodeBuild with `PrivilegedMode: true` to run Docker. Pushes
    the image to ECR with both the commit SHA and `latest` tags, and writes
    `imagedefinitions.json` for the Deploy action.
  - **Deploy** — ECS standard deploy provider. **`OnFailure.Result: ROLLBACK`**
    triggers automatic rollback if the deploy fails (V1 does not support this —
    the template is explicit `PipelineType: V2`).
- **ECR** (`ecs.yaml`) with scan-on-push and a `keep-only-last-5` lifecycle
  rule.

### Runtime
- **ECS Fargate cluster** with Container Insights and FARGATE + FARGATE_SPOT
  capacity providers.
- **Service** with desired count 2, deployment circuit breaker (also rollback
  on stuck deploys), `MinHealthyPercent: 50`, `MaxPercent: 200`.
- **Public ALB** in the two public subnets, ECS tasks attached via IP target
  type. Tasks run with public IPs (NAT Gateway off by default for cost).

### Networking
- VPC `10.0.0.0/16` across `ap-southeast-2a` / `ap-southeast-2b`.
- 2 public subnets (10.0.1.0/24, 10.0.2.0/24), 2 private subnets (10.0.3.0/24,
  10.0.4.0/24).
- Conditional NAT Gateway (`EnableNatGateway` parameter, default `false`).
- VPC Flow Logs (REJECTs only, 7-day retention) for audit visibility.
- ALB SG: 80 from 0.0.0.0/0. ECS SG: 3000 from ALB SG only — no direct
  internet → task path on the app port.

### Observability
- 4 CloudWatch alarms in `monitoring.yaml`:
  - `UnHealthyHostCount` (TargetGroup) ≥ 1 for 60s
  - `HTTPCode_Target_5XX_Count` (LB) ≥ 10 for 60s
  - `CPUUtilization` (ECS service) ≥ 80% for 2 minutes
  - `RunningTaskCount` (Container Insights) < 2 for 60s
- All four feed the `chaos-demo-alarms` SNS topic.
- A CloudWatch dashboard summarises requests, 5xx rate, healthy hosts, ECS
  CPU/memory, and running vs desired tasks.

### Chaos
- AWS FIS (`chaos.yaml`) with two experiment templates:
  - **stop-task** — `aws:ecs:stop-task` on all tasks tagged `ChaosReady=true`.
  - **task-cpu-stress** — 90% CPU for 5 minutes.
- The FIS IAM role is least-privilege: `ecs:StopTask` is restricted by
  `aws:ResourceTag/ChaosReady = "true"`. Targets are selected by tag, not ARN —
  they survive task replacement during chaos runs.

### Webhook bridge
- `webhook-bridge.yaml` provisions a Python 3.12 Lambda (`lambda/index.py`)
  subscribed to the SNS topic.
- Reads the DevOps Agent webhook URL + HMAC secret from SSM Parameter Store
  (cached at module scope for warm-start performance).
- Builds the Generic Webhook payload, signs with HMAC-SHA256, POSTs with
  `X-Signature: sha256=<hex>` header.
- Errors are logged but not re-raised — SNS retries would only flood the agent.

## End-to-end data flow

1. **Code push** to `main` on `github.com/adithyasubas/AIOps-AWS`.
2. CodeStar Connection notifies CodePipeline → Source stage emits `SourceOutput`.
3. CodeBuild pulls source, runs `buildspec.yml`: ECR login, `docker build` from
   `App/`, push image, write `imagedefinitions.json`.
4. ECS Deploy action updates the service with the new task definition. Circuit
   breaker watches steady-state — if a fresh task fails healthcheck, deploy
   rolls back automatically (`OnFailure: ROLLBACK`).
5. **Chaos run**: `bash scripts/run-chaos.sh stop` calls
   `aws fis start-experiment`. FIS calls `StopTask` on each `ChaosReady=true`
   task.
6. **Detection**: ALB target group transitions to unhealthy; `UnHealthyHostCount`
   alarm breaches in <60s. `RunningTaskCount` alarm breaches in parallel.
7. **Notification**: SNS topic publishes; Lambda is invoked; reads SSM secrets;
   POSTs signed payload to DevOps Agent webhook in us-east-1.
8. **Investigation**: DevOps Agent observes the topology, correlates the alarm
   with recent deploys / config changes, and identifies the failure mode.
9. **Slack message** with three remediation options:
   - **A — Auto-rollback only.** ECS circuit breaker has already done this; no
     further action.
   - **B — Rollback + apply agent's fix.** Agent provides the recommended diff;
     human applies it manually.
   - **C — Rollback + delegate to Kiro.** Agent hands off the fix to Kiro,
     which opens an automated PR.

## Security decisions

- **No IAM wildcards** in `Action` for any role. `Resource: "*"` is only used
  where AWS requires it (e.g. `ecr:GetAuthorizationToken`, generic
  `ecs:Describe*` calls).
- **HMAC-SHA256** on the webhook bridge so DevOps Agent can verify the
  signature and reject spoofed alarms.
- **SecureString SSM** for the webhook secret. Lambda role has GetParameter
  scoped to those two parameter ARNs, nothing more.
- **VPC Flow Logs** capture rejected traffic for audit.
- **ECR image scan-on-push** surfaces CVEs before they reach Fargate.
- **ALB SG** is the only path to ECS port 3000.

## Cost-optimisation decisions

- **NAT Gateway off by default** — saves ~$35/month. Tasks run in public
  subnets with public IPs. Trade-off: surface area is larger; ECS SG still
  blocks inbound except from ALB SG.
- **FARGATE_SPOT** in the cluster's capacity providers (the demo uses
  FARGATE for steady tasks, but spot is available for ad hoc runs).
- **256 CPU / 512 MB** task — minimum Fargate sizing.
- **ECR `keep-last-5`** lifecycle.
- **CloudWatch log retention 7 days**.
- **S3 artifact bucket 30-day expiry**.

## The three remediation options (recap)

| Option | Action | Operator effort | Best when |
|---|---|---|---|
| A | Accept ECS auto-rollback | None — already done | Transient/rare failure, low confidence in agent's fix |
| B | Apply agent's recommended fix manually | Review + commit | High-confidence fix, human wants final say |
| C | Delegate to Kiro for an automated PR | Approve PR | Low-risk fixes, repeatable patterns |
