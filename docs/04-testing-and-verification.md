# 04 — Testing and verification

How to fire a real incident, watch the agent reason, and verify the output.

---

## Two testing modes

**Real test** — invoke the broken Lambda enough times to breach the alarm threshold. End-to-end, simulates a real incident.

**Manual test** — force the alarm into the `ALARM` state directly. Faster feedback when iterating on the agent code.

Both flows result in the orchestrator running. Use the real test once at the start to confirm the wiring, then the manual test for everything else.

---

## Set up three terminals

**Terminal 1 — watch the alarm state:**

```bash
watch -n 10 "aws cloudwatch describe-alarms \
  --alarm-names payment-processor-errors \
  --query 'MetricAlarms[0].StateValue'"
```

You should see it transition from `OK` → `ALARM` → `OK` over the course of a test.

**Terminal 2 — tail the orchestrator logs:**

```bash
aws logs tail /aws/lambda/ai-ops-orchestrator --follow
```

This is where you'll see Claude's reasoning in action. Each `Agent calling: ...` line corresponds to one Bedrock turn.

**Terminal 3 — fire errors at the target Lambda:**

```bash
for i in {1..30}; do
  aws lambda invoke --function-name payment-processor /tmp/o.json 2>/dev/null
  echo "Invocation $i done"
done
```

Because the Lambda fails roughly 50% of the time, 30 invocations gives ~15 errors — comfortably above the 3-in-60-seconds threshold.

---

## What success looks like in Terminal 2

A successful agent run produces output like this:

```
Starting agent investigation for payment-processor
Agent calling: fetch_logs({'function_name': 'payment-processor', 'minutes': 30})
Agent calling: fetch_cloudtrail({'minutes': 30})
Agent calling: generate_runbook
Done. INC-1738502345 | P2
```

The number of `Agent calling:` lines varies per incident. **If every run shows the same number of tool calls, the agent isn't actually reasoning** — that's worth investigating.

---

## The fast path — manual alarm trigger

Skip waiting for real errors:

```bash
aws cloudwatch set-alarm-state \
  --alarm-name payment-processor-errors \
  --state-value ALARM \
  --state-reason "manual test"
```

This forces the SNS publish without needing real Lambda errors. The orchestrator runs immediately. **Use this for development iteration.**

---

## Verifying the outputs

### Slack

Within ~90 seconds of the alarm firing, a message should arrive in `#ai-ops-alerts` with:

- ✅ A severity emoji (🔴 / 🟠 / 🟡 / 🔵)
- ✅ The incident ID (`INC-<timestamp>`)
- ✅ A root cause line written in plain English
- ✅ A blast radius description
- ✅ Numbered remediation steps with commands

If the message is missing or malformed, check CloudWatch logs for the orchestrator — Slack failures are usually webhook URL or payload issues.

### DynamoDB

```bash
aws dynamodb scan --table-name ai-ops-incidents --limit 5
```

Each item should contain:

- ✅ `incident_id` — `INC-<unix_timestamp>`
- ✅ `severity` — one of P1, P2, P3, P4
- ✅ `root_cause` — populated, not blank
- ✅ `blast_radius` — populated
- ✅ `runbook` — full JSON string with `remediation_steps`
- ✅ `automation_candidates` — a number

### CloudWatch Logs (the agent's "thought process")

In Terminal 2 (or via `aws logs tail` after the fact), look for:

- ✅ `Starting agent investigation for <function_name>`
- ✅ One or more `Agent calling: <tool_name>(<inputs>)` lines
- ✅ Eventually `Agent calling: generate_runbook`
- ✅ `Done. INC-... | <severity>`

---

## Verification checklist

Run through this once after your first end-to-end test:

- [ ] Terminal 2 shows the agent making tool calls
- [ ] Different runs use different numbers of tool calls (the agent is making real decisions)
- [ ] Slack message arrives with severity, root cause, blast radius, steps
- [ ] DynamoDB record contains the full runbook JSON
- [ ] `automation_candidates` count is populated and varies between incidents
- [ ] The orchestrator Lambda's reported duration is well under the 120-second timeout
- [ ] No `Throttling` or `AccessDenied` errors in CloudWatch Logs

---

## Cost expectations during testing

| Service | Cost |
|---|---|
| Lambda invocations | Free tier covers thousands |
| Bedrock (Claude Sonnet 4.6) | ~$0.02 to $0.05 per incident (the agent makes multiple calls) |
| DynamoDB | Free tier |
| SNS | Free tier |
| CloudWatch Logs storage | Negligible during testing |

**50 test incidents will cost less than $3 total** — almost entirely Bedrock spend.

---

## Common issues

**"AccessDeniedException" calling Bedrock**
You haven't requested model access for Claude Sonnet 4.6 in your region. Console → Bedrock → Model access.

**Orchestrator Lambda times out at 120 seconds**
Either the agent is in a tool-call loop (unlikely with `max_iterations=10`) or Bedrock is rate-limiting you. Check CloudWatch Logs for partial output.

**Slack returns 400**
Webhook URL is malformed or the payload is too large. The Slack incoming-webhook payload limit is 40 KB; truncate the runbook if your remediation steps are very long.

**`fetch_logs` returns "No error logs found"**
The error might have logged in a way that doesn't match the filter pattern (`?ERROR ?Exception ?Timeout`). Tighten or relax the pattern in your `fetch_logs` implementation.

**The agent always calls the same tools in the same order**
Two possible causes: (a) your tool descriptions are too prescriptive, removing Claude's room to choose; (b) every test incident is genuinely identical. Vary your test scenarios.

---

[← Back to 03 — Agent design](03-agent-design.md) · [Next → 05 — Use cases and roadmap](05-use-cases-and-roadmap.md)
