# 05 — Use cases and roadmap

The same architecture extends well beyond Lambda failures. This document covers six realistic incident types the agent can handle today, plus the V2 enhancements that take it from "cool side project" toward "enterprise-grade".

---

## Real-world use cases

The pattern is the same in every case: a CloudWatch alarm fires, the agent runs its reasoning loop, and a structured runbook lands in Slack. What changes is the **alarm metric**, the **tools the agent has access to**, and the **shape of the runbook**.

### 1. RDS / Aurora connection exhaustion

**What triggers it:** `DatabaseConnections` metric breaches a per-cluster threshold, or applications start logging "too many connections" errors.

**What the agent does:**
- Calls `fetch_logs` to confirm the connection-pool symptoms
- Calls `fetch_cloudtrail` to look for recent deployments that may have changed connection behaviour
- Generates a runbook with steps to increase the pool size and identify the offending caller

### 2. API Gateway 5xx spikes

**What triggers it:** `5XXError` rate on an API Gateway stage rises above baseline.

**What the agent does:**
- Calls `fetch_logs` against the integration target Lambda(s)
- Distinguishes between three root causes: Lambda timeouts, throttling (concurrency limits hit), or upstream dependency failures
- The runbook differs significantly depending on which it was — this is a great showcase for agentic reasoning

### 3. ECS / EKS pod crash loops

**What triggers it:** Container insights metric for restart count, or Kubernetes events flooding into CloudWatch Logs.

**What the agent does:**
- Reads container logs from the most recent restart
- Correlates with recent ConfigMap or image-tag changes via CloudTrail (or via a new `fetch_kubernetes_events` tool)
- Identifies whether the cause is a config error, a bad image, or a downstream dependency

### 4. Unexpected AWS cost spike

**What triggers it:** A CloudWatch billing alarm or a Cost Anomaly Detection finding.

**What the agent does:**
- Calls `fetch_cloudtrail` for resource-creation events in the period before the cost spike
- Identifies the service responsible (e.g. NAT Gateway, EC2, S3, Bedrock itself)
- Generates a cost-attribution runbook with the suspect resources and remediation options

### 5. IAM / security policy misconfiguration

**What triggers it:** A spike in `AccessDenied` errors in CloudTrail or a service-specific failure pattern.

**What the agent does:**
- Identifies the failing principal and the resource being accessed
- Calls `fetch_cloudtrail` to find recent IAM role or policy modifications
- Recommends the minimum policy change to fix the issue (this needs care — see "Limitations" below)

### 6. CloudFront / ALB latency degradation

**What triggers it:** P99 latency metric on a CloudFront distribution or ALB target group breaches threshold.

**What the agent does:**
- Triages whether the cause is origin slowness, CDN config (e.g. cache miss rate), or DNS resolution
- Pulls origin response times if available
- Generates a runbook focused on where the latency is being added

---

## What you need to add for each use case

For each new incident type, you typically need:

1. **A new CloudWatch alarm** wired to the same SNS topic
2. **One or two new tools** giving the agent the right read-only data sources (e.g. `fetch_rds_metrics`, `fetch_eks_events`, `fetch_cost_explorer`)
3. **A possibly tweaked system prompt** giving the agent context about the incident class

The orchestrator code structure stays the same. The reasoning loop is unchanged.

---

## V2 roadmap

These are the natural next steps once the V1 system is running.

### V2.1 — Stateless agent fix (do this first)

The current agent restarts from scratch every time the alarm fires. If the same alarm fires five times in two minutes, you get five parallel runs, five Bedrock invocations, five Slack messages.

**Fix:** before starting a new run, query DynamoDB for active incidents on the same `function_name` within the last 10 minutes. If one exists, return early.

This is ~10 lines of code and pays for itself instantly.

### V2.2 — Tighten IAM `Resource: *`

The development policy uses `Resource: "*"` for clarity. Production should scope each action to the specific ARN it needs:

- `dynamodb:PutItem` → the `ai-ops-incidents` table ARN
- `logs:FilterLogEvents` → the specific log groups the agent reads
- `bedrock:InvokeModel` → the specific model ARN
- `cloudtrail:LookupEvents` → no resource scoping available, leave as `*`

### V2.3 — Human-in-the-loop (HITL)

Right now the agent posts a runbook and stops. The next step is to let a human approve a fix and have the agent execute it.

**How it works:**
- Slack message includes interactive buttons: **Approve remediation** and **Investigate further**
- API Gateway endpoint receives Slack's button-click callback
- The Slack signing secret is verified
- A second Lambda runs the approved remediation command

**Why this matters:** the leap from "AI suggests a fix" to "human clicks one button to apply it" is the moment this stops being a demo and becomes useful.

### V2.4 — RAG over past incidents

The DynamoDB table accumulates a history of every incident. Make it searchable.

**Implementation:** add a fourth tool, `search_past_incidents`, that scans DynamoDB (or, at scale, queries a vector index) for similar previous root causes. The agent calls it when patterns feel familiar.

This adds memory to the system — "we saw this exact incident on March 14, here's what fixed it then."

### V2.5 — Multi-agent collaboration

For complex incidents, split the work across specialised agents:

- **Triage Agent** (the V1 system) — identifies the root cause
- **Security Agent** — checks if the incident is actually a DDoS, injection attempt, or other attack
- **Executive Agent** — translates the technical runbook into a "Business Impact" summary for management or status pages

These run in parallel via Step Functions or a coordinator Lambda. The output is a richer, role-specific set of communications instead of a single technical message.

### V2.6 — Auto-remediation for low-risk incidents

For categories of incidents where the fix is well-understood and reversible (e.g. "scale up the connection pool", "restart the Lambda"), allow the agent to execute directly without human approval.

Use the `automation_candidate` boolean already present in the runbook schema as the gate. Wire approval rules per severity:

| Severity | Default behaviour |
|---|---|
| P1 | Always require human approval |
| P2 | Auto-apply if `automation_candidate == true` and `rollback_command` is defined |
| P3 | Auto-apply with notification |
| P4 | Auto-apply silently |

---

## Limitations to be honest about

This system is genuinely useful, but it's not magic. Three honest caveats:

**1. The agent only sees what its tools see.** If a root cause lives in a system the agent has no tool for (a third-party SaaS, a private network event, a database query plan), it will miss it.

**2. The agent can be confidently wrong.** LLMs hallucinate. The runbook should be treated as a strong starting hypothesis, not gospel — especially for P1 incidents where you don't want to apply the wrong fix at speed.

**3. The agent is stateless across incidents (V1).** It doesn't learn from yesterday's outage unless you build the RAG layer in V2.4. Without that, the same root cause produces a fresh runbook every time.

---

## Closing thoughts

The pattern this project demonstrates — **CloudWatch alarms triggering an LLM with tool access to investigate and propose fixes** — generalises well beyond SRE. The same architecture works for:

- Compliance auditing (alarm = "non-compliant resource detected")
- Cost optimisation (alarm = "underutilised resource", agent investigates and recommends termination)
- Security review (alarm = "unusual API pattern", agent triages whether real threat or benign)

What you're really building is a **template for autonomous AWS investigation agents.** The 2am incident response is just the first concrete instance.

---

[← Back to 04 — Testing and verification](04-testing-and-verification.md) · [Back to README](../README.md)
