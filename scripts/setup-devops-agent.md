# DevOps Agent Setup (manual — console only)

AWS DevOps Agent cannot be provisioned via CloudFormation. Complete these
steps **after** `scripts/deploy.sh` has finished and the ECS service is healthy.

## 1. Create the Agent Space

1. Open the AWS console in **us-east-1** (DevOps Agent is only available there;
   it can monitor resources in any region).
2. Navigate to **AWS DevOps Agent** → **Agent Spaces** → **Create space**.
3. Name: `chaos-cicd-demo`.

## 2. Configure the IAM trust + attached policy

The service-linked or custom role must trust DevOps Agent and grant access
to the resources it observes. Use a trust policy with **explicit conditions**
— wildcards on `aws:SourceAccount` / `aws:SourceArn` cause 400 errors.

Trust policy example:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "aiops.amazonaws.com" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "aws:SourceAccount": "<YOUR_ACCOUNT_ID>" },
        "ArnLike": { "aws:SourceArn": "arn:aws:aiops:us-east-1:<YOUR_ACCOUNT_ID>:*" }
      }
    }
  ]
}
```

Attached policy: `AIOpsAssistantPolicy` (AWS managed) + read access to the
ECS cluster, ALB, and CloudWatch resources in `ap-southeast-2`.

Wait **2-3 minutes** after attaching the role for IAM propagation and topology
discovery before continuing.

## 3. Connect GitHub (read-only)

1. In the Agent Space, **Integrations → GitHub → Connect**.
2. OAuth in the browser. Grant read-only.
3. Associate the repo: `<your-username>/AIOps-AWS`.

## 4. Connect Slack

1. **Integrations → Slack → Connect**.
2. Choose the workspace and channel where remediation prompts should appear.

## 5. Generate the webhook (HMAC auth)

1. **Capabilities → Webhooks → Create webhook**.
2. Authentication: **HMAC**.
3. Copy the **URL** and **Secret** that appear after creation.

## 6. Store the webhook URL + secret in SSM (ap-southeast-2)

These two parameters are referenced by the webhook-bridge Lambda. The
CloudFormation template creates them as plain `String` placeholders; you
overwrite them as `SecureString`:

```bash
aws ssm put-parameter \
  --name "/chaos-demo/devops-agent/webhook-url" \
  --value "<WEBHOOK_URL>" \
  --type SecureString \
  --overwrite \
  --region ap-southeast-2

aws ssm put-parameter \
  --name "/chaos-demo/devops-agent/webhook-secret" \
  --value "<WEBHOOK_SECRET>" \
  --type SecureString \
  --overwrite \
  --region ap-southeast-2
```

## 7. Verify the topology

In the DevOps Agent console, confirm the discovered topology shows:
- ECS cluster `chaos-demo-cluster`
- ALB `chaos-demo-alb`
- ECS service `chaos-demo-service` and tagged tasks

## 8. Smoke test

Run `bash scripts/run-chaos.sh stop`. Within ~60 seconds:
1. CloudWatch alarms `chaos-demo-unhealthy-hosts` and `chaos-demo-task-count-drop` breach.
2. SNS triggers the webhook-bridge Lambda.
3. DevOps Agent receives the signed webhook and posts an investigation summary
   to your Slack channel with the three remediation options.

If nothing arrives, check:
- Lambda logs: `/aws/lambda/chaos-demo-webhook-bridge` in ap-southeast-2.
- Verify SSM parameters were overwritten (they default to `PLACEHOLDER_*`).
- Webhook signature: the bridge uses `X-Signature: sha256=<hex>` HMAC-SHA256.
