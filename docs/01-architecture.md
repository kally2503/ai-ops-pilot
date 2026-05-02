# 01 — Architecture

How the AI Ops Agent works end to end, and why each piece of the AWS stack was chosen.

---

## The full flow

```
                     ┌────────────────────┐
                     │  Production Lambda │
                     │  (errors occur)    │
                     └──────────┬─────────┘
                                │
                                ▼
                     ┌────────────────────┐
                     │   CloudWatch       │
                     │   Metric: Errors   │
                     │   Threshold: >3/60s│
                     └──────────┬─────────┘
                                │
                                ▼
                     ┌────────────────────┐
                     │   SNS Topic        │
                     │   ai-ops-incidents │
                     └──────────┬─────────┘
                                │
                                ▼
                     ┌────────────────────┐
                     │  Orchestrator      │
                     │  Lambda            │
                     │  (the agent)       │
                     └──────────┬─────────┘
                                │
       ┌────────────────────────┼────────────────────────┐
       ▼                        ▼                        ▼
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│ CloudWatch   │         │ CloudTrail   │         │ Amazon       │
│ Logs         │         │ Events       │         │ Bedrock      │
│ (fetch_logs) │         │ (fetch_      │         │ (Claude      │
│              │         │  cloudtrail) │         │  reasoning)  │
└──────────────┘         └──────────────┘         └──────────────┘
                                │
                                ▼
                     ┌────────────────────┐
                     │  Runbook generated │
                     └──────────┬─────────┘
                                │
                ┌───────────────┴───────────────┐
                ▼                               ▼
       ┌────────────────┐              ┌────────────────┐
       │   DynamoDB     │              │  Slack channel │
       │ (history store)│              │  #ai-ops-alerts│
       └────────────────┘              └────────────────┘
```

---

## Component-by-component

### 1. The failing Lambda (the "patient")

A regular production Lambda that throws errors. In the test setup we use a synthetic `payment-processor` Lambda that fails 50% of the time, but the architecture works for any real Lambda function.

CloudWatch automatically captures Lambda errors as a metric — no instrumentation needed.

### 2. CloudWatch alarm (the "trigger")

A metric alarm watches the `Errors` metric for the target Lambda. When the count breaches the threshold (3 errors in 60 seconds in our setup), the alarm transitions to `ALARM` state and publishes to an SNS topic.

**Why a metric alarm and not log-based alerting?** Speed. Metric alarms evaluate every 60 seconds. Log-based alerts (Metric Filters or Logs Insights) add delays of several minutes.

### 3. SNS topic (the "fan-out")

A single SNS topic receives the alarm publication. The topic invokes the orchestrator Lambda asynchronously.

**Why SNS in the middle?** Three reasons:

- **Decoupling** — the alarm doesn't need to know about Lambda; it just publishes
- **Future fan-out** — if you later want a second consumer (e.g. PagerDuty bridge, a logger), you add another subscriber without changing the alarm
- **Retry semantics** — SNS handles retries to Lambda automatically with backoff

### 4. The orchestrator Lambda (the "agent host")

This is where the AI agent runs. The Lambda itself is small — it parses the SNS event to extract the failing function name, then enters the agent's reasoning loop.

**Critical configuration:** the timeout must be at least **120 seconds**. The agent may make 3 to 5 sequential calls to Bedrock, each with several seconds of latency. A standard 3-second Lambda timeout would always fail.

### 5. Amazon Bedrock + Claude Sonnet 4.6 (the "brain")

Bedrock hosts the Claude model. The orchestrator calls Bedrock's `invoke_model` API in a loop — each iteration is one round of "Claude looks at the conversation, picks a tool, gets the tool result, decides what to do next".

**Why Bedrock over the Anthropic API directly?**

- Stays inside the AWS account boundary — no egress for inference traffic
- IAM-based access control instead of API keys
- Spend appears on the AWS bill alongside the rest of the stack

**Why Sonnet 4.6 specifically?** It's the right balance of capability and cost for tool-use agents. Opus 4.7 is overkill for three simple tools; Haiku is too lightweight for nuanced root-cause reasoning.

### 6. CloudWatch Logs and CloudTrail (the "evidence sources")

The agent's two read tools query existing AWS data:

- **CloudWatch Logs** — actual error messages, stack traces, exceptions from the failing Lambda
- **CloudTrail** — recent AWS API events (e.g. who deployed, what config changed, IAM modifications)

The agent typically calls `fetch_logs` first. It only calls `fetch_cloudtrail` if the logs suggest a config or deployment change as the cause.

### 7. DynamoDB (the "incident history")

Each generated runbook is stored as one record in a single-table DynamoDB design with `incident_id` as the hash key. Stored fields include severity, root cause, blast radius, the full runbook JSON, and a count of automation candidates.

**Why DynamoDB and not RDS?** Three reasons:

- Pay-per-request billing — costs nothing when idle
- Schemaless — you can evolve the runbook structure without migrations
- Read patterns are simple key-by-id lookups, which DynamoDB handles trivially

### 8. Slack (the "delivery channel")

The final runbook is posted to a Slack channel via an incoming webhook. The webhook is the simplest possible Slack integration — no app, no OAuth, no bot user.

The Slack message is formatted with a severity emoji, the incident ID, root cause, blast radius, and numbered remediation steps with their commands.

---

## The reasoning loop, in plain English

The orchestrator Lambda does roughly this:

1. Build a starting message: "Investigate this incident for Lambda X."
2. Send to Bedrock with the system prompt and tool definitions.
3. Read the response.
4. If Claude asked to use a tool:
    - Run that tool (call CloudWatch Logs API, CloudTrail API, etc.)
    - Append the tool result to the conversation
    - Loop back to step 2
5. If Claude called `generate_runbook`, take the structured output, save to DynamoDB, post to Slack.
6. Done.

The number of loop iterations varies per incident. There is no hardcoded path. **That is the agent difference.**

---

## What this architecture is *not*

To be clear about scope:

- It is **not** a replacement for human SREs. It triages and proposes fixes; a human still decides whether to execute risky actions.
- It is **not** a self-healing system. It generates runbooks; it does not automatically apply them. (See V2 roadmap for human-in-the-loop remediation.)
- It is **not** a monitoring tool. CloudWatch is still your monitoring layer. The agent is what activates *after* monitoring detects something wrong.

---

[Next → 02 — Infrastructure setup](02-infrastructure-setup.md)
