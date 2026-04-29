"""
Investigator Lambda — Bedrock-backed RCA + auto-rollback agent.

Subscribed to the chaos-demo-alarms SNS topic. On each ALARM event:
  1. Gathers ECS service state, recent container logs, and pipeline history.
  2. Asks Claude (via Bedrock Converse API) to produce a structured decision:
     ROLLBACK / WAIT / MANUAL_INVESTIGATE.
  3. Posts the RCA + decision to Slack via an incoming webhook.
  4. If decision is ROLLBACK and a target execution id exists, calls
     codepipeline:RollbackStage to revert the Deploy stage.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

CLUSTER = os.environ["ECS_CLUSTER"]
SERVICE = os.environ["ECS_SERVICE"]
PIPELINE = os.environ["PIPELINE_NAME"]
LOG_GROUP = os.environ["ECS_LOG_GROUP"]
SLACK_URL_PARAM = os.environ["SLACK_URL_PARAM"]
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)
GITHUB_REPO = os.environ.get("GITHUB_REPO", "adithyasubas/AIOps-AWS")
GITHUB_TOKEN_PARAM = os.environ.get("GITHUB_TOKEN_PARAM", "")

ssm = boto3.client("ssm")
ecs = boto3.client("ecs")
logs_client = boto3.client("logs")
cp = boto3.client("codepipeline")
bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

_slack_url = None
_github_token = None


def get_slack_url() -> str:
    global _slack_url
    if _slack_url:
        return _slack_url
    resp = ssm.get_parameter(Name=SLACK_URL_PARAM, WithDecryption=True)
    _slack_url = resp["Parameter"]["Value"]
    return _slack_url


def get_github_token() -> str | None:
    global _github_token
    if _github_token is not None:
        return _github_token or None
    if not GITHUB_TOKEN_PARAM:
        _github_token = ""
        return None
    try:
        resp = ssm.get_parameter(Name=GITHUB_TOKEN_PARAM, WithDecryption=True)
        _github_token = resp["Parameter"]["Value"]
        return _github_token
    except Exception as e:
        LOG.warning(f"GITHUB_TOKEN_PARAM not readable, falling back to unauthenticated: {e}")
        _github_token = ""
        return None


def fetch_recent_commit(repo: str, sha: str) -> dict:
    """Fetch a commit's metadata + per-file patches via the GitHub REST API."""
    if not repo or not sha:
        return {"error": "missing repo or sha"}
    url = f"https://api.github.com/repos/{repo}/commits/{sha}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "chaos-demo-investigator",
    }
    token = get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": f"GitHub HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"}
    except Exception as e:
        return {"error": f"GitHub fetch failed: {e}"}
    return {
        "sha": data.get("sha", "")[:12],
        "message": data.get("commit", {}).get("message", "")[:500],
        "author": data.get("commit", {}).get("author", {}).get("name", ""),
        "url": data.get("html_url", ""),
        "files": [
            {
                "filename": f["filename"],
                "status": f["status"],
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "patch": f.get("patch", "")[:1500],
            }
            for f in data.get("files", [])[:6]
        ],
    }


def gather_ecs_state() -> dict:
    svc = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])["services"][0]
    return {
        "desired": svc["desiredCount"],
        "running": svc["runningCount"],
        "pending": svc["pendingCount"],
        "deployments": [
            {
                "id": d["id"],
                "status": d["status"],
                "rolloutState": d.get("rolloutState"),
                "rolloutStateReason": d.get("rolloutStateReason"),
                "running": d["runningCount"],
                "createdAt": d["createdAt"].isoformat(),
            }
            for d in svc["deployments"][:3]
        ],
        "recentEvents": [e["message"] for e in svc["events"][:5]],
    }


def gather_recent_logs() -> list[str]:
    end = int(time.time() * 1000)
    start = end - 10 * 60 * 1000
    try:
        resp = logs_client.filter_log_events(
            logGroupName=LOG_GROUP, startTime=start, endTime=end, limit=20
        )
        return [e["message"][:200] for e in resp.get("events", [])][-15:]
    except Exception as e:
        return [f"(could not read logs: {e})"]


def gather_pipeline_history() -> list[dict]:
    resp = cp.list_pipeline_executions(pipelineName=PIPELINE, maxResults=5)
    return [
        {
            "id": e["pipelineExecutionId"],
            "status": e["status"],
            "startTime": e["startTime"].isoformat(),
            "sourceRevisions": [
                {"summary": r.get("revisionSummary", "")[:80], "id": r.get("revisionId")}
                for r in e.get("sourceRevisions", [])
            ],
        }
        for e in resp.get("pipelineExecutionSummaries", [])
    ]


def call_bedrock(
    alarm: dict,
    ecs_state: dict,
    log_lines: list[str],
    pipeline_history: list[dict],
    recent_commit: dict,
) -> dict:
    prompt = f"""You are an SRE investigation agent for a containerised ECS Fargate application
behind an ALB, fed by a CodePipeline V2 pipeline with on-failure rollback.

The CURRENTLY DEPLOYED commit is the latest entry in pipeline executions (newest first).
A unified diff for that commit is provided below — use it to pinpoint the change that broke the service.

ALARM:
{json.dumps(alarm, indent=2)}

ECS SERVICE STATE:
{json.dumps(ecs_state, indent=2, default=str)}

LAST CONTAINER LOG LINES (newest last; may be empty if tasks died fast):
{json.dumps(log_lines, indent=2)}

LAST PIPELINE EXECUTIONS (newest first):
{json.dumps(pipeline_history, indent=2, default=str)}

RECENT DEPLOY COMMIT (the change currently running in production):
{json.dumps(recent_commit, indent=2, default=str)}

Your job: produce a structured RCA, identify the broken code (cite filename + lines if possible),
and propose a concrete fix as a unified diff. Then offer the human three options.

Respond ONLY with a single valid JSON object, no surrounding prose, no code fences:
{{
  "rca": "<2-3 sentence root cause analysis tying the alarm to a specific cause>",
  "deploy_correlation": "yes|no|unclear with one short sentence",
  "implicated_files": ["<filename>", ...],
  "broken_code_excerpt": "<a short verbatim quote of the offending lines, or empty if not identifiable>",
  "fix_summary": "<one sentence describing what the fix does>",
  "fix_diff": "<unified diff text starting with --- a/<file>\\n+++ b/<file>\\n@@ ...; or empty string if no fix is recommended>",
  "options": [
    {{"id": "A", "name": "Trust auto-rollback", "description": "<why/why not>", "command": "<exact CLI command or 'no action needed'>"}},
    {{"id": "B", "name": "Apply the agent's fix", "description": "<why/why not>", "command": "<exact CLI command to apply the fix>"}},
    {{"id": "C", "name": "Delegate to Kiro for an automated PR", "description": "<why/why not>", "command": "<exact tag/handoff>"}}
  ],
  "recommended_option": "A|B|C",
  "recommended_action": "ROLLBACK|WAIT|MANUAL_INVESTIGATE",
  "rollback_target_execution_id": "<id of the previous SUCCEEDED execution, or null>",
  "slack_summary": "<one-line summary <= 120 chars>"
}}
"""
    resp = bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 2000, "temperature": 0.2},
    )
    text = resp["output"]["message"]["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    return json.loads(text.strip())


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 3] + "..."


def post_slack(url: str, alarm_name: str, decision: dict) -> None:
    action = decision.get("recommended_action", "MANUAL_INVESTIGATE")
    emoji = {"ROLLBACK": ":rewind:", "WAIT": ":hourglass_flowing_sand:", "MANUAL_INVESTIGATE": ":mag:"}.get(
        action, ":robot_face:"
    )
    files = decision.get("implicated_files") or []
    files_md = ", ".join(f"`{f}`" for f in files[:5]) if files else "_none identified_"
    excerpt = _truncate(decision.get("broken_code_excerpt") or "", 600)
    fix_diff = _truncate(decision.get("fix_diff") or "", 2400)
    fix_summary = decision.get("fix_summary") or "(no fix proposed)"
    rec = decision.get("recommended_option") or ""

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} Investigation: {alarm_name}"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Action*\n{action}"},
                {"type": "mrkdwn", "text": f"*Deploy implicated*\n{decision.get('deploy_correlation', '?')}"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary:* {decision.get('slack_summary', '(none)')}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Root cause:*\n{decision.get('rca', '(none)')}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Implicated files:* {files_md}"}},
    ]
    if excerpt:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Offending code:*\n```{excerpt}```"}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Proposed fix:* {fix_summary}"}})
    if fix_diff:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{fix_diff}```"}})

    blocks.append({"type": "divider"})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Pick an option (recommended: " + (rec or "—") + ")*"}})

    options_list = decision.get("options") or []
    for opt in options_list:
        oid = opt.get("id", "?")
        name = opt.get("name", "")
        desc = opt.get("description", "")
        cmd = opt.get("command", "")
        prefix = "✅ " if oid == rec else ""
        text = f"*{prefix}Option {oid} — {name}*\n{desc}"
        if cmd:
            text += f"\n```{_truncate(cmd, 600)}```"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    # Interactive buttons. Click → Slack POSTs to API Gateway → investigator-actions Lambda.
    # Requires the investigator-interactions stack deployed AND Slack app Interactivity enabled
    # with the Request URL pointing at the API GW endpoint.
    rollback_target = decision.get("rollback_target_execution_id")
    kiro_message = ""
    for opt in options_list:
        if opt.get("id") == "C":
            kiro_message = opt.get("command", "") or ""
            break
    btn_value = json.dumps(
        {
            "alarm_name": alarm_name,
            "rollback_target_execution_id": rollback_target,
            "kiro_message": kiro_message,
        }
    )
    button_elements: list[dict] = []
    for opt in options_list[:3]:
        oid = opt.get("id", "?")
        action_id = {"A": "option_a", "B": "option_b", "C": "option_c"}.get(oid)
        if not action_id:
            continue
        btn = {
            "type": "button",
            "action_id": action_id,
            "text": {"type": "plain_text", "text": f"{oid} — {_truncate(opt.get('name', ''), 30)}"},
            "value": btn_value,
        }
        if oid == rec:
            btn["style"] = "primary"
        button_elements.append(btn)
    if button_elements:
        blocks.append({"type": "actions", "block_id": "investigator_actions", "elements": button_elements})

    if action == "ROLLBACK" and decision.get("rollback_target_execution_id"):
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f":information_source: Suggested rollback target execution: `{decision['rollback_target_execution_id']}`",
                    }
                ],
            }
        )

    payload = {"blocks": blocks, "text": f"{action}: {decision.get('slack_summary', alarm_name)}"}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            LOG.info(f"Slack post: {r.status}")
    except urllib.error.HTTPError as e:
        LOG.error(f"Slack {e.code}: {e.read().decode(errors='replace')[:200]}")


def execute_rollback(target_execution_id: str) -> None:
    LOG.info(f"Rolling back Deploy stage to execution {target_execution_id}")
    try:
        cp.rollback_stage(
            pipelineName=PIPELINE,
            stageName="Deploy",
            targetPipelineExecutionId=target_execution_id,
        )
    except Exception as e:
        LOG.error(f"rollback_stage failed ({e}), falling back to start_pipeline_execution")
        cp.start_pipeline_execution(name=PIPELINE)


def handler(event, context):
    try:
        msg = json.loads(event["Records"][0]["Sns"]["Message"])
        alarm_name = msg.get("AlarmName", "unknown")
        new_state = msg.get("NewStateValue", "")
        if new_state != "ALARM":
            LOG.info(f"Skipping non-ALARM: {alarm_name} -> {new_state}")
            return {"status": "skipped"}

        LOG.info(f"Investigating {alarm_name}")
        ecs_state = gather_ecs_state()
        log_lines = gather_recent_logs()
        pipeline_history = gather_pipeline_history()

        # Pull the SHA from the most recent pipeline execution and fetch the diff
        recent_commit: dict = {}
        for exec_summary in pipeline_history:
            for rev in exec_summary.get("sourceRevisions", []) or []:
                if rev.get("id"):
                    recent_commit = fetch_recent_commit(GITHUB_REPO, rev["id"])
                    break
            if recent_commit:
                break

        decision = call_bedrock(
            {
                "name": alarm_name,
                "state": new_state,
                "reason": msg.get("NewStateReason"),
                "time": msg.get("StateChangeTime"),
            },
            ecs_state,
            log_lines,
            pipeline_history,
            recent_commit,
        )
        LOG.info(f"Decision: {json.dumps(decision)}")

        post_slack(get_slack_url(), alarm_name, decision)
        # Intentionally do NOT auto-execute rollback. The Slack message presents
        # the human with three concrete options and the exact commands; the
        # operator chooses which one to run. (Interactive buttons are a planned
        # Phase 2 add-on — see scripts/setup-investigator.md.)
    except Exception as e:
        LOG.exception(f"Investigator error: {e}")
    return {"status": "done"}
