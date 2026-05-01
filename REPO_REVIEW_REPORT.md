# Repository review report

A senior-engineer audit of the repo for public-portfolio readiness.
Run on 2026-05-01.

## Summary

The repo is in good shape. The application works end-to-end (verified
in a prior session: memory miss + memory hit + auto-remediation gate +
Slack interactivity all confirmed live). The codebase is small and
purposeful: ~860 LOC of Python across three Lambdas, ~1500 LOC of
CloudFormation across eight nested stacks, plus a tiny Express app and
three shell scripts. Nothing is fake or load-bearing only for the
demo.

The audit identified one portfolio risk (the original AI prompt was
checked in), one security non-issue (no real secrets in source, but
the previous chat history did expose secrets that should be rotated),
several pieces of dead code from abandoned features, and a missing
LICENSE / tests / current architecture doc.

All actionable items below have been applied.

## What was found

### Security

- **No exposed secrets** in any source file. `git grep` for AKIA, sk_,
  github_pat_, hooks.slack.com/services, and BEGIN PRIVATE KEY all
  came back clean.
- `.DS_Store` x2 were tracked. macOS metadata, harmless but unprofessional.
  Removed and properly gitignored.
- No hardcoded account IDs, ARNs, or Slack workspace identifiers in
  source.
- `claude-code-prompt.md` was the original AI bootstrap prompt
  committed to the repo. Not a security issue but a portfolio risk:
  any reader opening that file would conclude the project was
  AI-generated rather than engineered. **Removed.**

### Dead code

- `lambda/index.py` + `cloudformation/webhook-bridge.yaml` - the
  legacy AWS DevOps Agent webhook bridge. The DevOps Agent path was
  abandoned (see `docs/decisions.md` ADR-1). The bridge still posted
  to a webhook that returns 403 forever. **Removed**, including the
  `WebhookBridgeStack` reference in `cloudformation/main.yaml`.
- `scripts/setup-devops-agent.md` - manual setup instructions for the
  abandoned DevOps Agent path. **Removed.**

### Documentation

- `docs/architecture.md` was from the V1 (pre-Bedrock) era. **Rewritten** to
  match the current architecture, with a Mermaid diagram and a
  "deliberate limits" section that calls out the failure paths.
- No setup, usage, or decisions documentation. **Added** as
  `docs/setup.md`, `docs/usage.md`, `docs/decisions.md`.
- No Medium article preparation. **Added** `docs/medium-article-draft.md`
  with title options, sections, voice notes, and a list of things to
  deliberately leave out.

### Testing

- No tests existed. **Added** `tests/test_signature.py` with 16 unit
  tests covering the pure investigator functions (`normalize_error`,
  `build_incident_signature`, `auto_action_for`). All pass. These are
  the parts that govern memory-layer correctness and the
  auto-remediation gate, so they're worth testing in isolation. The
  AWS-integration paths (Bedrock, DynamoDB, CodePipeline, GitHub API,
  Slack) are exercised by deploying and using the chaos-trigger
  Lambda - mocking those would be worse than no tests at all.

### Hygiene

- `.gitignore` was minimal. **Expanded** to cover `.env`, `*.pem`,
  `*.key`, IDE folders, and AWS credentials.
- No LICENSE. **Added** MIT.
- No CONTRIBUTING.md - intentionally not added; this is a portfolio
  project not a contribution-seeking OSS project.
- No CI workflow. Intentionally not added; the project IS the CI/CD,
  adding GitHub Actions on top would be confusing.

## Files changed

### Removed (with rationale)

| File | Why |
|---|---|
| `.DS_Store`, `App/.DS_Store` | macOS metadata, gitignored |
| `claude-code-prompt.md` | Original AI prompt; portfolio risk |
| `lambda/index.py` | Dead code (DevOps Agent webhook bridge) |
| `cloudformation/webhook-bridge.yaml` | Same |
| `scripts/setup-devops-agent.md` | Dead instructions |

### Added

| File | What it is |
|---|---|
| `LICENSE` | MIT |
| `docs/architecture.md` (rewrite) | Current architecture + Mermaid diagram |
| `docs/setup.md` | Deploy walkthrough |
| `docs/usage.md` | Demo usage |
| `docs/decisions.md` | Nine ADRs covering the non-obvious choices |
| `docs/medium-article-draft.md` | Article outline + draft prose |
| `tests/test_signature.py` | 16 unit tests on pure investigator functions |
| `REPO_REVIEW_REPORT.md` | This document |

### Modified

| File | Change |
|---|---|
| `README.md` | Full rewrite with Mermaid, screenshots placeholders, author section |
| `cloudformation/main.yaml` | Dropped `WebhookBridgeStack`; updated description |
| `.gitignore` | Broader coverage of secrets / artefacts / IDE files |

## Issues fixed

1. Tracked `.DS_Store` files removed.
2. Original AI prompt removed.
3. Dead webhook-bridge code path removed (lambda + CFN + main.yaml reference).
4. Architecture doc updated from V1 to current.
5. Missing LICENSE added.
6. Missing tests added.
7. README first-person voice toned down; added author section, Mermaid
   diagram, screenshots placeholders.

## Security risks identified

1. **No risks in source code.** All scans clean.
2. **In-chat secret exposure (out of repo scope).** During development
   the Slack signing secret, Slack webhook URL, and DevOps Agent
   webhook secret were pasted into chat. They are not in the repo,
   but they are in transcript history. **Recommendation: rotate them
   before publishing the article that links to the repo.**
   - Slack signing secret: regenerate at api.slack.com -> your app -> Basic Information -> Regenerate
   - Slack webhook URL: revoke + regenerate at the Incoming Webhooks page
   - DevOps Agent webhook: irrelevant (the path was abandoned)
3. **GitHub PAT.** Only created if you opted into the PR feature. Use
   a fine-grained token scoped to the single repo and short
   expiration; never paste it anywhere.

## Remaining improvements (not applied)

These are non-blocking but would harden the project for production
use:

- **Idempotency on SNS subscription.** SNS retries can fire the
  investigator twice for the same alarm transition. A small DynamoDB
  lock keyed on `<alarm,timestamp_bucket>` would dedupe.
- **Per-user authorisation in actions Lambda.** Anyone in the Slack
  channel can click rollback. For a team setting validate
  `payload.user.id` against an allowlist in SSM.
- **Bedrock budget alarms.** A composite CloudWatch alarm on Bedrock
  invocation count would cap cost in a runaway scenario.
- **Screenshots.** Drop real PNGs into `docs/screenshots/` and update
  the README references.
- **CONTRIBUTING.md / CODE_OF_CONDUCT.md.** Add only if the repo is
  meant to receive PRs; not needed for a portfolio project.

## Suggestions before publishing publicly

A pre-flight checklist (also reproduced at the bottom).

1. **Rotate the three secrets** that were exposed during development
   (Slack signing secret, Slack incoming webhook, DevOps Agent webhook).
2. **Re-deploy the stack** with the new template so `WebhookBridgeStack`
   is cleanly removed from the live account. Command:
   ```bash
   bash scripts/deploy.sh   # or the targeted deploy from docs/setup.md
   ```
3. **Take 4 screenshots** for the README (Slack RCA, memory hit,
   chaos-trigger console, generated PR).
4. **Run the unit tests once** to confirm nothing regresses on this
   machine: `python3 -m unittest tests.test_signature`.
5. **Read every file in `docs/`** before pushing. They were drafted in
   one pass; I want you to skim for anything that doesn't sound like
   you.
6. **Pick a Medium title** from the four options in
   `docs/medium-article-draft.md` and start the draft from that
   skeleton. Don't post the draft as-is; rewrite it in your voice.

## Suggested GitHub repo description

Choose one:

- "Self-healing CI/CD on AWS with a Bedrock-backed incident
  investigator. CodePipeline V2 + ECS Fargate + Claude + DynamoDB
  memory + Slack interactivity."
- "An AIOps loop on AWS: CloudWatch alarms in, Claude RCA out, GitHub
  PR optional. ~$30/month."
- "What happens when you let an LLM look at your CloudWatch alarms.
  Production-shaped demo with rollback, memory, and Slack buttons."

Recommendation: option 1. It's the most descriptive of the actual
content and uses concrete service names that AWS-fluent recruiters
will scan for.

## Suggested GitHub topics

```
aws
aws-cloudformation
aws-lambda
ecs-fargate
codepipeline
bedrock
anthropic-claude
aiops
sre
slack-bot
incident-response
self-healing
devops
infrastructure-as-code
serverless
```

Pick 8-10. GitHub allows up to 20 but most readers stop at the first
6.

## Suggested Medium article angle

The strongest angle is **"the broken paths matter as much as the
working one."** The DevOps Agent failure, the Free Plan gating, the
ECR-empty-on-first-deploy chicken-and-egg, the find/replace vs unified
diff trade-off - those are the moments that make the article useful to
other engineers. Don't sand them off.

The second strongest angle is **the cost story.** A working LLM-driven
SRE loop for ~$30/month is a non-obvious result. Lead with it in the
hook.

Avoid:
- "10 lessons I learned" listicle structure
- "I built X" without explaining what was hard
- AI marketing language ("revolutionary", "self-healing", "intelligent
  agent")
- Quoting Claude's prompt verbatim (it makes the article feel
  derivative)

## Final pre-publish checklist

Tick these in order before the first `git push --tags` for v1.0:

- [ ] Run `python3 -m unittest tests.test_signature` - 16 tests pass
- [ ] Read each file in `docs/`, edit prose to your voice
- [ ] Read the README, replace any placeholder you'd rather not have
      in print (account IDs, ARNs, etc.)
- [ ] Take 4 screenshots, drop them into `docs/screenshots/`,
      uncomment the references in the README
- [ ] Rotate Slack signing secret + Slack incoming webhook URL
- [ ] Update `docs/setup.md` with your final account / connection ARN
      placeholders if you're publishing them
- [ ] Pick a GitHub repo description from the suggestions above
- [ ] Add 8-10 topics from the list above
- [ ] (Optional) Set the repo's default branch description and pin it
      on your GitHub profile
- [ ] Commit the changes from this audit:
      ```bash
      git add -A
      git commit -m "audit: remove dead code, add docs, tests, license, README polish"
      git push
      ```
- [ ] Write the Medium article from `docs/medium-article-draft.md` in
      your voice, take a coffee, post it
