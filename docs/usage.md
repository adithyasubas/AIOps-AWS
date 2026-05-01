# Usage

How to drive the demo from the AWS console once the stack is deployed.
The fastest path is the chaos-trigger Lambda; you can run the full
investigation flow with one click.

## Open the chaos-trigger Lambda console

```bash
aws cloudformation describe-stacks --stack-name chaos-cicd-demo \
  --region ap-southeast-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`ChaosTriggerConsoleUrl`].OutputValue' \
  --output text
```

Open the URL. You'll land on the Test tab.

## Save these four test events

Click **Configure new event** for each, paste the JSON, give it the
suggested name, save.

### `clear-memory`

Wipes the DynamoDB incident-memory table. Run this before any demo so
the next investigation is a guaranteed memory miss.

```json
{ "mode": "clear_memory" }
```

### `fire-alarm-fresh`

Publishes a synthetic CloudWatch alarm to the SNS topic. The investigator
calls Bedrock and produces a Slack RCA. ECS is unaffected.

```json
{
  "mode": "fire_alarm",
  "reason": "Threshold Crossed: 1 datapoint [1.0] was less than the threshold (2.0). Application crashed: TypeError: Cannot read properties of undefined."
}
```

### `fire-alarm-repeat`

Same payload as `fire-alarm-fresh`. Run it right after to demonstrate the
memory hit path: Slack message arrives in ~1 second instead of ~15
seconds, with `:brain: Memory hit` instead of `:sparkles: Fresh
investigation`.

### `stop-tasks`

Real chaos. Stops both Fargate tasks. ECS replaces them within ~60-90s,
but during the gap the real `chaos-demo-task-count-drop` alarm trips,
the investigator runs with real container logs in context.

```json
{ "mode": "stop_tasks" }
```

### `break-deploy` (optional)

Pushes a deliberately broken commit to a side branch first, grab its
SHA, paste it here. The pipeline will build, the Deploy stage will fail
on the broken image, and `OnFailure: ROLLBACK` will run. The investigator
fires with the broken commit's diff in Bedrock's context, so its
suggested patches will reference real lines.

```json
{ "mode": "break_deploy", "commit_sha": "<sha>" }
```

## Suggested 3-minute demo

1. Open the chaos-trigger Lambda's Test tab. "We're going to trigger
   chaos with one click."
2. Run **clear-memory**. Returns `{"deleted":[...],"count":N}`. "No
   memories yet."
3. Run **fire-alarm-fresh**. Switch to Slack. Wait ~15s.
   - "Fresh investigation" banner.
   - Confidence circle, risk level, four buttons.
   - `:lock: Auto-remediation not performed` line explains the gate.
4. Run **fire-alarm-repeat**. Slack message arrives in ~1 second.
   - "Memory hit" banner.
   - "Skipped Bedrock call to save cost."
   - Same RCA, no LLM call.
5. (Optional) Run **stop-tasks**. Wait ~60s. Real alarm fires naturally.

## Watching the work

Useful tail commands during a demo:

```bash
# investigator
aws logs tail /aws/lambda/chaos-demo-investigator \
  --region ap-southeast-2 --since 5m --follow

# button-click handler
aws logs tail /aws/lambda/chaos-demo-investigator-actions \
  --region ap-southeast-2 --since 5m --follow

# memory table
aws dynamodb scan --table-name chaos-demo-incident-memory \
  --region ap-southeast-2 \
  --query 'Items[*].{sig:incident_signature.S,success:success_count.N,fix:fix_summary.S}' \
  --output table
```

## Turning auto-remediation on

By default the investigator only posts to Slack and waits for a button
click. To let it act on its own:

```bash
aws lambda update-function-configuration \
  --function-name chaos-demo-investigator --region ap-southeast-2 \
  --environment 'Variables={ ... ,AUTO_REMEDIATE_ENABLED=true,AUTO_REMEDIATE_CONFIDENCE_THRESHOLD=0.7,AUTO_REMEDIATE_REQUIRE_LOW_RISK=false}'
```

Lower the confidence threshold and accept MEDIUM risk only for testing;
keep them strict in production. The Slack message includes the gate
reason on every fire, so you can calibrate based on what you see.

## Resetting the demo state

To re-run the demo cleanly:

1. Run the **clear-memory** test event.
2. If you bumped the service down to 0 with `stop-tasks` and ECS hasn't
   recovered, force a redeploy: `aws ecs update-service
   --cluster chaos-demo-cluster --service chaos-demo-service
   --force-new-deployment --region ap-southeast-2`.
3. Close any test PRs the investigator opened, delete their branches.
