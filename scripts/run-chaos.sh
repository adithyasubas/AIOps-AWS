#!/usr/bin/env bash
# Trigger a chaos event without AWS FIS (which is gated on the AWS Free Plan).
#
# Modes:
#   stop   — stop all running tasks of the ECS service. ECS will replace them, but
#            CloudWatch alarms (UnHealthyHostCount, RunningTaskCount, ALB 5xx) will
#            breach in the meantime. SNS fires the webhook bridge → DevOps Agent.
#   break  — same as stop, but stops only ONE task at a time, repeated 3 times,
#            120 seconds apart, to keep the alarm tripping for longer (more
#            realistic incident shape for the agent to investigate).
#
# Usage: bash scripts/run-chaos.sh [stop|break]    (default: stop)

set -euo pipefail

REGION="${AWS_REGION:-ap-southeast-2}"
CLUSTER="${ECS_CLUSTER:-chaos-demo-cluster}"
SERVICE="${ECS_SERVICE:-chaos-demo-service}"
MODE="${1:-stop}"

list_running_tasks() {
  aws ecs list-tasks --cluster "${CLUSTER}" --service-name "${SERVICE}" \
    --region "${REGION}" --desired-status RUNNING --query 'taskArns' --output text
}

stop_one() {
  local task_arn="$1"
  local short="${task_arn##*/}"
  echo "  stopping ${short}"
  aws ecs stop-task --cluster "${CLUSTER}" --task "${task_arn}" \
    --region "${REGION}" --reason "chaos-demo run-chaos.sh" > /dev/null
}

case "${MODE}" in
  stop)
    echo "Killing all running tasks in ${CLUSTER}/${SERVICE}..."
    TASKS=$(list_running_tasks)
    if [ -z "${TASKS}" ] || [ "${TASKS}" = "None" ]; then
      echo "ERROR: no running tasks found. Is the service scaled to 0?"
      exit 1
    fi
    for t in ${TASKS}; do stop_one "$t"; done
    ;;
  break)
    echo "Stopping one task at a time, 3 times, 120s apart..."
    for i in 1 2 3; do
      TASKS=$(list_running_tasks)
      FIRST=$(echo "${TASKS}" | tr -s '[:space:]' '\n' | head -1)
      if [ -z "${FIRST}" ] || [ "${FIRST}" = "None" ]; then
        echo "  iter ${i}: no running task to kill, skipping"
      else
        echo "  iter ${i}/3:"
        stop_one "${FIRST}"
      fi
      [ "${i}" -lt 3 ] && sleep 120
    done
    ;;
  *)
    echo "Unknown mode '${MODE}'. Use: stop | break"
    exit 1
    ;;
esac

cat <<EOF

Chaos triggered. What to watch:
  - CloudWatch alarms (60s window):
      chaos-demo-unhealthy-hosts        (ALB target unhealthy)
      chaos-demo-task-count-drop        (running < 2)
      chaos-demo-high-5xx               (if requests hit while tasks restart)

  - SNS topic chaos-demo-alarms fans out to:
      Lambda chaos-demo-webhook-bridge → DevOps Agent webhook → Slack

  - ECS will self-heal: Fargate replaces stopped tasks within 30-90s.

Quick checks:
  aws cloudwatch describe-alarms --region ${REGION} --alarm-names chaos-demo-unhealthy-hosts chaos-demo-task-count-drop --query 'MetricAlarms[*].[AlarmName,StateValue]' --output table
  aws logs tail /aws/lambda/chaos-demo-webhook-bridge --region ${REGION} --since 5m --follow
EOF
