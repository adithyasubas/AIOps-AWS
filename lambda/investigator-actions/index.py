"""
Investigator interactions Lambda — receives Slack Block Kit button clicks.

Flow:
  1. API Gateway HTTP API forwards Slack's POST to /slack/actions.
  2. Verify Slack request signature (HMAC-SHA256 over `v0:<ts>:<body>`).
  3. Parse the form-encoded payload and dispatch on action_id:
       option_a → no-op acknowledgement
       option_b → codepipeline:RollbackStage Deploy → previous Succeeded execution
       option_c → post a Kiro handoff message
  4. Use Slack's response_url to post a follow-up to the same channel.

The button's `value` carries the JSON context (alarm name, target execution id,
broken commit SHA, kiro handoff text) that the investigator generated at post time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

PIPELINE = os.environ["PIPELINE_NAME"]
SIGNING_SECRET_PARAM = os.environ["SLACK_SIGNING_SECRET_PARAM"]

ssm = boto3.client("ssm")
cp = boto3.client("codepipeline")

_signing_secret: str | None = None


def get_signing_secret() -> str:
    global _signing_secret
    if _signing_secret:
        return _signing_secret
    resp = ssm.get_parameter(Name=SIGNING_SECRET_PARAM, WithDecryption=True)
    _signing_secret = resp["Parameter"]["Value"]
    return _signing_secret


def verify_slack(headers: dict, raw_body: str) -> bool:
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > 60 * 5:
            return False
    except ValueError:
        return False
    base = f"v0:{ts}:{raw_body}".encode()
    expected = "v0=" + hmac.new(get_signing_secret().encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def respond_in_channel(response_url: str, text: str, thread_ts: str | None = None) -> None:
    payload = {"text": text, "response_type": "in_channel", "replace_original": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    body = json.dumps(payload).encode()
    req = urllib.request.Request(response_url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            LOG.info(f"response_url POST: {r.status}")
    except urllib.error.HTTPError as e:
        LOG.error(f"response_url HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
    except Exception as e:
        LOG.error(f"response_url failed: {e}")


def _commit_sha_for_execution(execution_id: str) -> str | None:
    """Return the source commit SHA that ran in the given pipeline execution."""
    try:
        resp = cp.get_pipeline_execution(pipelineName=PIPELINE, pipelineExecutionId=execution_id)
        revs = resp["pipelineExecution"].get("artifactRevisions") or []
        return revs[0].get("revisionId") if revs else None
    except Exception as e:
        LOG.warning(f"could not resolve commit SHA for {execution_id}: {e}")
        return None


def _start_with_source(target_sha: str | None) -> str:
    """Kick off a fresh pipeline run, pinned to a specific commit when known."""
    kwargs: dict = {"name": PIPELINE}
    if target_sha:
        kwargs["sourceRevisions"] = [
            {"actionName": "Source", "revisionType": "COMMIT_ID", "revisionValue": target_sha}
        ]
    resp = cp.start_pipeline_execution(**kwargs)
    return resp.get("pipelineExecutionId", "?")


def execute_rollback(target_execution_id: str | None) -> str:
    # Resolve the commit SHA the operator wants us to redeploy. If we have it,
    # the fallback path becomes a genuine rollback (re-deploy that commit) rather
    # than a no-op re-run of current HEAD.
    target_sha = _commit_sha_for_execution(target_execution_id) if target_execution_id else None

    if not target_execution_id:
        try:
            new_id = _start_with_source(None)
            return f"⚠️ No specific target execution provided; kicked off a fresh pipeline run `{new_id}` from current HEAD."
        except Exception as e:
            return f"❌ start_pipeline_execution failed: {e}"

    # Try the native CodePipeline V2 rollback first — only works when the target
    # is the immediately-prior *forward* Succeeded execution of the Deploy stage.
    try:
        resp = cp.rollback_stage(
            pipelineName=PIPELINE,
            stageName="Deploy",
            targetPipelineExecutionId=target_execution_id,
        )
        return f"✅ Rolled back Deploy stage to execution `{target_execution_id}` (rollback id `{resp.get('pipelineExecutionId', '?')}`)."
    except Exception as e:
        # Fallback: re-run the pipeline pinned to the target execution's commit SHA.
        try:
            new_id = _start_with_source(target_sha)
            if target_sha:
                return (
                    f"⚠️ rollback_stage rejected ({type(e).__name__}); "
                    f"started a new pipeline run `{new_id}` pinned to commit `{target_sha[:12]}` from `{target_execution_id}`."
                )
            return (
                f"⚠️ rollback_stage rejected ({type(e).__name__}) and could not resolve target commit SHA; "
                f"fell back to a fresh pipeline run `{new_id}` from current HEAD."
            )
        except Exception as e2:
            return f"❌ rollback_stage failed: {e}; fallback also failed: {e2}"


def handler(event, context):
    headers = {(k or "").lower(): v for k, v in (event.get("headers") or {}).items()}
    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    if not verify_slack(headers, raw_body):
        LOG.warning("Slack signature verification failed")
        return {"statusCode": 401, "body": "invalid signature"}

    parsed = urllib.parse.parse_qs(raw_body, keep_blank_values=True)
    payload_json = parsed.get("payload", [""])[0]
    if not payload_json:
        return {"statusCode": 400, "body": "no payload"}
    payload = json.loads(payload_json)

    actions = payload.get("actions") or []
    response_url = payload.get("response_url", "")
    user = payload.get("user", {}).get("name") or payload.get("user", {}).get("id", "?")
    if not actions:
        return {"statusCode": 200, "body": ""}

    action = actions[0]
    action_id = action.get("action_id", "")
    raw_value = action.get("value", "") or ""
    try:
        ctx = json.loads(raw_value) if raw_value else {}
    except Exception:
        ctx = {"raw": raw_value}

    LOG.info(f"user={user} action_id={action_id} ctx={ctx}")

    if action_id == "option_a":
        msg = (
            f"✅ <@{user}> chose *Option A — Trust auto-rollback*. "
            "No action taken; CodePipeline's `OnFailure: ROLLBACK` already restored the previous good revision."
        )
    elif action_id == "option_b":
        result = execute_rollback(ctx.get("rollback_target_execution_id"))
        msg = f":wrench: <@{user}> chose *Option B — Apply fix via CodePipeline rollback*.\n{result}"
    elif action_id == "option_c":
        sha = ctx.get("broken_sha", "?")
        kiro = ctx.get("kiro_message") or f"@kiro investigate commit {sha} and open a fix PR"
        msg = (
            f":rocket: <@{user}> chose *Option C — Delegate to Kiro*.\n"
            f"Posting handoff:\n```{kiro}```"
        )
    else:
        msg = f"Unknown action `{action_id}` from <@{user}>"

    respond_in_channel(response_url, msg)
    return {"statusCode": 200, "body": ""}
