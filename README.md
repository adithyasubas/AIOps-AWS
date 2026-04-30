# AIOps CI/CD demo

Self-healing pipeline for an ECS Fargate service. CodePipeline V2 builds and
deploys a containerised Node.js API behind an ALB. CloudWatch alarms feed an
"investigator" Lambda that calls Claude on Bedrock, looks up known incidents
in DynamoDB, and posts a Slack message with a diagnosis, a confidence score,
a risk level, and four buttons: trust auto-rollback, run the rollback now,
open a fix PR on GitHub, or delegate to Kiro. With auto-remediation enabled
the Lambda can also act on its own when confidence and risk are within
configured limits.

## Architecture

```
GitHub (main)
    │
    ▼
CodePipeline V2  ──► CodeBuild ──► ECR
    │                                 │
    ▼                                 │
ECS Fargate (ALB) ◄──────────────────┘
    │
    ▼
CloudWatch alarms ──► SNS ──► investigator Lambda
                                │
                                ├─► DynamoDB incident memory (PK: signature)
                                │     ├─ memory hit → reuse known fix, skip Bedrock
                                │     └─ memory miss → call Bedrock, save result
                                │
                                ├─► Bedrock (Claude Sonnet 4.6, us-east-1)
                                │
                                ├─► Slack (incoming webhook)
                                │     RCA + confidence + risk + 4 buttons
                                │
                                └─► auto-remediate gate
                                      ├─ rollback (CodePipeline RollbackStage)
                                      └─ create_pr (GitHub REST API)

Slack button click ──► API Gateway ──► actions Lambda
                                          ├─ verify HMAC signature
                                          ├─ option_a/b: ack / rollback
                                          ├─ option_pr: open GitHub PR
                                          └─ option_c: Kiro handoff
```

## Three layers of intelligence

1. **Memory** (DynamoDB, PAY_PER_REQUEST). Every investigation produces a
   stable signature from the alarm name, service, and a normalised version
   of the recent error logs. If we've successfully fixed this same shape of
   incident before, the Lambda skips the Bedrock call entirely and reuses
   the stored fix. Each investigation increments `success_count` or
   `failure_count` on the row, and items expire automatically via TTL.
2. **Reasoning** (Bedrock + Claude Sonnet 4.6). On a memory miss the Lambda
   gathers ECS state, recent container logs, the last five pipeline
   executions and the most-recent commit's diff (via the GitHub API), then
   asks Claude to produce a structured JSON decision: root cause,
   implicated files, a fix as exact-match find/replace patches,
   confidence (0–1), risk (LOW/MEDIUM/HIGH), and a recommended option.
3. **Action** (CodePipeline + GitHub REST API). Either the operator clicks
   a Slack button, or the auto-remediation gate fires automatically when
   confidence ≥ threshold and risk is within bounds. Both paths use the
   same `execute_rollback` and `create_fix_pr` helpers.

## Repo layout

```
App/                            Express API + Dockerfile + healthcheck
buildspec.yml                   CodeBuild build steps
cloudformation/
  main.yaml                     parent stack
  vpc.yaml                      VPC, subnets, optional NAT
  ecs.yaml                      ECR, ECS cluster, ALB, target group, service
  pipeline.yaml                 CodePipeline V2 + CodeBuild + IAM
  monitoring.yaml               4 CloudWatch alarms + SNS + dashboard
  investigator.yaml             investigator Lambda + DynamoDB memory + IAM
  investigator-interactions.yaml HTTP API + actions Lambda + IAM
  webhook-bridge.yaml           legacy DevOps Agent webhook (kept dormant)
  chaos.yaml                    optional FIS templates (off by default)
lambda/
  investigator/index.py         memory + Bedrock + auto-remediation + PR
  investigator-actions/index.py Slack button dispatch + PR creation
  index.py                      legacy webhook-bridge handler
scripts/
  deploy.sh                     two-phase deploy (count=0 then count=2)
  destroy.sh                    teardown
  run-chaos.sh                  triggers ecs:StopTask without FIS
```

## Deploy

### 1. CodeStar GitHub connection

`AWS Console → Developer Tools → Settings → Connections → Create connection`,
provider `GitHub`. Authorize via browser and copy the resulting ARN.

### 2. SecureString secrets

Three SSM SecureString parameters need to exist before deploy. None of them
are managed by CloudFormation — they hold tokens we don't want in template
state.

```bash
# Slack incoming webhook URL (https://hooks.slack.com/services/...)
aws ssm put-parameter \
  --name /chaos-demo/investigator/slack-webhook-url \
  --value "$SLACK_INCOMING_WEBHOOK_URL" \
  --type SecureString --region ap-southeast-2

# Slack app signing secret (from app settings -> Basic Information)
aws ssm put-parameter \
  --name /chaos-demo/investigator/slack-signing-secret \
  --value "$SLACK_SIGNING_SECRET" \
  --type SecureString --region ap-southeast-2

# GitHub PAT with repo write access (only needed for option_pr / auto-PR)
aws ssm put-parameter \
  --name /chaos-demo/investigator/github-token \
  --value "$GITHUB_PAT" \
  --type SecureString --region ap-southeast-2
```

### 3. Deploy

```bash
GITHUB_OWNER=adithyasubas \
GITHUB_REPO=AIOps-AWS \
GITHUB_BRANCH=main \
GITHUB_CONNECTION_ARN="arn:aws:codeconnections:ap-southeast-2:<acct>:connection/<id>" \
bash scripts/deploy.sh
```

`deploy.sh` runs in two phases because the ECS service can't pull an image
that doesn't exist yet:

1. Stack is created with `DesiredCount=0`. The pipeline is created and
   triggered. CodeBuild pushes the first image to ECR.
2. Stack is updated with `DesiredCount=2`. ECS rolls out two tasks.

Outputs to grab from the stack after deploy:

- `InvestigatorActionsApiUrl` — paste this into the Slack app under
  `Interactivity & Shortcuts → Request URL`.
- `IncidentMemoryTableName` — the DynamoDB table for the memory layer.

### 4. Slack app config

In the Slack app you used to create the incoming webhook:

1. `Interactivity & Shortcuts` → on → set Request URL to the value of
   `InvestigatorActionsApiUrl` from stack outputs → Save.
2. Copy the **Signing Secret** from `Basic Information → App Credentials`
   into the SSM parameter from step 2.

## Configuration

All knobs are CFN parameters with sensible defaults. The most useful:

| Parameter | Default | What it does |
|---|---|---|
| `AutoRemediateEnabled` | `false` | Master switch. When `false` the investigator only posts to Slack and never executes anything itself. |
| `AutoRemediateConfidenceThreshold` | `0.85` | Claude's reported `confidence` must be at least this to auto-execute. |
| `AutoRemediateAllowedActions` | `rollback,create_pr` | Which auto actions are permitted. Setting this to `rollback` only disables auto-PR. |
| `AutoRemediateRequireLowRisk` | `true` | When `true`, only `risk_level=LOW` auto-executes. Flip to `false` to also accept `MEDIUM`. |
| `IncidentMemoryEnabled` | `true` | Toggle the DynamoDB memory layer. |
| `IncidentMemoryTtlDays` | `90` | How long stored fixes live before TTL deletes them. |
| `MemoryMinSuccessCount` | `1` | A memory row must have at least this many successful applications before it's reused. |
| `GitHubTokenParamName` | `/chaos-demo/investigator/github-token` | SSM path to the GitHub PAT. |

## How auto-remediation gates a fix

Inside `lambda/investigator/index.py:auto_action_for`, all of these must be
true for a fix to fire automatically:

- `AUTO_REMEDIATE_ENABLED=true`
- `confidence >= AUTO_REMEDIATE_CONFIDENCE_THRESHOLD`
- `risk_level` is `LOW` (or `MEDIUM` when `AUTO_REMEDIATE_REQUIRE_LOW_RISK=false`); `HIGH` is never auto-eligible
- `auto_remediation_safe == true` from Claude
- `recommended_option` is in `AUTO_REMEDIATE_ALLOWED_ACTIONS`
- For `create_pr`, at least one patch with `find` + `replace` is present

Anything else and the Lambda just posts the Slack message and waits for a
human click. The Slack message includes a `:lock:` line explaining exactly
which gate failed, so you can tune thresholds based on what you see.

## How the GitHub PR creation is safe

The find/replace approach is intentionally narrower than applying a unified
diff. For each patch the actions Lambda:

- Rejects any path that begins with `/`, contains `..`, or contains `//`.
- Pulls the file from the base branch via the GitHub Contents API.
- Refuses if `find` is not an exact substring of the current file.
- Refuses if `replace == find` (no-op).
- Replaces the first occurrence only.
- Commits to a fresh `aiops-fix/<signature>-<ts>` branch via the Contents
  API (so we never push to `main`).
- Opens a PR back to `main` with the alarm name, pipeline execution id,
  failing commit SHA, confidence, and risk level in the body.

If any file fails its check, that patch is skipped. If no patches apply
cleanly the Slack message says so and the branch is left for inspection.

## How the memory layer saves cost

Each Bedrock call costs roughly $0.05 in tokens for this prompt size. The
memory layer is keyed on a normalized incident signature: alarm name,
ECS service, hashed error message after stripping timestamps and request
IDs, and failed pipeline stage. A repeat of the same incident — same
broken commit deployed twice, or a flapping deploy — hits memory and skips
Bedrock entirely.

DynamoDB is on `PAY_PER_REQUEST` with TTL enabled. Storage is bounded by
TTL (90 days default). Reads are single-key `GetItem`; we never scan.

## Cost

| Resource | Monthly |
|---|---|
| ECS Fargate (2 × 0.25 vCPU / 0.5 GB) | ~$10 |
| Application Load Balancer | ~$16 |
| CodePipeline V2 (per active pipeline) | ~$1 |
| CloudWatch logs + alarms | <$2 |
| ECR (5 images, lifecycle policy) | <$0.05 |
| S3 artifact bucket (30d expiry) | <$0.10 |
| DynamoDB incident memory | <$0.10 idle |
| API Gateway HTTP API (button clicks) | <$0.05 |
| Bedrock Claude Sonnet 4.6 | ~$0.05 / investigation |
| **Total idle** | **~$30 / month** |

## Demo: drive a real fix from a broken commit

```bash
# Branch and break
git checkout -b chaos-demo-break
# edit App/server.js to throw on startup, e.g.:
#   const k = process.env.DOES_NOT_EXIST; console.log(k.substring(0,4));
git commit -am "chaos: synthetic break"
git push -u origin chaos-demo-break

# Trigger the pipeline against that SHA without changing the watched branch
python3 - <<EOF
import boto3
sha = "<paste broken sha>"
boto3.client("codepipeline", region_name="ap-southeast-2").start_pipeline_execution(
    name="chaos-demo-pipeline",
    sourceRevisions=[{"actionName":"Source","revisionType":"COMMIT_ID","revisionValue":sha}],
)
EOF
```

The Deploy stage will fail when the new tasks crash on startup. CodePipeline's
`OnFailure: ROLLBACK` runs the previous good commit. While the alarm is
firing, the investigator runs, posts to Slack, and (if auto-remediation is
on) opens a PR with the suggested fix. Click the **PR** button to do it
manually instead.

After the demo, clean up:

```bash
git push origin --delete chaos-demo-break
git branch -D chaos-demo-break
```

## Cleanup

```bash
bash scripts/destroy.sh
```

Manual cleanup that the script doesn't touch: the CFN templates S3 bucket,
the SSM SecureString parameters, the DynamoDB table contents (TTL will
expire them but you can `delete-table` if you don't want to wait).

## Troubleshooting

- **Pipeline stuck at Source.** The CodeStar Connection is in `PENDING` —
  open it in the console and finish the OAuth handshake.
- **CodeBuild failing on `App/`.** Linux is case-sensitive; the directory
  is `App/`, not `app/`. Make sure `buildspec.yml` matches.
- **Investigator silent.** Check `/aws/lambda/chaos-demo-investigator` for
  Bedrock access errors — common ones are unsubscribed Anthropic models
  and wrong inference profile id. The default profile is
  `us.anthropic.claude-sonnet-4-6` which requires that profile to be
  available in your account.
- **Slack signature 401.** The signing secret in SSM doesn't match the
  Slack app's signing secret. Regenerate in the Slack app, overwrite the
  SSM SecureString, and try again.
- **Option PR returns "find text not present".** Claude proposed a patch
  whose `find` string didn't match the current file exactly. The Lambda
  refuses to guess. Either edit the patch manually or fall back to
  Option B (rollback).

## Security notes

- Three secrets live only in SSM SecureString: Slack webhook URL, Slack
  signing secret, GitHub PAT. None of them are in CloudFormation state.
- The actions Lambda verifies every Slack request via HMAC-SHA256 over
  `v0:<timestamp>:<raw body>` with a 5-minute replay window.
- IAM is scoped per-resource: the investigator role can only read its
  exact SSM parameters, write its DynamoDB table, and call CodePipeline
  on the demo pipeline.
- The PR creation path never pushes to `main`. It always creates a new
  branch and opens a PR for human review.
- Path traversal (`../`, leading `/`, double `//`) is blocked before any
  GitHub Contents call.

## What this is and isn't

It is a working end-to-end demo of an LLM-driven SRE loop: an alarm fires,
an agent reasons about it, and either a human or the agent itself can act
on the diagnosis. It demonstrates the full chain — CodePipeline, ECS,
Bedrock, DynamoDB, GitHub REST, Slack interactivity — with no third-party
dependencies and a single deploy script.

It is not production-grade. Don't connect it to a paging system or auto-
remediate against a real customer-facing service without first calibrating
the confidence threshold against your own incident history, locking down
who can press buttons, deduping SNS retries, and adding budget alarms on
the Bedrock spend.
