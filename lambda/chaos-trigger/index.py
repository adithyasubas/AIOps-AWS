"""
Chaos trigger.

Single-purpose Lambda you invoke from the AWS console (Lambda -> Test) to
drive the demo end-to-end. Pick a mode in the test event JSON:

  { "mode": "fire_alarm" }
      Publishes a synthetic CloudWatch alarm payload to the chaos-demo-alarms
      SNS topic. Triggers the investigator without disrupting ECS. Useful
      for showing the investigation pipeline without real downtime. Optional:
        "reason": "<text shown in the Slack RCA>"
        "alarm":  "<alarm name; default chaos-demo-task-count-drop>"

  { "mode": "stop_tasks" }
      Stops all currently-running ECS tasks for the service. ECS replaces
      them within ~60s, but the running count drops below threshold long
      enough to trip the real alarm. End-to-end realistic chaos.

  { "mode": "break_deploy", "commit_sha": "<sha>" }
      Starts a CodePipeline execution pinned to that commit. If the commit
      contains a startup crash, the Deploy stage fails, OnFailure: ROLLBACK
      kicks in, and alarms fire during the gap. Use this for the full
      "agent reads the broken commit and proposes a real fix" demo.

  { "mode": "clear_memory" }
      Empties the DynamoDB incident-memory table so you can re-demo the
      memory miss -> hit cycle from a clean state.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

CLUSTER = os.environ["ECS_CLUSTER"]
SERVICE = os.environ["ECS_SERVICE"]
PIPELINE = os.environ["PIPELINE_NAME"]
TOPIC = os.environ["ALARMS_TOPIC_ARN"]
MEMORY_TABLE = os.environ.get("INCIDENT_MEMORY_TABLE_NAME", "")
ACCOUNT = os.environ.get("AWS_ACCOUNT_ID", "")
REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

ecs = boto3.client("ecs")
sns = boto3.client("sns")
cp = boto3.client("codepipeline")
ddb = boto3.client("dynamodb")


def fire_alarm(reason: str, alarm: str) -> dict:
    msg = {
        "AlarmName": alarm,
        "NewStateValue": "ALARM",
        "NewStateReason": reason,
        "StateChangeTime": datetime.now(timezone.utc).isoformat(),
        "Region": REGION,
        "AWSAccountId": ACCOUNT,
    }
    resp = sns.publish(TopicArn=TOPIC, Subject=f"ALARM: {alarm}", Message=json.dumps(msg))
    return {
        "mode": "fire_alarm",
        "alarm": alarm,
        "reason": reason,
        "sns_message_id": resp["MessageId"],
        "tip": "Watch Slack within ~30s. The first time this signature fires you'll see 'Fresh investigation' (Bedrock call). The second time, 'Memory hit' (Bedrock skipped).",
    }


def stop_tasks() -> dict:
    arns = ecs.list_tasks(cluster=CLUSTER, serviceName=SERVICE, desiredStatus="RUNNING").get("taskArns", [])
    for arn in arns:
        ecs.stop_task(cluster=CLUSTER, task=arn, reason="chaos-demo-trigger Lambda invocation")
    return {
        "mode": "stop_tasks",
        "stopped": [a.split("/")[-1] for a in arns],
        "tip": "Fargate replaces tasks within ~60s. The task-count-drop alarm fires during the gap, the investigator runs, and Slack gets a real RCA.",
    }


def break_deploy(commit_sha: str) -> dict:
    if not commit_sha:
        return {"error": "break_deploy requires a 'commit_sha' field. Push a commit that crashes on startup, then pass its SHA here."}
    resp = cp.start_pipeline_execution(
        name=PIPELINE,
        sourceRevisions=[
            {"actionName": "Source", "revisionType": "COMMIT_ID", "revisionValue": commit_sha}
        ],
    )
    return {
        "mode": "break_deploy",
        "execution_id": resp["pipelineExecutionId"],
        "commit_sha": commit_sha,
        "tip": "Build will succeed but Deploy will fail when ECS tasks crash on startup. CodePipeline rollbacks and the investigator fires with the broken commit's diff in context.",
    }


def clear_memory() -> dict:
    if not MEMORY_TABLE:
        return {"error": "INCIDENT_MEMORY_TABLE_NAME not configured"}
    deleted: list[str] = []
    last_key = None
    while True:
        kw = {"TableName": MEMORY_TABLE, "ProjectionExpression": "incident_signature"}
        if last_key:
            kw["ExclusiveStartKey"] = last_key
        page = ddb.scan(**kw)
        for item in page.get("Items", []):
            sig = item["incident_signature"]["S"]
            ddb.delete_item(TableName=MEMORY_TABLE, Key={"incident_signature": {"S": sig}})
            deleted.append(sig)
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
    return {"mode": "clear_memory", "deleted": deleted, "count": len(deleted)}


def handler(event, context):
    LOG.info(f"event: {json.dumps(event)}")
    mode = (event or {}).get("mode") or "fire_alarm"
    try:
        if mode == "fire_alarm":
            reason = (event or {}).get("reason") or "console chaos trigger: synthetic ALARM with no real outage"
            alarm = (event or {}).get("alarm") or "chaos-demo-task-count-drop"
            result = fire_alarm(reason, alarm)
        elif mode == "stop_tasks":
            result = stop_tasks()
        elif mode == "break_deploy":
            result = break_deploy((event or {}).get("commit_sha", ""))
        elif mode == "clear_memory":
            result = clear_memory()
        else:
            result = {
                "error": f"unknown mode '{mode}'",
                "valid_modes": ["fire_alarm", "stop_tasks", "break_deploy", "clear_memory"],
            }
    except Exception as e:
        LOG.exception("trigger failed")
        result = {"mode": mode, "error": f"{type(e).__name__}: {e}"}
    LOG.info(f"result: {json.dumps(result, default=str)}")
    return result
