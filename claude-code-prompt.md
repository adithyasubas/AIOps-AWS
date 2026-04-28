# Claude Code Prompt: AWS CI/CD Chaos Engineering with DevOps Agent

## Project overview

Build a complete, deployable AWS project that demonstrates a self-healing CI/CD pipeline. The architecture uses CodePipeline V2 to deploy a containerised Node.js API on ECS Fargate. AWS Fault Injection Service (FIS) simulates application failures (chaos engineering). CloudWatch detects the failure, and AWS DevOps Agent autonomously investigates, identifies root cause, and presents the human with three options via Slack: (A) keep the auto-rollback only, (B) rollback + manually apply the agent's recommended fix, or (C) rollback + delegate the fix to Kiro for an automated PR.

All infrastructure is defined in AWS CloudFormation (no CDK). The project targets the ap-southeast-2 (Sydney) region for the application and us-east-1 for DevOps Agent (which is only available there but can monitor resources in other regions). Design decisions must align with the AWS Well-Architected Framework's six pillars: operational excellence, security, reliability, performance efficiency, cost optimisation, and sustainability.

---

## Project structure

Create the following directory structure:

```
chaos-cicd-demo/
├── app/
│   ├── server.js
│   ├── package.json
│   ├── Dockerfile
│   └── .dockerignore
├── cloudformation/
│   ├── main.yaml                  # Parent stack (nested stack orchestrator)
│   ├── vpc.yaml                   # VPC, subnets, NAT, route tables
│   ├── ecs.yaml                   # ECR, ECS cluster, task def, service, ALB
│   ├── pipeline.yaml              # CodePipeline V2, CodeBuild, IAM roles
│   ├── monitoring.yaml            # CloudWatch alarms, SNS topic, dashboard
│   ├── chaos.yaml                 # FIS experiment templates and IAM role
│   └── webhook-bridge.yaml        # Lambda function to bridge CW alarms to DevOps Agent
├── lambda/
│   └── webhook-bridge/
│       └── index.py               # Lambda function code for the webhook bridge
├── buildspec.yml                  # CodeBuild build specification
├── scripts/
│   ├── deploy.sh                  # One-command deploy script
│   ├── destroy.sh                 # One-command teardown script
│   ├── run-chaos.sh               # Script to start FIS experiment
│   └── setup-devops-agent.md      # Manual steps for DevOps Agent setup (console only)
├── docs/
│   └── architecture.md            # Architecture documentation
└── README.md                      # Project README with full instructions
```

---

## Phase 1: Application code (app/ directory)

### server.js
Create a minimal Express.js API with these endpoints:
- `GET /` — returns `{ "message": "Chaos CICD Demo API", "version": "1.0.0", "region": "ap-southeast-2" }`
- `GET /health` — returns `{ "status": "healthy", "timestamp": "<ISO timestamp>", "uptime": process.uptime() }`
- `GET /info` — returns `{ "hostname": os.hostname(), "platform": os.platform(), "nodeVersion": process.version }`

The app should:
- Listen on port 3000 (configurable via PORT env var)
- Log all requests to stdout with timestamp (for CloudWatch Logs)
- Handle SIGTERM gracefully for ECS task shutdown
- Use only the `express` dependency — keep it minimal

### package.json
- Name: chaos-cicd-demo
- Version: 1.0.0
- Node engine: >=18.0.0
- Scripts: start, test (a simple health check curl or a basic jest test)
- Only dependency: express

### Dockerfile
- Base image: node:18-alpine
- Multi-stage build is NOT needed (the app is tiny)
- WORKDIR /app
- Copy package*.json first, run `npm ci --only=production`
- Copy remaining files
- EXPOSE 3000
- Add a HEALTHCHECK using wget against localhost:3000/health
- CMD ["node", "server.js"]
- Run as non-root user (add a `node` user)

### .dockerignore
Include: node_modules, .git, Dockerfile, .dockerignore, *.md, .env

---

## Phase 2: CloudFormation templates (cloudformation/ directory)

### CRITICAL CloudFormation rules:
- All templates must use AWSTemplateFormatVersion: "2010-09-09"
- Use Description fields on every template
- Use Parameters with sensible defaults where appropriate
- Use Outputs to export values needed by other stacks
- Use Conditions where applicable (e.g., optional NAT gateway)
- Tag ALL resources with: Project=chaos-cicd-demo, Environment=demo, ManagedBy=CloudFormation
- Follow least-privilege IAM throughout — never use Action: "*" or Resource: "*"
- Add ChaosReady=true tag to ECS resources that FIS will target

### main.yaml (Parent stack)
- Orchestrates all nested stacks
- Parameters:
  - GitHubOwner (string, no default — user must provide)
  - GitHubRepo (string, default: chaos-cicd-demo)
  - GitHubBranch (string, default: main)
  - GitHubConnectionArn (string, no default — user must create CodeStar connection first)
  - EnvironmentName (string, default: chaos-demo)
  - EnableNatGateway (string, AllowedValues: "true"/"false", default: "false") — cost optimisation toggle
- Nested stack resources pointing to each child template
- Pass outputs between stacks via !GetAtt NestedStack.Outputs.OutputName
- NOTE: Nested stacks require templates to be in S3. Include instructions in README for packaging with `aws cloudformation package`

### vpc.yaml
- VPC with CIDR 10.0.0.0/16
- 2 public subnets (10.0.1.0/24, 10.0.2.0/24) in ap-southeast-2a and ap-southeast-2b
- 2 private subnets (10.0.3.0/24, 10.0.4.0/24) in ap-southeast-2a and ap-southeast-2b
- Internet Gateway attached to VPC
- Conditional NAT Gateway (only if EnableNatGateway=true) in one AZ for cost saving
  - If NAT is disabled, Fargate tasks go in PUBLIC subnets with auto-assign public IP — document this trade-off (less secure but $0 networking cost for demo)
- Route tables for public (route to IGW) and private (route to NAT if enabled)
- VPC Flow Logs to CloudWatch Logs (security pillar)
- Outputs: VpcId, PublicSubnet1Id, PublicSubnet2Id, PrivateSubnet1Id, PrivateSubnet2Id

### ecs.yaml
Parameters: VpcId, SubnetIds (comma-delimited), EnvironmentName
Resources:
- **ECR Repository**:
  - ImageScanningConfiguration enabled (security pillar)
  - LifecyclePolicy: keep only the last 5 images (cost optimisation)
  - Encryption: AES256
- **ECS Cluster**:
  - ContainerInsights enabled
  - Capacity providers: FARGATE and FARGATE_SPOT
- **CloudWatch Log Group** for ECS tasks:
  - Retention: 7 days (cost optimisation)
- **Task Execution Role** (IAM):
  - Allows: ecr:GetDownloadUrlForLayer, ecr:BatchGetImage, ecr:GetAuthorizationToken
  - Allows: logs:CreateLogStream, logs:PutLogEvents
  - Trust: ecs-tasks.amazonaws.com
- **Task Role** (IAM):
  - NO permissions (the app doesn't call AWS APIs)
  - Trust: ecs-tasks.amazonaws.com
- **Task Definition**:
  - Family: {EnvironmentName}-app
  - RequiresCompatibilities: FARGATE
  - NetworkMode: awsvpc
  - Cpu: "256" (0.25 vCPU — minimum for cost)
  - Memory: "512" (0.5 GB — minimum for cost)
  - Container definition:
    - Name: app
    - Image: !Sub "${ECRRepository.RepositoryUri}:latest"
    - PortMappings: containerPort 3000, protocol tcp
    - LogConfiguration: awslogs driver pointing to the log group
    - HealthCheck: CMD-SHELL, wget -qO- http://localhost:3000/health || exit 1
    - Essential: true
  - Tags: ChaosReady=true
- **ALB Security Group**:
  - Inbound: port 80 from 0.0.0.0/0
  - Outbound: all
- **ECS Security Group**:
  - Inbound: port 3000 from ALB security group only
  - Outbound: all
- **Application Load Balancer**:
  - Scheme: internet-facing
  - Subnets: public subnets
  - Security groups: ALB SG
  - Tags: ChaosReady=true
- **ALB Target Group**:
  - TargetType: ip
  - Port: 3000, Protocol: HTTP
  - VpcId: from parameter
  - HealthCheckPath: /health
  - HealthCheckIntervalSeconds: 15
  - HealthyThresholdCount: 2
  - UnhealthyThresholdCount: 2
  - Tags: ChaosReady=true
- **ALB Listener**:
  - Port 80, HTTP
  - DefaultActions: forward to target group
- **ECS Service**:
  - Cluster: ECS cluster
  - LaunchType: FARGATE
  - DesiredCount: 2
  - TaskDefinition: task def
  - NetworkConfiguration:
    - Subnets: from parameter
    - SecurityGroups: ECS SG
    - AssignPublicIp: ENABLED (when no NAT gateway)
  - LoadBalancers: container name "app", container port 3000, target group ARN
  - DeploymentConfiguration:
    - MinimumHealthyPercent: 50
    - MaximumPercent: 200
  - DeploymentCircuitBreaker: Enable: true, Rollback: true
  - Tags: ChaosReady=true
  - DependsOn: ALBListener (critical — service must wait for listener)
- Outputs: ECRRepositoryUri, ECSClusterName, ECSClusterArn, ECSServiceName, ECSServiceArn, ALBDnsName, ALBArn, TargetGroupArn, TaskDefinitionArn, ECSLogGroupName

### pipeline.yaml
Parameters: ECRRepositoryUri, ECSClusterName, ECSServiceName, GitHubOwner, GitHubRepo, GitHubBranch, GitHubConnectionArn, EnvironmentName
Resources:
- **S3 Artifact Bucket**:
  - BucketEncryption: AES256
  - LifecycleConfiguration: expire objects after 30 days
  - PublicAccessBlockConfiguration: all blocked
- **CodeBuild IAM Role**:
  - Trust: codebuild.amazonaws.com
  - Policies: ECR push/pull, S3 artifact read/write, CloudWatch Logs, STS (for ECR login)
- **CodeBuild Project**:
  - Environment:
    - ComputeType: BUILD_GENERAL1_SMALL
    - Image: aws/codebuild/amazonlinux2-x86_64-standard:5.0
    - Type: LINUX_CONTAINER
    - PrivilegedMode: true (required for Docker builds)
    - EnvironmentVariables:
      - ECR_URI: the ECR repo URI
      - AWS_DEFAULT_REGION: ap-southeast-2
      - AWS_ACCOUNT_ID: !Ref AWS::AccountId
  - Source: CODEPIPELINE
  - BuildSpec: buildspec.yml
- **CodePipeline IAM Role**:
  - Trust: codepipeline.amazonaws.com
  - Policies: S3 artifact access, CodeBuild start/stop, ECS deploy, CodeStar connections, IAM PassRole
- **CodePipeline** (THIS IS CRITICAL — must be V2 type):
  - PipelineType: V2
  - Stages:
    - **Source** stage:
      - ActionTypeId: Category: Source, Owner: AWS, Provider: CodeStarSourceConnection, Version: "1"
      - Configuration: ConnectionArn, FullRepositoryId (owner/repo), BranchName
      - OutputArtifacts: SourceOutput
    - **Build** stage:
      - ActionTypeId: Category: Build, Owner: AWS, Provider: CodeBuild, Version: "1"
      - InputArtifacts: SourceOutput
      - OutputArtifacts: BuildOutput
    - **Deploy** stage:
      - ActionTypeId: Category: Deploy, Owner: AWS, Provider: ECS, Version: "1"
      - Configuration: ClusterName, ServiceName, FileName: imagedefinitions.json
      - InputArtifacts: BuildOutput
      - **OnFailure:**
        - **Result: ROLLBACK**  ← This single line enables automatic rollback on deploy failure
  - ArtifactStore: S3 bucket
- Outputs: PipelineName, PipelineArn, ArtifactBucketName

### monitoring.yaml
Parameters: ECSClusterName, ECSServiceName, ALBArn, TargetGroupArn, ECSLogGroupName, EnvironmentName
Resources:
- **SNS Topic** for alarm notifications:
  - TopicName: {EnvironmentName}-alarms
- **CloudWatch Alarm — UnhealthyHosts**:
  - Namespace: AWS/ApplicationELB
  - MetricName: UnHealthyHostCount
  - Dimensions: TargetGroup (extract from ARN), LoadBalancer (extract from ARN)
  - Statistic: Maximum
  - Period: 60
  - EvaluationPeriods: 1
  - Threshold: 1
  - ComparisonOperator: GreaterThanOrEqualToThreshold
  - AlarmActions: SNS topic ARN
  - TreatMissingData: notBreaching
- **CloudWatch Alarm — High5xxErrors**:
  - Namespace: AWS/ApplicationELB
  - MetricName: HTTPCode_Target_5XX_Count
  - Statistic: Sum
  - Period: 60
  - EvaluationPeriods: 1
  - Threshold: 10
  - ComparisonOperator: GreaterThanOrEqualToThreshold
  - AlarmActions: SNS topic ARN
  - TreatMissingData: notBreaching
- **CloudWatch Alarm — HighCPU**:
  - Namespace: AWS/ECS
  - MetricName: CPUUtilization
  - Dimensions: ClusterName, ServiceName
  - Statistic: Average
  - Period: 60
  - EvaluationPeriods: 2
  - Threshold: 80
  - ComparisonOperator: GreaterThanOrEqualToThreshold
  - AlarmActions: SNS topic ARN
- **CloudWatch Alarm — TaskCountDrop**:
  - Namespace: ECS/ContainerInsights
  - MetricName: RunningTaskCount
  - Dimensions: ClusterName, ServiceName
  - Statistic: Minimum
  - Period: 60
  - EvaluationPeriods: 1
  - Threshold: 2
  - ComparisonOperator: LessThanThreshold
  - AlarmActions: SNS topic ARN
- **CloudWatch Dashboard** (optional but impressive):
  - Widgets showing ALB request count, 5xx errors, ECS CPU/memory, task count, alarm states
- Outputs: SNSTopicArn, DashboardName

### chaos.yaml
Parameters: ECSClusterArn, ECSServiceArn, EnvironmentName
Resources:
- **FIS IAM Role**:
  - Trust: fis.amazonaws.com
  - Policies (least privilege):
    - ecs:StopTask, ecs:DescribeTasks (for stop-task experiments)
    - ecs:ListTasks (to find targets)
    - Tag condition: aws:ResourceTag/ChaosReady = "true"
- **FIS Experiment Template — Stop ECS Tasks**:
  - Description: "Chaos: Stop all ECS tasks to simulate application crash"
  - Actions:
    - Name: StopTasks
    - ActionId: aws:ecs:stop-task
    - Parameters: {}
    - Targets: ecsTargets
  - Targets:
    - ecsTargets:
      - ResourceType: aws:ecs:task
      - ResourceTags: ChaosReady: "true"
      - SelectionMode: ALL
      - Parameters:
        - cluster: ECS cluster ARN
  - StopConditions:
    - Source: none
  - RoleArn: FIS role ARN
  - Tags: Project: chaos-cicd-demo, ExperimentType: task-stop
- **FIS Experiment Template — CPU Stress** (if supported for Fargate):
  - Description: "Chaos: CPU stress on ECS tasks"
  - ActionId: aws:ecs:task-cpu-stress
  - Duration: PT5M (5 minutes)
  - Parameters: Percent: "90"
  - Same target config as above
- Outputs: StopTasksExperimentId, CpuStressExperimentId

### webhook-bridge.yaml
Parameters: SNSTopicArn, EnvironmentName
Resources:
- **SSM Parameters** (SecureString):
  - /chaos-demo/devops-agent/webhook-url (value to be manually set after DevOps Agent setup)
  - /chaos-demo/devops-agent/webhook-secret (value to be manually set)
- **Lambda Execution Role**:
  - Trust: lambda.amazonaws.com
  - Policies:
    - SSM GetParameter for the two parameters above
    - CloudWatch Logs for Lambda logging
- **Lambda Function**:
  - Runtime: python3.12
  - Handler: index.handler
  - Timeout: 30
  - Code: see lambda/webhook-bridge/index.py specification below
  - Environment variables:
    - WEBHOOK_URL_PARAM: /chaos-demo/devops-agent/webhook-url
    - WEBHOOK_SECRET_PARAM: /chaos-demo/devops-agent/webhook-secret
- **SNS Subscription**:
  - Protocol: lambda
  - TopicArn: SNS topic
  - Endpoint: Lambda function ARN
- **Lambda Permission**:
  - Allow SNS to invoke the Lambda function
- Outputs: WebhookBridgeFunctionArn

---

## Phase 3: Lambda function (lambda/webhook-bridge/index.py)

Write a Python Lambda handler that:
1. Receives SNS events (the event structure is `event['Records'][0]['Sns']['Message']`)
2. Parses the CloudWatch Alarm JSON from the SNS message
3. Reads the DevOps Agent webhook URL and HMAC secret from SSM Parameter Store (cache these in the global scope to avoid repeated SSM calls on warm starts)
4. Constructs a payload in the DevOps Agent Generic Webhook format:
   ```json
   {
     "source": "cloudwatch-alarm",
     "title": "<alarm name> triggered",
     "description": "<alarm description and state reason>",
     "severity": "HIGH",
     "timestamp": "<ISO timestamp>",
     "metadata": {
       "alarmName": "<name>",
       "newState": "ALARM",
       "reason": "<state reason>",
       "region": "ap-southeast-2",
       "accountId": "<account ID>"
     }
   }
   ```
5. Signs the payload with HMAC-SHA256 using the secret
6. POSTs to the webhook URL with headers:
   - Content-Type: application/json
   - X-Signature: sha256=<hex digest>
7. Logs the response status code
8. Handles errors gracefully (log and don't throw — we don't want SNS retries flooding the agent)

Use only standard library modules: json, os, hashlib, hmac, urllib.request, logging, boto3.

---

## Phase 4: Build specification (buildspec.yml)

Create a CodeBuild buildspec that:
- Installs phase: nothing needed (Docker is available in the standard image)
- Pre-build phase:
  - Logs in to ECR: `aws ecr get-login-password | docker login --username AWS --password-stdin $ECR_URI`
  - Sets IMAGE_TAG to `$CODEBUILD_RESOLVED_SOURCE_VERSION` (the git commit SHA)
- Build phase:
  - Builds the Docker image: `docker build -t $ECR_URI:$IMAGE_TAG app/`
  - Tags as latest: `docker tag $ECR_URI:$IMAGE_TAG $ECR_URI:latest`
- Post-build phase:
  - Pushes both tags to ECR
  - Creates imagedefinitions.json: `[{"name":"app","imageUri":"$ECR_URI:$IMAGE_TAG"}]`
- Artifacts: imagedefinitions.json

---

## Phase 5: Helper scripts (scripts/ directory)

### deploy.sh
A bash script that:
1. Sets variables: STACK_NAME=chaos-cicd-demo, REGION=ap-southeast-2, S3_BUCKET for templates
2. Checks AWS CLI is installed and configured
3. Creates the S3 bucket for CloudFormation templates if it doesn't exist
4. Packages the templates: `aws cloudformation package --template-file cloudformation/main.yaml --s3-bucket $S3_BUCKET --output-template-file packaged.yaml`
5. Deploys: `aws cloudformation deploy --template-file packaged.yaml --stack-name $STACK_NAME --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND --parameter-overrides GitHubOwner=<param> GitHubRepo=<param> GitHubConnectionArn=<param> --region $REGION`
6. Waits for stack completion
7. Prints the ALB DNS name from stack outputs
8. Reminds user to: (a) push app code to GitHub to trigger pipeline, (b) set up DevOps Agent manually

### destroy.sh
A bash script that:
1. Empties the S3 artifact bucket (CloudFormation can't delete non-empty buckets)
2. Deletes all ECR images (CloudFormation can't delete repos with images)
3. Deletes the CloudFormation stack: `aws cloudformation delete-stack --stack-name $STACK_NAME --region $REGION`
4. Waits for deletion
5. Reminds user to manually delete: DevOps Agent Space, FIS experiments if orphaned, CloudWatch log groups

### run-chaos.sh
A bash script that:
1. Retrieves the FIS experiment template ID from CloudFormation stack outputs
2. Starts the experiment: `aws fis start-experiment --experiment-template-id $TEMPLATE_ID --region $REGION`
3. Prints the experiment ID and a message: "Chaos experiment started. Monitor CloudWatch alarms and DevOps Agent for the investigation."

### setup-devops-agent.md
A markdown guide with MANUAL steps (DevOps Agent cannot be provisioned via CloudFormation):
1. Navigate to AWS DevOps Agent console in us-east-1
2. Create Agent Space named "chaos-cicd-demo"
3. Configure IAM role with AIOpsAssistantPolicy + CloudWatch + ECS read permissions
4. Trust policy must include: aws:SourceAccount condition and aws:SourceArn condition (NOT wildcards — wildcards cause 400 errors)
5. Wait 2-3 minutes for IAM propagation and topology discovery
6. Connect GitHub integration (OAuth, read-only, associate the repo)
7. Connect Slack integration (select channel for notifications)
8. Generate webhook under Capabilities > Webhooks (HMAC Authentication)
9. Store webhook URL and secret in SSM Parameter Store:
   ```bash
   aws ssm put-parameter --name "/chaos-demo/devops-agent/webhook-url" --value "<WEBHOOK_URL>" --type SecureString --region ap-southeast-2
   aws ssm put-parameter --name "/chaos-demo/devops-agent/webhook-secret" --value "<WEBHOOK_SECRET>" --type SecureString --region ap-southeast-2
   ```
10. Verify topology shows ECS cluster, ALB, and tasks

---

## Phase 6: Documentation

### README.md
Write a comprehensive README with:
- Project title and one-line description
- Architecture diagram (ASCII art showing the flow: GitHub → CodePipeline → CodeBuild → ECR → ECS Fargate → ALB, with FIS → CloudWatch → Lambda → DevOps Agent → Slack branch)
- Prerequisites list (AWS account, CLI, Docker, GitHub, Slack)
- Step-by-step deployment instructions:
  1. Create a CodeStar Connection to GitHub (console — cannot be automated)
  2. Clone the repo and configure parameters
  3. Run deploy.sh
  4. Push app code to trigger first pipeline run
  5. Set up DevOps Agent (link to setup-devops-agent.md)
  6. Run chaos experiment
  7. Observe the full chain
- Cost estimate table (same as in the Word document I created earlier)
- Well-Architected alignment summary (which pillar each design decision addresses)
- Cleanup instructions (run destroy.sh + manual steps)
- Troubleshooting section

### architecture.md
Document the architecture with:
- Component descriptions
- Data flow narrative (step by step what happens from code push to incident resolution)
- Security decisions and rationale
- Cost optimisation decisions and rationale
- The three human approval options (rollback only, rollback + fix, rollback + Kiro)

---

## Important implementation notes

1. **CodePipeline MUST be PipelineType: V2** — V1 does not support the OnFailure/ROLLBACK feature. This is the most critical config in the project.

2. **ECS Service must DependsOn the ALB Listener** — without this, the service tries to register targets before the listener exists and the stack fails.

3. **The CodeStar Connection to GitHub must be created manually in the console BEFORE deploying** — it requires OAuth browser confirmation. Document this clearly in the README.

4. **DevOps Agent is only available in us-east-1** — but it discovers and monitors resources in ap-southeast-2 through cross-region IAM access. The webhook bridge Lambda runs in ap-southeast-2 alongside the application.

5. **FIS experiment targets use resource tags (ChaosReady=true)** — not ARNs. This is more flexible and survives task replacements.

6. **For cost optimisation, default EnableNatGateway to false** — this saves ~$35/month. Fargate tasks will run in public subnets with public IPs instead. Document the security trade-off.

7. **ECR lifecycle policy keeping only 5 images** prevents storage costs from growing.

8. **CloudWatch Log retention of 7 days** prevents log storage costs from growing.

9. **All IAM roles must follow least-privilege** — no wildcards in actions or resources. Use specific ARNs and conditions where possible.

10. **Tag everything** with Project, Environment, and ManagedBy tags for cost tracking and resource identification.

---

## Validation checklist

After creating all files, verify:
- [ ] Every CloudFormation template passes `aws cloudformation validate-template`
- [ ] All cross-stack references (Outputs → Parameters) are consistent
- [ ] IAM policies use least privilege (no Action: "*")
- [ ] All resources are tagged
- [ ] The Lambda function handles errors gracefully
- [ ] The buildspec.yml produces valid imagedefinitions.json
- [ ] deploy.sh and destroy.sh are executable and handle edge cases
- [ ] README provides complete deployment instructions
- [ ] The CodePipeline is explicitly PipelineType: V2
- [ ] The Deploy stage has OnFailure.Result: ROLLBACK configured
