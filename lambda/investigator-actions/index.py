"""
Slack interactivity handler.

Receives Block Kit button clicks via API Gateway, verifies the Slack request
signature, and dispatches to one of:
  option_a  -> ack
  option_b  -> CodePipeline rollback (with start-pipeline-execution fallback
               pinned to the target execution's commit SHA)
  option_pr -> apply Claude's find/replace patches on a new branch and open
               a GitHub PR
  option_c  -> post a Kiro handoff message
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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

PIPELINE = os.environ["PIPELINE_NAME"]
SIGNING_SECRET_PARAM = os.environ["SLACK_SIGNING_SECRET_PARAM"]

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "adithyasubas")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "AIOps-AWS")
GITHUB_BASE_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "main")
GITHUB_TOKEN_PARAM = os.environ.get("GITHUB_TOKEN_PARAM_NAME", "")

ssm = boto3.client("ssm")
cp = boto3.client("codepipeline")

_signing_secret: str | None = None
_github_token: str | None = None


# ---- secret cache ----------------------------------------------------------

def get_signing_secret() -> str:
    global _signing_secret
    if _signing_secret:
        return _signing_secret
    _signing_secret = ssm.get_parameter(Name=SIGNING_SECRET_PARAM, WithDecryption=True)["Parameter"]["Value"]
    return _signing_secret


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
        LOG.warning(f"github token unreadable: {e}")
        _github_token = ""
        return None


# ---- Slack ---------------------------------------------------------------

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


def respond_in_channel(response_url: str, text: str) -> None:
    body = json.dumps({"text": text, "response_type": "in_channel", "replace_original": False}).encode()
    req = urllib.request.Request(response_url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            LOG.info(f"response_url POST: {r.status}")
    except urllib.error.HTTPError as e:
        LOG.error(f"response_url HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
    except Exception as e:
        LOG.error(f"response_url failed: {e}")


# ---- rollback -----------------------------------------------------------

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
    if target_execution_id:
        try:
            resp = cp.rollback_stage(
                pipelineName=PIPELINE, stageName="Deploy", targetPipelineExecutionId=target_execution_id
            )
            return f"✅ Rolled back Deploy stage to `{target_execution_id}` (rollback id `{resp.get('pipelineExecutionId', '?')}`)."
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
                    return (
                        f"⚠️ rollback_stage rejected ({type(e).__name__}); "
                        f"started a new pipeline run `{new_id}` pinned to commit `{target_sha[:12]}` from `{target_execution_id}`."
                    )
                return (
                    f"⚠️ rollback_stage rejected ({type(e).__name__}) and could not resolve target commit SHA; "
                    f"fell back to a fresh pipeline run `{new_id}` from current HEAD."
                )
            except Exception as e2:
                return f"❌ rollback failed: {e}; fallback also failed: {e2}"
    try:
        resp = cp.start_pipeline_execution(name=PIPELINE)
        return f"⚠️ No target execution provided; started a fresh pipeline run `{resp.get('pipelineExecutionId', '?')}`."
    except Exception as e:
        return f"❌ start_pipeline_execution failed: {e}"


# ---- GitHub PR creation ------------------------------------------------

_BAD_PATH = re.compile(r"(^/|\.\./|//)")


def _gh(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    token = get_github_token()
    if not token:
        return 0, "no github token configured"
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "chaos-investigator-actions",
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


def create_fix_pr(ctx: dict) -> dict:
    patches = ctx.get("patches") or []
    if not patches:
        return {"ok": False, "error": "no patches in context (Claude did not produce a safe find/replace fix)"}

    sig = (ctx.get("incident_signature") or "incident").replace("sig_", "")[:10]
    branch = f"aiops-fix/{sig}-{int(time.time())}"

    code, ref = _gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/ref/heads/{GITHUB_BASE_BRANCH}")
    if code != 200 or not isinstance(ref, dict):
        return {"ok": False, "error": f"GET ref/heads/{GITHUB_BASE_BRANCH} -> {code}: {ref}"}
    base_sha = ref["object"]["sha"]

    code, _ = _gh(
        "POST",
        f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    if code not in (200, 201):
        return {"ok": False, "error": f"create branch ref -> {code}"}

    applied: list[dict] = []
    for p in patches[:5]:
        path = (p.get("file_path") or "").lstrip("/")
        find = p.get("find") or ""
        replace = p.get("replace") or ""
        if not path or not find or _BAD_PATH.search(path):
            applied.append({"file_path": path, "ok": False, "error": "rejected: empty/invalid path or find"})
            continue

        code, fblob = _gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{urllib.parse.quote(path)}?ref={GITHUB_BASE_BRANCH}")
        if code != 200 or not isinstance(fblob, dict) or "content" not in fblob:
            applied.append({"file_path": path, "ok": False, "error": f"GET contents -> {code}"})
            continue
        try:
            current = base64.b64decode(fblob["content"]).decode("utf-8")
        except Exception as e:
            applied.append({"file_path": path, "ok": False, "error": f"decode failed: {e}"})
            continue
        if find not in current:
            applied.append({"file_path": path, "ok": False, "error": "find text not present (exact match required)"})
            continue
        if replace == find:
            applied.append({"file_path": path, "ok": False, "error": "replace == find (no-op)"})
            continue
        updated = current.replace(find, replace, 1)
        new_b64 = base64.b64encode(updated.encode("utf-8")).decode("ascii")

        code, _ = _gh(
            "PUT",
            f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{urllib.parse.quote(path)}",
            {
                "message": f"AIOps fix: {ctx.get('fix_summary', 'apply suggested patch')}\n\nfile: {path}",
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

    body_lines = [
        f"**Incident summary:** {ctx.get('summary', '(none)')}",
        "",
        f"**Root cause:** {ctx.get('root_cause', '(none)')}",
        f"**Fix summary:** {ctx.get('fix_summary', '(none)')}",
        "",
        f"- Alarm: `{ctx.get('alarm_name', '?')}`",
        f"- Pipeline execution: `{ctx.get('pipeline_execution_id', '?')}`",
        f"- Commit that caused failure: `{(ctx.get('commit_sha') or '?')[:12]}`",
        f"- AI confidence: `{ctx.get('confidence', '?')}`",
        f"- Risk level: `{ctx.get('risk_level', '?')}`",
        "",
        "Generated by the AIOps investigator. Review before merging.",
    ]
    title = f"AIOps fix: {(ctx.get('fix_summary') or 'investigator-suggested patch')[:80]}"
    code, pr = _gh(
        "POST",
        f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/pulls",
        {"title": title, "head": branch, "base": GITHUB_BASE_BRANCH, "body": "\n".join(body_lines)},
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


# ---- handler -----------------------------------------------------------

def handler(event, context):
    headers = {(k or "").lower(): v for k, v in (event.get("headers") or {}).items()}
    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    if not verify_slack(headers, raw_body):
        LOG.warning("slack signature verification failed")
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

    LOG.info(f"user={user} action_id={action_id}")

    if action_id == "option_a":
        msg = (
            f"✅ <@{user}> chose *Option A — Trust auto-rollback*. "
            "No action taken; CodePipeline `OnFailure: ROLLBACK` already restored the previous good revision."
        )
    elif action_id == "option_b":
        result = execute_rollback(ctx.get("rollback_target_execution_id"))
        msg = f":wrench: <@{user}> chose *Option B — Apply fix via CodePipeline rollback*.\n{result}"
    elif action_id == "option_pr":
        result = create_fix_pr(ctx)
        if result.get("ok"):
            applied = ", ".join(f"`{a['file_path']}`" for a in result.get("files", []) if a.get("ok"))
            msg = (
                f":octocat: <@{user}> chose *Option PR — Create GitHub fix PR*.\n"
                f"Opened <{result.get('url')}|PR #{result.get('number')}> on branch `{result.get('branch')}`.\n"
                f"Files: {applied or '(none)'}"
            )
        else:
            details = ""
            if result.get("files"):
                details = "\nPer-file results:\n" + "\n".join(
                    f"- `{f.get('file_path')}`: {'✅' if f.get('ok') else '❌'} {f.get('error', '')}"
                    for f in result["files"]
                )
            msg = f":x: <@{user}> chose *Option PR* but the patch could not be applied: {result.get('error')}{details}"
    elif action_id == "option_c":
        sha = (ctx.get("commit_sha") or "")[:12]
        kiro = ctx.get("kiro_message") or f"@kiro investigate commit {sha} and open a fix PR"
        msg = f":rocket: <@{user}> chose *Option C — Delegate to Kiro*.\n```{kiro}```"
    else:
        msg = f"Unknown action `{action_id}` from <@{user}>"

    respond_in_channel(response_url, msg)
    return {"statusCode": 200, "body": ""}
