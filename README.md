Here's a quick breakdown of each section:
What This Project Does — Sets the problem: service breaks at 2am, engineer wastes 30-45 mins. This automates that to under 60 seconds with no human involvement until the answer is ready.
How It Works — Shows the full flow: Lambda errors → CloudWatch alarm → SNS → Orchestrator Lambda → Claude agent investigates autonomously → posts runbook to Slack.
What You're Building — Two Lambdas: one fake broken service, one AI agent that investigates it.

        Phase 1 — Creates the payment-processor Lambda that randomly fails 50% of the time, then fires 20 invocations to generate real errors.
        Phase 2 — Creates two SNS topics: one to receive alarm triggers (ai-ops-incidents), one for outbound notifications (ai-ops-notifications).
        Phase 3 — Sets up a CloudWatch alarm that fires when >3 Lambda errors occur in 60 seconds, wired to the SNS topic.
        Phase 4 — Creates a DynamoDB table to store the generated runbooks per incident.
        Phase 5 — Sets up a Slack incoming webhook so the agent can post alerts to a channel.
        Phase 6 — Builds the core AI agent (ai-ops-orchestrator): defines the tools Claude can call, the agentic loop, log/CloudTrail fetching, DynamoDB saving, and Slack notification.
        Phase 7 — Wires SNS → Lambda so alarm triggers automatically invoke the orchestrator.
        Phase 8 — End-to-end test: fires errors, watches the alarm trip, observes Claude's tool call sequence in logs, verifies Slack message and DynamoDB record.
Why This Is an Agent — Key distinction: Claude decides which tools to call and in what order at runtime — not hardcoded. That's what makes it an agent vs a workflow.
