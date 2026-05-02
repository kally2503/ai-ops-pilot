# 03 — Agent design

This is the heart of the project. The orchestrator Lambda is small in lines of code but conceptually rich. This document explains what the agent does, how it makes decisions, and how to build it yourself.

> **Note on code:** this doc describes the design in enough detail to implement, without providing the literal source. Building it from this spec is a strong learning exercise in agentic AI design.

---

## What you're building

A single Python Lambda (`ai-ops-orchestrator`) that:

1. Receives an SNS message containing a CloudWatch alarm payload
2. Extracts the failing function name
3. Runs an agentic reasoning loop against Claude on Bedrock
4. Saves the resulting runbook to DynamoDB
5. Posts the runbook to Slack

It uses three AWS clients (`bedrock-runtime`, `logs`, `cloudtrail`) and one outbound HTTP call (Slack).

---

## The Lambda's environment variables

The Lambda needs three environment variables:

| Variable | Value |
|---|---|
| `SLACK_WEBHOOK` | Your Slack incoming webhook URL from Phase 5 |
| `MODEL_ID` | `anthropic.claude-sonnet-4-6` |
| `TABLE` | `ai-ops-incidents` (the DynamoDB table name) |

Configuration also needs:

- **Timeout:** 120 seconds minimum (the agent makes several sequential Bedrock calls)
- **Memory:** 256 MB is plenty (the work is I/O-bound, not CPU)

---

## The IAM policy

The orchestrator role needs permissions for Bedrock, CloudWatch Logs, CloudTrail, and DynamoDB:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "bedrock:InvokeModel",
      "logs:FilterLogEvents",
      "cloudtrail:LookupEvents",
      "dynamodb:PutItem",
      "dynamodb:Scan"
    ],
    "Resource": "*"
  }]
}
```

> **Production hardening:** scope `Resource` down to specific ARNs (your DynamoDB table, your CloudWatch log groups, the specific Bedrock model ARN). The `*` is for development clarity only.

---

## The system prompt

A short prompt that tells Claude its role and the severity scale:

> You are a senior SRE agent triaging a live incident.
> Use tools to investigate. Start with `fetch_logs`, then `fetch_cloudtrail` if needed.
> When you know the root cause, call `generate_runbook`.
> Severity: P1 = outage, P2 = degraded, P3 = elevated errors, P4 = anomaly.

That's the entire prompt. Tool descriptions do most of the heavy lifting; the system prompt just sets context.

---

## The three tools

This is where the agent design really lives. Tool names, descriptions, and input schemas are what guide Claude's decisions.

### Tool 1 — `fetch_logs`

**Purpose:** Pull recent error events from CloudWatch Logs for the failing Lambda.

**Description shown to Claude:** "Fetch recent CloudWatch logs for the affected Lambda. Use this first."

**Inputs:**
- `function_name` (string, required) — name of the Lambda to query
- `minutes` (integer, optional) — how far back to look (default 30)

**Implementation:** call `logs_client.filter_log_events()` with a filter pattern matching `ERROR`, `Exception`, or `Timeout`. Return the matched messages joined by newlines, or "No error logs found." if none.

### Tool 2 — `fetch_cloudtrail`

**Purpose:** Look up recent AWS API events to spot config changes that might have caused the issue.

**Description shown to Claude:** "Fetch recent AWS API events. Use if you suspect a config change caused the errors."

**Inputs:**
- `minutes` (integer, optional) — time window (default 30)

**Implementation:** call `trail_client.lookup_events()` for the time window. For each event, return a line like `<timestamp> <event_name> by <username>`.

The phrase "Use if you suspect" in the description is deliberate — it nudges Claude to call this tool conditionally rather than every time.

### Tool 3 — `generate_runbook`

**Purpose:** Emit the final structured runbook. This is how Claude signals "I'm done investigating".

**Description shown to Claude:** "Emit the final structured runbook when you have identified the root cause."

**Inputs (this schema is the heart of the project):**

```json
{
  "severity": "P1 | P2 | P3 | P4",
  "root_cause": "string",
  "blast_radius": "string",
  "remediation_steps": [
    {
      "step_number": 1,
      "title": "string",
      "command": "string",
      "rollback_command": "string",
      "automation_candidate": true
    }
  ]
}
```

**Implementation:** there is none. The "tool" never executes a function. When Claude calls it, the orchestrator simply takes the input as the final answer and exits the loop.

This is a powerful pattern: **structured output via tool calling.** Instead of asking Claude to return JSON in a free-form response (which is fragile), you define a tool and force the structure.

---

## The reasoning loop

Pseudocode for the orchestrator's main loop:

```
function run_agent(function_name):
    messages = [
        { role: "user",
          content: f"Investigate incident for Lambda: {function_name}. Start now." }
    ]

    repeat up to 10 times:
        response = bedrock.invoke_model(
            model      = "anthropic.claude-sonnet-4-6",
            max_tokens = 4096,
            system     = SRE_PROMPT,
            tools      = [fetch_logs, fetch_cloudtrail, generate_runbook],
            messages   = messages
        )

        append response to messages

        if response.stop_reason == "end_turn":
            return None    # Claude finished without calling generate_runbook — failure case

        for each tool_use in response:
            if tool_use.name == "generate_runbook":
                return tool_use.input    # success — runbook ready

        # Otherwise execute all requested tools and append all results
        tool_results = []
        for each tool_use in response:
            if tool_use.name == "fetch_logs":
                result = run fetch_logs(tool_use.input)
            else if tool_use.name == "fetch_cloudtrail":
                result = run fetch_cloudtrail(tool_use.input)

            append { tool_use_id, content: result } to tool_results

        append { role: "user", content: tool_results } to messages

    return None    # max iterations reached
```

Three things to notice about this loop:

**1. All tool results are sent in one message.** If Claude calls two tools in the same turn, run both and bundle the results back. Don't send them one at a time.

**2. The loop has a hard cap of 10 iterations.** A safety belt against runaway costs. In practice, real incidents resolve in 2–4 iterations.

**3. `generate_runbook` is treated specially.** It exits the loop. Every other tool's result feeds back into the next Bedrock call.

---

## DynamoDB record shape

When the runbook is ready, write a single item:

| Attribute | Type | Source |
|---|---|---|
| `incident_id` | String | `f"INC-{int(time.time())}"` (hash key) |
| `timestamp` | String | UTC ISO 8601 |
| `severity` | String | from runbook |
| `root_cause` | String | from runbook |
| `blast_radius` | String | from runbook |
| `runbook` | String | full JSON of the runbook |
| `automation_candidates` | Number | count of remediation steps where `automation_candidate == true` |

That last field is interesting — it's a forward-looking metric for "how much of this could a future V2 auto-remediate?"

---

## Slack notification format

Compose a single message that captures the essentials. Suggested format:

```
🟠 *[P2] INC-1738502345*
*Root cause:* Database connection timeout — pool exhausted by traffic spike
*Blast radius:* All inbound payment requests, ~12% error rate

*Steps:*
1. Increase DB connection pool to 50
   `aws rds modify-db-parameter-group --parameter-group-name prod-pg --parameters "ParameterName=max_connections,ParameterValue=200,ApplyMethod=immediate"`
2. Restart payment service
   `aws lambda update-function-configuration --function-name payment-processor --environment "Variables={...}"`
```

Map severity to an emoji:

| Severity | Emoji |
|---|---|
| P1 | 🔴 |
| P2 | 🟠 |
| P3 | 🟡 |
| P4 | 🔵 |

POST the message as JSON to the Slack webhook URL with `Content-Type: application/json`.

---

## Why this design works as an agent

Three design choices make this an autonomous agent rather than a workflow:

1. **The tools have intent-revealing descriptions.** Phrases like "Use this first" and "Use if you suspect" steer Claude's tool selection without hardcoding the path.

2. **`generate_runbook` is a tool, not a return type.** Claude decides when it has enough evidence to conclude. We don't tell it.

3. **The loop has no fixed length.** Two tool calls or four, the agent picks. The same architecture handles trivial errors and gnarly cascading failures with no code changes.

That's the agent difference, in design terms.

---

## Deployment commands

After implementing the handler:

```bash
zip handler.zip handler.py

aws iam create-role \
  --role-name ai-ops-orchestrator-role \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Service":"lambda.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }]
  }'

aws iam attach-role-policy \
  --role-name ai-ops-orchestrator-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

aws iam put-role-policy \
  --role-name ai-ops-orchestrator-role \
  --policy-name ai-ops-perms \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Action":[
        "bedrock:InvokeModel",
        "logs:FilterLogEvents",
        "cloudtrail:LookupEvents",
        "dynamodb:PutItem",
        "dynamodb:Scan"
      ],
      "Resource":"*"
    }]
  }'

sleep 10

aws lambda create-function \
  --function-name ai-ops-orchestrator \
  --runtime python3.11 \
  --handler handler.lambda_handler \
  --role arn:aws:iam::<YOUR_ACCOUNT_ID>:role/ai-ops-orchestrator-role \
  --zip-file fileb://handler.zip \
  --timeout 120 \
  --environment "Variables={
    SLACK_WEBHOOK=<YOUR_SLACK_WEBHOOK>,
    MODEL_ID=anthropic.claude-sonnet-4-6,
    TABLE=ai-ops-incidents
  }"
```

Then return to [02 — Phase 7](02-infrastructure-setup.md#phase-7--wire-sns-to-the-orchestrator-lambda) to wire the SNS trigger.

---

[← Back to 02 — Infrastructure setup](02-infrastructure-setup.md) · [Next → 04 — Testing and verification](04-testing-and-verification.md)
