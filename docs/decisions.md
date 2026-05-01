# Architecture decisions

ADR-style notes on choices that aren't obvious from the code. Useful if
you're picking up the project, evaluating it for a job interview, or
just curious why something is the way it is.

## ADR-1: AWS DevOps Agent abandoned for a custom Bedrock investigator

**Context.** The original spec called for AWS DevOps Agent to receive
CloudWatch alarms via a webhook bridge and post investigation summaries
to Slack with three remediation options.

**What happened.** On a Free Plan account, FIS and DevOps Agent are
gated entirely - the console shows an "Upgrade plan" wall. After
upgrading, FIS unblocked but DevOps Agent stayed silent on every
trigger we tried (alarms via SNS, direct `@` mentions in Slack, both
). CloudTrail showed the agent's IAM role was never assumed, suggesting
the issue was upstream of our code.

**Decision.** Replace the agent with a custom investigator Lambda that
calls Claude Sonnet 4.6 directly via Bedrock's Converse API. We control
the prompt, the structured output schema, the latency, and the cost.

**Consequences.**
- Loss: zero managed-service uptime guarantees. We own the prompt and
  any drift in Claude's response shape.
- Win: full control. The Bedrock call costs ~$0.05 and takes ~15s,
  comparable to a managed agent. We added confidence scoring, risk
  rating, and a memory layer that the managed product can't.

## ADR-2: AWS FIS replaced with `aws ecs stop-task` for default chaos

**Context.** FIS is the AWS-recommended way to inject failures.

**What happened.** FIS is gated on Free Plan. It also adds a per-action
cost (~$0.10/minute), an IAM role with broad permissions, and a CFN
template that can't deploy without an account-level opt-in.

**Decision.** Default chaos in `scripts/run-chaos.sh` and the
chaos-trigger Lambda is `aws ecs stop-task`. FIS templates remain
available behind `EnableChaos=true` for accounts that want the richer
experiments.

**Consequences.** The demo works on a brand-new AWS account with no
opt-ins. The "kill all tasks then watch ECS replace them" path is
realistic enough to drive real alarms.

## ADR-3: Two-phase deploy in `scripts/deploy.sh`

**Context.** On a fresh deploy the ECS service is created with
`Image: <ECR>:latest`, but the ECR repository is created empty by the
same stack. ECS can't pull `:latest`, the deployment circuit breaker
trips, the stack rolls back.

**Decision.** Deploy in two phases. Phase 1 creates everything with
`DesiredCount=0` so the service exists but doesn't try to pull. The
pipeline then builds and pushes the first image. Phase 2 updates the
stack with `DesiredCount=2`, which now succeeds because ECR has the
image.

**Consequences.** First deploy takes ~10-12 minutes instead of ~5.
Subsequent deploys can pass `DesiredCount=2` directly and skip the
scale-down/up cycle. `scripts/deploy.sh` always does both phases for
simplicity; the README documents how to bypass for incremental updates.

## ADR-4: PR creation uses find/replace, not unified diff

**Context.** The investigator wants to commit a code fix in a new
branch and open a PR.

**Decision.** Each patch is a single-file `find` + `replace` pair.
Apply only if `find` is an exact substring of the current file
contents.

**Why not unified diffs.** Diffs are hard to apply against a moving
target. If the file changed since Claude saw it, the diff fails in
ambiguous ways. A unified-diff applier needs hunk fuzzing, line-number
tolerance, and conflict resolution - all of which add risk to a path
that should fail safely. Find/replace is narrower but predictable: it
applies cleanly or it refuses.

**Consequences.** Claude's prompt is updated to demand exact-match
strings. If the broken code has cosmetic drift since Claude read it,
the PR creation fails with a clear error and the operator falls back to
a rollback. We accept this loss in exchange for never landing a
half-applied patch.

## ADR-5: DynamoDB memory keyed on a normalised signature

**Context.** Same alarms recur. Calling Bedrock every time is expensive
and slow.

**Decision.** Build an `incident_signature` from
`(alarm_name, service, normalised_error_logs, failed_pipeline_stage)`.
Look it up in DDB. On hit, reuse the stored decision and skip Bedrock.

**Why not just hash the alarm name.** Two different bugs can fire the
same alarm. The error log content is what differentiates them. The
normalisation step strips timestamps, request IDs, hex tokens, and
high-precision numbers so cosmetic variation between identical bugs
doesn't produce different signatures.

**Why PAY_PER_REQUEST.** Read volume is one GetItem per alarm; storage
is bounded by TTL (90 days). On-demand pricing wins below ~10
requests/sec sustained, which is well above this workload.

**Consequences.** A typo in the prompt that makes Claude produce a bad
fix will be cached and reused. Mitigations: `MEMORY_MIN_SUCCESS_COUNT`
gates reuse on at least one auto-remediation success, and TTL eventually
flushes stale entries. For demo purposes the gate is set to 0 so memory
hits work on first repeat.

## ADR-6: `App/` capitalisation

**Context.** Conventional Node projects use `app/` or `src/`.

**Decision.** The folder is `App/` because that's how the user
originally created it. Renaming would break `buildspec.yml` (`docker
build -t $ECR_URI:$IMAGE_TAG App/`) and the `Dockerfile`'s `WORKDIR`,
and would require an ECR push that retags every image. Linux is
case-sensitive, and we've already fixed one case-related bug from this
in `buildspec.yml`. Not worth re-introducing the risk for cosmetics.

## ADR-7: Bedrock client targets `us-east-1` from a `ap-southeast-2` Lambda

**Context.** The Lambda runs in `ap-southeast-2`. Anthropic models on
Bedrock are subscribed per-region, and the user's playground first-use
acceptance was in `us-east-1`.

**Decision.** Hardcode `BEDROCK_REGION=us-east-1` for the runtime
client. The Lambda's network egress goes through the public AWS API
endpoint; cross-region call latency is ~50-100ms, well within budget
on a 15-second investigation.

**Alternative considered.** Subscribing in `ap-southeast-2` separately
and calling locally. Adds another opt-in step and another model id to
manage; not worth the saved 100ms.

## ADR-8: Slack signature verification, not OAuth

**Context.** API Gateway endpoint receives Slack interactivity payloads
publicly. Anyone can POST to it.

**Decision.** Verify Slack's HMAC-SHA256 signature on every request,
with a 5-minute replay window. The signing secret lives in SSM
SecureString and is read once per cold start and cached.

**Why not OAuth-protected user identity.** Slack already authenticates
the user inside its app (you have to be in the channel to click the
button). The HMAC layer authenticates that the request originated from
*your* Slack workspace, which is the only thing the Lambda actually
needs to know.

## ADR-9: Auto-remediation off by default

**Context.** The system can auto-rollback or auto-PR.

**Decision.** `AutoRemediateEnabled` defaults to `false`. The
investigator only posts to Slack until you've watched its decisions
match reality on a few real incidents and decided you trust the
calibration.

**Why.** An LLM-driven rollback that fires wrongly is worse than a
manual rollback that fires correctly. The cost of the wait is small
(60-90s human reaction time); the cost of a wrong auto action can be
unbounded. Default-off is the safe stance for a portfolio demo.
