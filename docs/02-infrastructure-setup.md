# 02 — Infrastructure setup

The AWS plumbing the agent depends on. Build this first, then deploy the orchestrator (covered in [03 — Agent design](03-agent-design.md)).

> **Throughout this guide:** replace `<YOUR_ACCOUNT_ID>` with your AWS account ID, `<REGION>` with your AWS region (e.g. `eu-west-2`), and `<YOUR_SLACK_WEBHOOK>` with your Slack incoming webhook URL.

---

## Prerequisites

- AWS CLI v2 installed and configured (`aws configure`)
- Python 3.11 or higher
- Bedrock model access enabled for Claude Sonnet 4.6 in your region:
  Console → Bedrock → Model access → Request access for **Claude Sonnet 4.6**

Get your account ID — you'll need it throughout:

```bash
aws sts get-caller-identity --query Account --output text
```

---

## Phase 1 — A target Lambda to monitor

In a real deployment this is whatever Lambda you want to protect. For learning, use a deliberately broken one that fails roughly half the time so you can trigger alarms on demand.

### 1.1 Create the function code

Write a small Python file that randomly raises an exception:

```python
# index.py
import random

def handler(event, context):
    if random.random() < 0.5:
        raise Exception("Database connection timeout")
    return {"statusCode": 200, "body": "ok"}
```

Zip it:

```bash
zip function.zip index.py
```

### 1.2 Create the Lambda execution role

```bash
aws iam create-role \
  --role-name payment-lambda-role \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Service":"lambda.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }]
  }'

aws iam attach-role-policy \
  --role-name payment-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

sleep 10  # let IAM propagate
```

### 1.3 Deploy the Lambda

```bash
aws lambda create-function \
  --function-name payment-processor \
  --runtime python3.11 \
  --handler index.handler \
  --role arn:aws:iam::<YOUR_ACCOUNT_ID>:role/payment-lambda-role \
  --zip-file fileb://function.zip
```

Wait until it's active before invoking:

```bash
aws lambda get-function \
  --function-name payment-processor \
  --query 'Configuration.State'
# Must return "Active"
```

Generate some errors to seed CloudWatch:

```bash
for i in {1..20}; do
  aws lambda invoke --function-name payment-processor /tmp/out.json 2>/dev/null
  echo "Invocation $i done"
done
```

---

## Phase 2 — SNS topics

One topic to receive alarm triggers, one for outbound notifications (the second is optional but useful for future extensions).

```bash
aws sns create-topic --name ai-ops-incidents
aws sns create-topic --name ai-ops-notifications
```

Note the returned ARNs — you'll need them in Phase 3 and Phase 7.

---

## Phase 3 — CloudWatch alarm

This alarm is what wakes the agent up.

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name payment-processor-errors \
  --metric-name Errors \
  --namespace AWS/Lambda \
  --statistic Sum \
  --period 60 \
  --threshold 3 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 1 \
  --dimensions Name=FunctionName,Value=payment-processor \
  --alarm-actions arn:aws:sns:<REGION>:<YOUR_ACCOUNT_ID>:ai-ops-incidents
```

**Why these numbers?** A 60-second period with a threshold of 3 errors triggers on a real spike but ignores single transient failures. Tune for your service.

---

## Phase 4 — DynamoDB table

Stores generated runbooks. One record per incident, keyed on `incident_id`.

```bash
aws dynamodb create-table \
  --table-name ai-ops-incidents \
  --attribute-definitions AttributeName=incident_id,AttributeType=S \
  --key-schema AttributeName=incident_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

`PAY_PER_REQUEST` is the right choice here — runbook writes are sporadic, so on-demand billing keeps costs at near zero when the system is quiet.

---

## Phase 5 — Slack incoming webhook

1. In Slack: **Apps** → **Incoming Webhooks** → **Add to Slack**
2. Pick the channel where alerts should land (suggestion: `#ai-ops-alerts`)
3. Copy the generated webhook URL
4. Treat it as a secret — anyone with the URL can post to your channel

You'll inject this URL as an environment variable into the orchestrator Lambda in [03 — Agent design](03-agent-design.md).

---

## Phase 7 — Wire SNS to the orchestrator Lambda

> Phase 6 (creating the orchestrator Lambda itself) is covered in [03 — Agent design](03-agent-design.md). Once that Lambda exists, run the commands below to wire it to the SNS topic.

Allow SNS to invoke the Lambda:

```bash
aws lambda add-permission \
  --function-name ai-ops-orchestrator \
  --statement-id sns-trigger \
  --action lambda:InvokeFunction \
  --principal sns.amazonaws.com \
  --source-arn arn:aws:sns:<REGION>:<YOUR_ACCOUNT_ID>:ai-ops-incidents
```

Subscribe the Lambda to the SNS topic:

```bash
aws sns subscribe \
  --topic-arn arn:aws:sns:<REGION>:<YOUR_ACCOUNT_ID>:ai-ops-incidents \
  --protocol lambda \
  --notification-endpoint arn:aws:lambda:<REGION>:<YOUR_ACCOUNT_ID>:function:ai-ops-orchestrator
```

After this step, every CloudWatch alarm transition to `ALARM` will automatically invoke the agent.

---

## What you should have at the end of this phase

- ✅ A failing Lambda generating real CloudWatch errors
- ✅ A CloudWatch alarm that fires on those errors
- ✅ Two SNS topics ready to fan out alarms
- ✅ A DynamoDB table waiting for runbook writes
- ✅ A Slack webhook ready to receive POSTs
- ✅ (After Phase 6) The orchestrator Lambda wired to receive SNS triggers

Next, build the agent itself.

---

[← Back to 01 — Architecture](01-architecture.md) · [Next → 03 — Agent design](03-agent-design.md)
