"""
Investigator Lambda.

SNS alarm in -> RCA out:

  1. Build a stable signature from alarm + service + normalized error logs.
  2. Look the signature up in DynamoDB. If we have a known good fix that's
     succeeded before, skip Bedrock and reuse it.
  3. Otherwise call Claude (Bedrock) with ECS state, recent logs, pipeline
     history and the most-recent commit's diff. Save the response to DDB.
  4. Post a Slack Block Kit message with confidence, risk, memory hit/miss,
     and four buttons (trust auto-rollback, rollback now, create fix PR,
     delegate to Kiro).
  5. If AUTO_REMEDIATE_ENABLED and confidence/risk gates pass, execute the
     recommended action automatically (rollback or create_pr).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

# ---- env -------------------------------------------------------------------

CLUSTER = os.environ["ECS_CLUSTER"]
SERVICE = os.environ["ECS_SERVICE"]
PIPELINE = os.environ["PIPELINE_NAME"]
LOG_GROUP = os.environ["ECS_LOG_GROUP"]
SLACK_URL_PARAM = os.environ["SLACK_URL_PARAM"]

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "adithyasubas")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "AIOps-AWS")
GITHUB_BASE_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "main")
GITHUB_TOKEN_PARAM = os.environ.get("GITHUB_TOKEN_PARAM_NAME", "")

MEMORY_TABLE = os.environ.get("INCIDENT_MEMORY_TABLE_NAME", "")
MEMORY_ENABLED = os.environ.get("INCIDENT_MEMORY_ENABLED", "true").lower() == "true"
MEMORY_TTL_DAYS = int(os.environ.get("INCIDENT_MEMORY_TTL_DAYS", "90"))
MEMORY_MIN_SUCCESS = int(os.environ.get("MEMORY_MIN_SUCCESS_COUNT", "1"))

AUTO_ENABLED = os.environ.get("AUTO_REMEDIATE_ENABLED", "false").lower() == "true"
AUTO_THRESHOLD = float(os.environ.get("AUTO_REMEDIATE_CONFIDENCE_THRESHOLD", "0.85"))
AUTO_ALLOWED = {
    a.strip()
    for a in os.environ.get("AUTO_REMEDIATE_ALLOWED_ACTIONS", "rollback,create_pr").split(",")
    if a.strip()
}
AUTO_REQUIRE_LOW_RISK = os.environ.get("AUTO_REMEDIATE_REQUIRE_LOW_RISK", "true").lower() == "true"

# ---- aws clients -----------------------------------------------------------

ssm = boto3.client("ssm")
ecs = boto3.client("ecs")
logs_client = boto3.client("logs")
cp = boto3.client("codepipeline")
bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
ddb = boto3.client("dynamodb")

_slack_url: str | None = None
_github_token: str | None = None


# ---- secret cache ----------------------------------------------------------

def get_slack_url() -> str:
    global _slack_url
    if _slack_url:
        return _slack_url
    _slack_url = ssm.get_parameter(Name=SLACK_URL_PARAM, WithDecryption=True)["Parameter"]["Value"]
    return _slack_url


def get_github_token() -> str | None:
    global _github_token
    if _github_token is not None:
        return _github_token or None
    if not GITHUB_TOKEN_PARAM:
        _github_token = ""
        return None
    try:
        _github_token = ssm.get_parameter(Name=GITHUB_TOKEN_PARAM, WithDecryption=True)["Parameter"]["Value"]
        return _github_token
    except Exception as e:
        LOG.warning(f"github token unreadable, falling back to anon: {e}")
        _github_token = ""
        return None


# ---- context gathering -----------------------------------------------------

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
        return [e["message"][:300] for e in resp.get("events", [])][-15:]
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


def fetch_recent_commit(sha: str) -> dict:
    if not sha:
        return {}
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits/{sha}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "chaos-investigator"}
    token = get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:
            data = json.loads(r.read())
    except Exception as e:
        return {"error": f"github commit fetch failed: {e}"}
    return {
        "sha": data.get("sha", "")[:12],
        "full_sha": data.get("sha", ""),
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


# ---- memory layer ----------------------------------------------------------

# Strip volatile parts (timestamps, request/task ids, hex tokens) before
# hashing so the same kind of failure produces the same signature.
_VOLATILE = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    re.compile(r"\b[0-9a-f]{16,64}\b"),
    re.compile(r"\b\d{10,}\b"),
    re.compile(r"\s+"),
]


def normalize_error(log_lines: list[str]) -> str:
    error_lines: list[str] = []
    for line in log_lines:
        if any(k in line for k in ("Error", "error", "Exception", "Traceback", "FATAL", "throw")):
            error_lines.append(line)
    if not error_lines:
        error_lines = log_lines[-3:]
    blob = " ".join(error_lines)[:2000]
    for pat in _VOLATILE:
        blob = pat.sub(" ", blob)
    return blob.strip().lower()


def build_incident_signature(alarm_name: str, service: str, log_lines: list[str], pipeline_history: list[dict]) -> str:
    failed_stage = ""
    for ex in pipeline_history:
        if ex.get("status") in ("Failed", "Stopped"):
            failed_stage = ex.get("status", "")
            break
    parts = [
        alarm_name or "",
        service or "",
        normalize_error(log_lines),
        failed_stage,
    ]
    h = hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"sig_{h}"


def get_memory_match(signature: str) -> dict | None:
    if not (MEMORY_ENABLED and MEMORY_TABLE and signature):
        return None
    try:
        resp = ddb.get_item(
            TableName=MEMORY_TABLE,
            Key={"incident_signature": {"S": signature}},
            ConsistentRead=False,
        )
    except Exception as e:
        LOG.warning(f"memory lookup failed: {e}")
        return None
    item = resp.get("Item")
    if not item:
        return None
    parsed = {k: list(v.values())[0] for k, v in item.items()}
    try:
        if int(parsed.get("success_count", "0")) < MEMORY_MIN_SUCCESS:
            return None
    except ValueError:
        return None
    if parsed.get("decision_json"):
        try:
            parsed["decision"] = json.loads(parsed["decision_json"])
        except Exception:
            pass
    return parsed


def save_incident_memory(signature: str, decision: dict, ctx: dict) -> None:
    if not (MEMORY_ENABLED and MEMORY_TABLE and signature):
        return
    now = int(time.time())
    ttl = now + MEMORY_TTL_DAYS * 86400
    item = {
        "incident_signature": {"S": signature},
        "alarm_name": {"S": ctx.get("alarm_name", "")[:128]},
        "service_name": {"S": SERVICE},
        "first_seen_at": {"N": str(now)},
        "last_seen_at": {"N": str(now)},
        "success_count": {"N": "0"},
        "failure_count": {"N": "0"},
        "ttl": {"N": str(ttl)},
        "decision_json": {"S": json.dumps(decision)[:32_000]},
        "root_cause": {"S": (decision.get("root_cause") or "")[:1000]},
        "fix_summary": {"S": (decision.get("fix_summary") or "")[:1000]},
        "recommended_option": {"S": decision.get("recommended_option") or ""},
        "confidence": {"N": str(decision.get("confidence") or 0)},
        "risk_level": {"S": decision.get("risk_level") or ""},
        "pipeline_execution_id": {"S": ctx.get("pipeline_execution_id", "")[:128]},
        "commit_sha": {"S": ctx.get("commit_sha", "")[:64]},
    }
    try:
        ddb.put_item(TableName=MEMORY_TABLE, Item=item)
    except Exception as e:
        LOG.warning(f"memory put failed: {e}")


def update_memory_counter(signature: str, success: bool) -> None:
    if not (MEMORY_ENABLED and MEMORY_TABLE and signature):
        return
    attr = "success_count" if success else "failure_count"
    try:
        ddb.update_item(
            TableName=MEMORY_TABLE,
            Key={"incident_signature": {"S": signature}},
            UpdateExpression=f"ADD {attr} :one SET last_seen_at = :now",
            ExpressionAttributeValues={
                ":one": {"N": "1"},
                ":now": {"N": str(int(time.time()))},
            },
        )
    except Exception as e:
        LOG.warning(f"memory update failed: {e}")


# ---- Bedrock --------------------------------------------------------------

def call_bedrock(alarm: dict, ecs_state: dict, log_lines: list[str], pipeline_history: list[dict], commit: dict) -> dict:
    prompt = f"""You are an SRE investigation agent for a containerised ECS Fargate application
behind an ALB, deployed by CodePipeline V2.

You have:
- The alarm payload.
- Current ECS service state and recent service events.
- The last 15 container log lines (newest last).
- The last 5 pipeline executions (newest first).
- The currently-deployed commit's diff (per-file patches).

Decide:
1. The root cause, in 2-3 sentences.
2. Whether the most recent deploy is implicated (yes/no/unclear) with a one-sentence reason.
3. The implicated source files. Cite filenames.
4. A short verbatim quote of the offending lines if you can identify them.
5. A one-sentence summary of the fix.
6. A safe, narrow fix as a list of single-file find/replace patches. The "find" string must be an
   EXACT verbatim substring of the current file contents (not a paraphrase) so the actions Lambda
   can apply it without ambiguity. Multi-line is fine. Leave the array empty if you cannot produce
   a confident exact-match patch.
7. Three options for the human plus an optional automated PR option:
   - A "Trust auto-rollback" — for cases where the pipeline already self-rolled back.
   - B "Apply fix via CodePipeline rollback" — re-deploys an older known-good commit.
   - C "Delegate to Kiro for an automated PR" — the legacy option.
   - PR "Create GitHub fix PR" — only if patches is non-empty.
8. The recommended option from {{option_a, option_b, option_c, option_pr}}.
9. A high-level recommended_action from {{ROLLBACK, WAIT, MANUAL_INVESTIGATE, CREATE_PR}}.
10. confidence: a single float 0.0 - 1.0 reflecting how confident you are in the RCA AND the fix.
11. risk_level: one of LOW, MEDIUM, HIGH. LOW = safe to auto-execute, HIGH = human required.
12. auto_remediation_safe: true/false. Independent gut-check: would you let a robot execute this
    without a human in the loop? Be conservative.
13. reason_auto_remediation_safe_or_unsafe: one short sentence.

ALARM:
{json.dumps(alarm, indent=2)}

ECS SERVICE STATE:
{json.dumps(ecs_state, indent=2, default=str)}

RECENT CONTAINER LOG LINES:
{json.dumps(log_lines, indent=2)}

LAST PIPELINE EXECUTIONS:
{json.dumps(pipeline_history, indent=2, default=str)}

DEPLOYED COMMIT:
{json.dumps(commit, indent=2, default=str)}

Respond with a SINGLE JSON object, no surrounding text and no code fences:
{{
  "root_cause": "...",
  "deploy_correlation": "yes|no|unclear with reason",
  "implicated_files": ["..."],
  "broken_code_excerpt": "...",
  "fix_summary": "...",
  "patches": [{{"file_path": "...", "find": "...", "replace": "...", "explanation": "..."}}],
  "options": [
    {{"id": "A", "name": "Trust auto-rollback", "description": "...", "command": "..."}},
    {{"id": "B", "name": "Apply fix via CodePipeline rollback", "description": "...", "command": "..."}},
    {{"id": "C", "name": "Delegate to Kiro for an automated PR", "description": "...", "command": "..."}},
    {{"id": "PR", "name": "Create GitHub fix PR", "description": "...", "command": "click the PR button"}}
  ],
  "recommended_option": "option_a|option_b|option_c|option_pr",
  "recommended_action": "ROLLBACK|WAIT|MANUAL_INVESTIGATE|CREATE_PR",
  "rollback_target_execution_id": "<id of the previous SUCCEEDED forward execution, or null>",
  "summary": "<one-line summary <= 120 chars>",
  "confidence": 0.0,
  "risk_level": "LOW|MEDIUM|HIGH",
  "auto_remediation_safe": true,
  "reason_auto_remediation_safe_or_unsafe": "..."
}}
"""
    resp = bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 3000, "temperature": 0.2},
    )
    text = resp["output"]["message"]["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    return json.loads(text.strip())


# ---- auto-remediation gate -------------------------------------------------

def auto_action_for(decision: dict) -> tuple[str | None, str]:
    """Return (action, reason). action is one of: 'rollback', 'create_pr', or None."""
    if not AUTO_ENABLED:
        return None, "auto-remediation disabled (AUTO_REMEDIATE_ENABLED=false)"
    try:
        confidence = float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        return None, "confidence missing or unparseable; failing safe"
    if confidence < AUTO_THRESHOLD:
        return None, f"confidence {confidence:.2f} below threshold {AUTO_THRESHOLD:.2f}"
    risk = (decision.get("risk_level") or "").upper()
    if risk == "HIGH":
        return None, "risk_level=HIGH — never auto-execute"
    if AUTO_REQUIRE_LOW_RISK and risk != "LOW":
        return None, f"risk_level={risk}; auto-remediation requires LOW (toggle AUTO_REMEDIATE_REQUIRE_LOW_RISK=false to relax)"
    if not decision.get("auto_remediation_safe"):
        return None, "Claude flagged auto_remediation_safe=false"
    rec = (decision.get("recommended_option") or "").lower()
    if rec == "option_pr":
        if "create_pr" not in AUTO_ALLOWED:
            return None, "create_pr not in AUTO_REMEDIATE_ALLOWED_ACTIONS"
        if not decision.get("patches"):
            return None, "no patches to apply"
        return "create_pr", f"confidence {confidence:.2f} >= {AUTO_THRESHOLD:.2f}, risk={risk}, patches present"
    if rec == "option_b":
        if "rollback" not in AUTO_ALLOWED:
            return None, "rollback not in AUTO_REMEDIATE_ALLOWED_ACTIONS"
        return "rollback", f"confidence {confidence:.2f} >= {AUTO_THRESHOLD:.2f}, risk={risk}"
    return None, f"recommended_option={rec} is not auto-eligible"


# ---- rollback (mirrors actions Lambda) ------------------------------------

def _commit_sha_for_execution(execution_id: str) -> str | None:
    try:
        resp = cp.get_pipeline_execution(pipelineName=PIPELINE, pipelineExecutionId=execution_id)
        revs = resp["pipelineExecution"].get("artifactRevisions") or []
        return revs[0].get("revisionId") if revs else None
    except Exception as e:
        LOG.warning(f"could not resolve commit SHA for {execution_id}: {e}")
        return None


def execute_rollback(target_execution_id: str | None) -> str:
    target_sha = _commit_sha_for_execution(target_execution_id) if target_execution_id else None
    try:
        if target_execution_id:
            resp = cp.rollback_stage(
                pipelineName=PIPELINE,
                stageName="Deploy",
                targetPipelineExecutionId=target_execution_id,
            )
            return f"Rolled back Deploy stage to `{target_execution_id}` (rollback id `{resp.get('pipelineExecutionId', '?')}`)."
        raise RuntimeError("no target_execution_id")
    except Exception as e:
        kwargs: dict = {"name": PIPELINE}
        if target_sha:
            kwargs["sourceRevisions"] = [
                {"actionName": "Source", "revisionType": "COMMIT_ID", "revisionValue": target_sha}
            ]
        try:
            resp = cp.start_pipeline_execution(**kwargs)
            new_id = resp.get("pipelineExecutionId", "?")
            if target_sha:
                return f"rollback_stage rejected ({type(e).__name__}); pinned a fresh run `{new_id}` to commit `{target_sha[:12]}`."
            return f"rollback_stage rejected ({type(e).__name__}); started a fresh run `{new_id}` from current HEAD."
        except Exception as e2:
            return f"rollback failed: {e}; fallback also failed: {e2}"


# ---- GitHub PR creation ---------------------------------------------------

_BAD_PATH = re.compile(r"(^/|\.\./|//)")


def _gh_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    token = get_github_token()
    if not token:
        return 0, "no github token configured"
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "chaos-investigator",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, "(unparseable error body)"
    except Exception as e:
        return 0, str(e)


def create_fix_pr(decision: dict, ctx: dict) -> dict:
    """
    Apply Claude's `patches` as exact find/replace on a new branch and open a PR.
    Returns a result dict suitable for Slack rendering.
    """
    patches = decision.get("patches") or []
    if not patches:
        return {"ok": False, "error": "no patches in decision"}

    # signature suffix for the branch name
    sig = (ctx.get("incident_signature") or "incident").replace("sig_", "")[:10]
    branch = f"aiops-fix/{sig}-{int(time.time())}"
    base = GITHUB_BASE_BRANCH

    # 1. base branch SHA
    code, ref = _gh_request("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/ref/heads/{base}")
    if code != 200 or not isinstance(ref, dict):
        return {"ok": False, "error": f"GET ref/heads/{base} -> {code}: {ref}"}
    base_sha = ref["object"]["sha"]

    # 2. create the new branch
    code, _ = _gh_request(
        "POST",
        f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    if code not in (200, 201):
        return {"ok": False, "error": f"create ref -> {code}"}

    applied: list[dict] = []
    for p in patches[:5]:
        path = (p.get("file_path") or "").lstrip("/")
        find = p.get("find") or ""
        replace = p.get("replace") or ""
        if not path or not find or _BAD_PATH.search(path):
            applied.append({"file_path": path, "ok": False, "error": "rejected: empty/invalid path or find"})
            continue
        # 2a. fetch the file
        code, fblob = _gh_request("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{urllib.parse.quote(path)}?ref={base}")
        if code != 200 or not isinstance(fblob, dict) or "content" not in fblob:
            applied.append({"file_path": path, "ok": False, "error": f"GET contents -> {code}"})
            continue
        try:
            current = base64.b64decode(fblob["content"]).decode("utf-8")
        except Exception as e:
            applied.append({"file_path": path, "ok": False, "error": f"decode failed: {e}"})
            continue
        if find not in current:
            applied.append({"file_path": path, "ok": False, "error": "find text not present in file (exact match required)"})
            continue
        if replace == find:
            applied.append({"file_path": path, "ok": False, "error": "replace == find (no-op)"})
            continue
        updated = current.replace(find, replace, 1)
        new_b64 = base64.b64encode(updated.encode("utf-8")).decode("ascii")
        code, _ = _gh_request(
            "PUT",
            f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{urllib.parse.quote(path)}",
            {
                "message": f"AIOps fix: {decision.get('fix_summary', 'apply suggested patch')}\n\nfile: {path}",
                "content": new_b64,
                "branch": branch,
                "sha": fblob["sha"],
            },
        )
        if code not in (200, 201):
            applied.append({"file_path": path, "ok": False, "error": f"PUT contents -> {code}"})
            continue
        applied.append({"file_path": path, "ok": True})

    if not any(a["ok"] for a in applied):
        return {"ok": False, "error": "no patches applied cleanly", "files": applied, "branch": branch}

    # 3. open the PR
    body_lines = [
        f"**Incident summary:** {decision.get('summary', '(none)')}",
        "",
        f"**Root cause:** {decision.get('root_cause', '(none)')}",
        f"**Fix summary:** {decision.get('fix_summary', '(none)')}",
        "",
        f"- Alarm: `{ctx.get('alarm_name', '?')}`",
        f"- Pipeline execution: `{ctx.get('pipeline_execution_id', '?')}`",
        f"- Commit that caused failure: `{ctx.get('commit_sha', '?')[:12]}`",
        f"- AI confidence: `{decision.get('confidence', '?')}`",
        f"- Risk level: `{decision.get('risk_level', '?')}`",
        "",
        "Generated automatically by the AIOps investigator.",
    ]
    title = f"AIOps fix: {decision.get('fix_summary', 'investigator-suggested patch')[:80]}"
    code, pr = _gh_request(
        "POST",
        f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/pulls",
        {"title": title, "head": branch, "base": base, "body": "\n".join(body_lines)},
    )
    if code not in (200, 201) or not isinstance(pr, dict):
        return {"ok": False, "error": f"POST pulls -> {code}: {pr}", "branch": branch, "files": applied}
    return {
        "ok": True,
        "url": pr.get("html_url"),
        "number": pr.get("number"),
        "branch": branch,
        "files": applied,
    }


# ---- Slack rendering -------------------------------------------------------

def _trunc(s: str, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 3] + "..."


def _confidence_emoji(c: float) -> str:
    if c >= 0.9:
        return ":green_circle:"
    if c >= 0.75:
        return ":large_yellow_circle:"
    return ":red_circle:"


def post_slack(url: str, alarm_name: str, decision: dict, ctx: dict) -> None:
    action = decision.get("recommended_action", "MANUAL_INVESTIGATE")
    emoji = {
        "ROLLBACK": ":rewind:",
        "WAIT": ":hourglass_flowing_sand:",
        "MANUAL_INVESTIGATE": ":mag:",
        "CREATE_PR": ":wrench:",
    }.get(action, ":robot_face:")
    files = decision.get("implicated_files") or []
    files_md = ", ".join(f"`{f}`" for f in files[:5]) if files else "_none identified_"
    excerpt = _trunc(decision.get("broken_code_excerpt") or "", 600)
    fix_summary = decision.get("fix_summary") or "(no fix proposed)"
    rec = (decision.get("recommended_option") or "").lower()
    confidence = float(decision.get("confidence") or 0)
    risk = decision.get("risk_level") or "?"
    memory_hit = ctx.get("memory_hit", False)
    auto_status = ctx.get("auto_status") or {}

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} Investigation: {alarm_name}"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Recommended action*\n{action}"},
                {"type": "mrkdwn", "text": f"*Deploy implicated*\n{decision.get('deploy_correlation', '?')}"},
                {"type": "mrkdwn", "text": f"*Confidence*\n{_confidence_emoji(confidence)} {confidence:.2f}"},
                {"type": "mrkdwn", "text": f"*Risk level*\n{risk}"},
            ],
        },
    ]

    if memory_hit:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f":brain: *Memory hit*: known incident `{ctx.get('incident_signature', '?')}` "
                    f"seen {ctx.get('memory_success_count', 0)} time(s) before. Skipped Bedrock call to save cost."
                )
            }],
        })
    else:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f":sparkles: *Fresh investigation* — signature `{ctx.get('incident_signature', '?')}` saved to memory."
            }],
        })

    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary:* {decision.get('summary') or decision.get('slack_summary') or '(none)'}"}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Root cause:*\n{decision.get('root_cause') or decision.get('rca') or '(none)'}"}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Implicated files:* {files_md}"}})
    if excerpt:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Offending code:*\n```{excerpt}```"}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Proposed fix:* {fix_summary}"}})

    # auto-remediation status
    if auto_status.get("performed"):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                f":robot_face: *Auto-remediation executed* — {auto_status.get('action')}.\n"
                f"_{auto_status.get('reason', '')}_\n"
                f"Result: {auto_status.get('result', '(no result)')}"
            )},
        })
    elif auto_status.get("reason"):
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f":lock: *Auto-remediation not performed:* {auto_status.get('reason')}"
            }],
        })

    blocks.append({"type": "divider"})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Pick an option (recommended: {rec or '—'})*"}})

    options_list = decision.get("options") or []
    for opt in options_list:
        oid = opt.get("id", "?")
        prefix = "✅ " if {"A": "option_a", "B": "option_b", "C": "option_c", "PR": "option_pr"}.get(oid, "") == rec else ""
        text = f"*{prefix}Option {oid} — {opt.get('name', '')}*\n{opt.get('description', '')}"
        if opt.get("command"):
            text += f"\n```{_trunc(opt['command'], 400)}```"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    # interactive buttons
    btn_value = json.dumps({
        "alarm_name": alarm_name,
        "rollback_target_execution_id": decision.get("rollback_target_execution_id"),
        "incident_signature": ctx.get("incident_signature"),
        "patches": decision.get("patches") or [],
        "fix_summary": decision.get("fix_summary"),
        "summary": decision.get("summary"),
        "root_cause": decision.get("root_cause"),
        "confidence": decision.get("confidence"),
        "risk_level": decision.get("risk_level"),
        "alarm_name_for_pr": alarm_name,
        "pipeline_execution_id": ctx.get("pipeline_execution_id"),
        "commit_sha": ctx.get("commit_sha"),
    })[:1900]  # Slack limits button value to 2000 chars

    btn_map = [("A", "option_a"), ("B", "option_b"), ("PR", "option_pr"), ("C", "option_c")]
    elements: list[dict] = []
    have_options = {(o.get("id") or "").upper() for o in options_list}
    for oid, action_id in btn_map:
        if oid not in have_options:
            continue
        opt = next((o for o in options_list if (o.get("id") or "").upper() == oid), {})
        btn = {
            "type": "button",
            "action_id": action_id,
            "text": {"type": "plain_text", "text": f"{oid} — {_trunc(opt.get('name', ''), 25)}"},
            "value": btn_value,
        }
        if action_id == rec:
            btn["style"] = "primary"
        if action_id == "option_pr" and not decision.get("patches"):
            btn["confirm"] = {
                "title": {"type": "plain_text", "text": "No patches"},
                "text": {"type": "mrkdwn", "text": "The investigator did not propose any patches; the PR will be empty."},
                "confirm": {"type": "plain_text", "text": "Open PR anyway"},
                "deny": {"type": "plain_text", "text": "Cancel"},
            }
        elements.append(btn)
    if elements:
        blocks.append({"type": "actions", "block_id": "investigator_actions", "elements": elements})

    body = json.dumps({"blocks": blocks, "text": f"{action}: {decision.get('summary', alarm_name)}"}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            LOG.info(f"slack post: {r.status}")
    except urllib.error.HTTPError as e:
        LOG.error(f"slack {e.code}: {e.read().decode(errors='replace')[:200]}")


# ---- handler --------------------------------------------------------------

def handler(event, context):
    try:
        msg = json.loads(event["Records"][0]["Sns"]["Message"])
    except Exception as e:
        LOG.error(f"could not parse SNS message: {e}")
        return {"status": "bad_event"}

    alarm_name = msg.get("AlarmName", "unknown")
    new_state = msg.get("NewStateValue", "")
    if new_state != "ALARM":
        LOG.info(f"skipping non-ALARM: {alarm_name} -> {new_state}")
        return {"status": "skipped"}

    LOG.info(f"investigating {alarm_name}")
    ecs_state = gather_ecs_state()
    log_lines = gather_recent_logs()
    pipeline_history = gather_pipeline_history()

    commit_sha = ""
    pipeline_execution_id = ""
    for ex in pipeline_history:
        pipeline_execution_id = ex.get("id") or pipeline_execution_id
        for rev in ex.get("sourceRevisions") or []:
            if rev.get("id"):
                commit_sha = rev["id"]
                break
        if commit_sha:
            break
    commit_ctx = fetch_recent_commit(commit_sha) if commit_sha else {}

    signature = build_incident_signature(alarm_name, SERVICE, log_lines, pipeline_history)
    ctx = {
        "alarm_name": alarm_name,
        "incident_signature": signature,
        "pipeline_execution_id": pipeline_execution_id,
        "commit_sha": commit_sha,
    }

    # memory lookup
    memory = get_memory_match(signature) if MEMORY_ENABLED else None
    if memory and memory.get("decision"):
        LOG.info(f"memory hit on {signature} (success_count={memory.get('success_count')})")
        decision = memory["decision"]
        ctx["memory_hit"] = True
        ctx["memory_success_count"] = memory.get("success_count", 0)
    else:
        try:
            decision = call_bedrock(
                {"name": alarm_name, "state": new_state, "reason": msg.get("NewStateReason"), "time": msg.get("StateChangeTime")},
                ecs_state,
                log_lines,
                pipeline_history,
                commit_ctx,
            )
            LOG.info(f"decision: {json.dumps(decision)[:1500]}")
        except Exception as e:
            LOG.exception(f"bedrock failed: {e}")
            decision = {
                "root_cause": f"investigator could not call Bedrock: {e}",
                "summary": "investigator-bedrock-error",
                "recommended_action": "MANUAL_INVESTIGATE",
                "confidence": 0.0,
                "risk_level": "HIGH",
                "auto_remediation_safe": False,
                "options": [],
            }
        # save fresh investigations to memory
        save_incident_memory(signature, decision, ctx)
        ctx["memory_hit"] = False

    # auto-remediation gate
    auto_action, auto_reason = auto_action_for(decision)
    auto_status: dict = {"reason": auto_reason}
    if auto_action == "rollback":
        result = execute_rollback(decision.get("rollback_target_execution_id"))
        auto_status = {"performed": True, "action": "rollback", "reason": auto_reason, "result": result}
        update_memory_counter(signature, success=True)
    elif auto_action == "create_pr":
        result = create_fix_pr(decision, ctx)
        if result.get("ok"):
            auto_status = {
                "performed": True,
                "action": "create_pr",
                "reason": auto_reason,
                "result": f"PR opened: {result.get('url')} (branch `{result.get('branch')}`)",
            }
            update_memory_counter(signature, success=True)
        else:
            auto_status = {
                "performed": False,
                "action": "create_pr",
                "reason": f"{auto_reason}; but PR creation failed: {result.get('error')}",
            }
    ctx["auto_status"] = auto_status

    try:
        post_slack(get_slack_url(), alarm_name, decision, ctx)
    except Exception as e:
        LOG.exception(f"slack post failed: {e}")

    return {"status": "done", "memory_hit": ctx.get("memory_hit", False), "auto": auto_status.get("performed", False)}
