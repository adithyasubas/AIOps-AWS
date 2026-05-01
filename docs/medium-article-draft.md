# Medium article draft

A working outline plus draft prose. Edit the prose to your voice
before publishing. Length target: 1800-2400 words, ~10 minute read.

## Working titles (pick one)

1. **I built an AI SRE on AWS for $30/month - here's what it actually does**
2. **From Slack alert to merged PR: building a self-healing pipeline with Bedrock and CodePipeline**
3. **What happens when you let an LLM look at your CloudWatch alarms**
4. **Replacing AWS DevOps Agent with my own Bedrock investigator (and what I learned)**

Recommendation: title #2 has the strongest scope-of-work claim and the
clearest reader payoff. Title #4 is the strongest if the publication
audience skews "AWS engineers."

## Hook (first 3 paragraphs)

> A deploy fails. CloudWatch fires an alarm. Within 30 seconds, a Slack
> message appears: a structured RCA, the implicated file, a confidence
> score, a risk level, and four buttons - trust auto-rollback, run the
> rollback now, open a fix PR, or hand it to a teammate. Click the PR
> button. A new branch appears in GitHub. A pull request opens with
> the patch, the alarm name, and the failing commit SHA. You review it
> and merge.
>
> No human read a log file. No one paged the on-call. The system did
> the diagnosis itself, in production, on real data.
>
> This is what I built over a few weeks on AWS. It cost about $30 a
> month to run. The investigator is a Lambda function with a 200-line
> prompt for Claude. The "agent" is just three layers stitched together
> with one trade-off you have to get right.

## Sections

### Why I built it

- I wanted to ship a real AIOps loop that wasn't a tutorial, wasn't a
  toy, and didn't depend on a managed black box.
- I'd seen "self-healing pipeline" demos that were just `OnFailure:
  ROLLBACK` plus a YAML diagram. I wanted the LLM to actually read the
  failure context and propose a fix - not just rollback blindly.
- It also turned into a real test of how far you can push a single
  Lambda + Bedrock + a 50-line memory layer before you start needing
  something heavier.

### What it does

A short ladder of capability:

1. **Detect.** Standard CloudWatch alarms watch the ECS service.
2. **Investigate.** A Lambda gathers ECS state, container logs,
   pipeline history, and the most-recent commit's diff via the GitHub
   REST API.
3. **Reason.** It sends all of that to Claude Sonnet 4.6 with a
   structured prompt that demands JSON back: root cause, implicated
   files, a fix as exact-match find/replace patches, three options for
   the human, a confidence score, and a risk level.
4. **Decide.** A gate checks confidence + risk + an explicit
   `auto_remediation_safe` flag. If the gate passes and the action is
   in the allow-list, the Lambda executes it itself.
5. **Act.** Rollback runs `codepipeline:RollbackStage` with a
   commit-SHA-pinned `start-pipeline-execution` fallback. PR creation
   uses the GitHub Contents API: branch, commit, PR.
6. **Remember.** Every investigation produces a stable signature that
   gets stored in DynamoDB. A repeat of the same incident skips the
   Bedrock call entirely.

### The architecture

Insert the Mermaid diagram from `docs/architecture.md` here, or
re-render as an image.

A short narrative:

> I tried to keep the moving parts as small as possible. CodePipeline
> V2 handles deploys. Two Lambdas do all the LLM work and all the
> Slack work. One DynamoDB table provides memory. One API Gateway
> endpoint receives button clicks. That's it.

Highlight the "three layers of intelligence" framing:

- **Memory** (DynamoDB) skips the LLM when an incident repeats.
- **Reasoning** (Bedrock + Claude) handles novel incidents.
- **Action** (CodePipeline + GitHub) executes either way, with the same
  code path whether a human clicked the button or the gate fired
  automatically.

### The prompt is the product

A few hundred words on the structured output schema. Show a trimmed
version:

```json
{
  "root_cause": "...",
  "implicated_files": ["..."],
  "patches": [{"file_path": "...", "find": "...", "replace": "..."}],
  "options": [...],
  "recommended_option": "option_pr",
  "confidence": 0.91,
  "risk_level": "LOW",
  "auto_remediation_safe": true
}
```

Why I chose find/replace patches instead of unified diffs:

> A unified-diff applier needs hunk fuzzing, line-number tolerance, and
> conflict resolution - three places where you can land a half-applied
> patch on `main`. Find/replace is narrower: the substring is either
> there or it isn't. If Claude proposed a patch that doesn't match the
> current file, the Lambda refuses and falls back to rollback. The
> failure mode is "we did nothing", not "we corrupted a file."

### What didn't work

This is the most useful section. Be honest:

- **AWS DevOps Agent.** I built the whole webhook bridge first - HMAC
  signing, an SSM SecureString for the secret, the whole flow. The
  agent never responded. CloudTrail showed its IAM role was never even
  assumed. I spent half a day debugging the Slack signing format
  before realising the gate was upstream of my code.
- **AWS FIS.** Not available on Free Plan, and adds enough
  complication on Paid Plan that I defaulted to `aws ecs stop-task`
  instead.
- **Free Plan in general.** Three different services were silently
  gated. Each one cost an hour of confusion. Lesson: when AWS returns
  403 with a vague subscription error, check the plan before checking
  IAM.

### What I'd change

A focused list:

1. **Idempotency on the SNS subscription.** A flapping alarm can fire
   the investigator four times in a minute. A small DDB lock keyed on
   `<alarm,timestamp_bucket>` would dedupe.
2. **A signed audit trail of auto-remediation actions.** Right now the
   only record is the Slack post and a Lambda log line.
3. **Per-user authorisation in the actions Lambda.** Anyone in the
   Slack channel can click rollback. For a team setting I'd validate
   `payload.user.id` against an allowlist in SSM.
4. **Bedrock budget alarms.** Each call is ~$0.05; an alarm storm
   could runaway. CloudWatch composite alarms on Bedrock invocation
   count would cap it.

### What I learned

- **Confidence scoring is more useful than a yes/no recommendation.**
  Claude's confidence on synthetic alarms hovers around 0.5; on real
  broken commits with the diff in context, it climbs to 0.85+. The
  threshold becomes the system's risk dial.
- **Memory layers are cheap and pay for themselves fast.** DynamoDB
  PAY_PER_REQUEST plus a TTL is ~$0.10/month; even a single skipped
  Bedrock call covers it.
- **`OnFailure: ROLLBACK` on CodePipeline V2 is one line of YAML and
  half the value of the whole demo.** The agent layer adds the RCA
  message, but the actual self-healing is already happening at the
  pipeline level.
- **Find/replace beats diff-apply for LLM-generated patches.**
  Repeatedly. Every time. Don't fight this.
- **A console-clickable demo trigger is worth more than a one-liner
  shell script.** It's not engineering work; it's storytelling work.
  But it's the difference between "I built this" and "let me show
  you."

### Why this matters

Pitch on real-world relevance:

> This isn't trying to replace SREs. It's trying to compress the time
> between "an alarm fires" and "the right person sees the right
> structured RCA." For most incidents, that's ten minutes that you
> save. For some incidents, the rollback or PR is a button click that
> the on-call doesn't have to make at 3am.
>
> The part that surprised me most is how much the LLM matters less
> than the schema you put around it. Claude is the easy part. The
> three years of hard parts are: choosing what context to gather,
> choosing what shape of output is safe to act on, and choosing where
> to put the human in the loop.

### Closing

Short. Maybe link to the repo. Maybe a one-liner like:

> The whole thing is a single CloudFormation parent stack and ~1500
> lines of Python. If you want to play with it, the README walks you
> through a deploy in 10-15 minutes.

## Things to include / link

- Mermaid architecture diagram (already in `docs/architecture.md`)
- Screenshot of a real Slack RCA message with confidence + buttons
- Screenshot of a generated PR
- Screenshot of the chaos-trigger Lambda Test tab with the saved events
- Cost table from the README

## Things to deliberately leave out

- Account IDs, ARNs, the Slack workspace name. Use `<placeholder>`.
- The original Claude prompt that bootstrapped the project. The
  article should read like you wrote the system, not like Claude wrote
  it for you.
- Specific dollar figures from your AWS bill beyond the high-level
  monthly estimate.

## Voice notes

- First person, but light on it. "I built", not "I architected".
- Show the broken paths. The DevOps Agent failure is the strongest
  honest moment in the story; don't smooth it over.
- Use code blocks for one-line claims, prose for everything else.
- One screenshot per major section is plenty.
- Avoid "self-healing" as a noun. Use it once, max, then describe what
  the system actually does.
