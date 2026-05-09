# ai-ops-pilot

> An agentic AI Ops investigator: a Lambda breaks at 2am, Claude figures out why,
> drafts a runbook, posts it to Slack — with no human in the loop until the answer is ready.

A hands-on learning project built during a career break to deepen agentic-Lambda and
SRE-meets-GenAI patterns. **Lab-grade, not production** — but architected the way the
production version would be built for a regulated environment (see hardening table below).

---

## The problem

A service breaks at 2am. The on-call engineer spends 30–45 minutes pulling logs,
scanning CloudTrail, cross-referencing dashboards, and drafting a Slack update
before they even start fixing the thing. Most of that work is mechanical
investigation, not creative diagnosis.

This pilot collapses that to under 60 seconds and hands the engineer a structured
runbook to execute against.

---

## Architecture

┌────────────────────┐    errors    ┌──────────────────┐   >3 in 60s   ┌──────────┐
│ payment-processor  │ ───────────► │ CloudWatch alarm │ ────────────► │ SNS topic│
│ Lambda (lab)       │              │                  │               │ incidents│
└────────────────────┘              └──────────────────┘               └────┬─────┘
│
┌───────────────────────────────┘
▼
┌──────────────────────────────┐
│ orchestrator Lambda          │
│  ┌────────────────────────┐  │   tool calls
│  │ Claude (agentic loop)  │  │ ◄──────────────┐
│  └────────────────────────┘  │                │
└────────────┬─────────────────┘                │
│                                  ▼
┌───────────────────┼───────────────────┐    ┌──────────────────┐
▼                   ▼                   ▼    │ CloudWatch Logs  │
┌─────────────┐     ┌─────────────┐     ┌──────────┐ │ CloudTrail       │
│ DynamoDB    │     │ Slack       │     │  audit   │ │ Lambda metadata  │
│ (runbooks)  │     │ (notify)    │     │  logs    │ └──────────────────┘
└─────────────┘     └─────────────┘     └──────────┘



---

## Why this is an agent, not a workflow

Claude **decides** which AWS tools to call, in what order, based on what each tool
returns. The orchestrator doesn't hardcode "first fetch logs, then fetch CloudTrail"
— Claude chooses based on the symptoms.

For a known-recurring incident class, a Step Function workflow is cheaper and
faster. For novel incidents the agent's tool flexibility wins. In a production
SRE platform I'd run both — workflow for known classes, agent for everything else.

---

## Stack

- **Compute** — AWS Lambda (Python)
- **Reasoning** — Claude via Anthropic API *(production: Amazon Bedrock with VPC endpoints)*
- **Eventing** — CloudWatch alarm → SNS → Lambda
- **State** — DynamoDB (one runbook record per incident)
- **Notification** — Slack incoming webhook
- **Provisioning** — AWS CLI/console *(roadmap: Terraform module)*

---

## Build phases

1. **Lab service** — payment-processor Lambda that fails 50% of the time + 20-invocation generator
2. **SNS topics** — `ai-ops-incidents` (in) and `ai-ops-notifications` (out)
3. **CloudWatch alarm** — fires when >3 Lambda errors in 60s, wired to SNS
4. **DynamoDB table** — per-incident runbook persistence
5. **Slack incoming webhook** — outbound notification channel
6. **Orchestrator Lambda** — tool definitions, agentic loop, log/CloudTrail fetch, DynamoDB write, Slack post
7. **SNS → orchestrator subscription** — alarms invoke the agent automatically
8. **End-to-end smoke test** — fire errors, watch alarm trip, observe Claude's tool sequence in CloudWatch Logs, verify Slack + DynamoDB record

