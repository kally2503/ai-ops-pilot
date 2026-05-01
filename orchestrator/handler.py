import json, time, os, urllib.request
from datetime import datetime, timedelta, timezone
import boto3

logs_client  = boto3.client('logs')
trail_client = boto3.client('cloudtrail')
bedrock      = boto3.client('bedrock-runtime', region_name='eu-west-2')
ddb          = boto3.client('dynamodb')

SLACK_WEBHOOK = os.environ['SLACK_WEBHOOK']
MODEL_ID      = os.environ.get('MODEL_ID', 'anthropic.claude-sonnet-4-6')
TABLE         = os.environ.get('TABLE', 'ai-ops-incidents')

SRE_PROMPT = """You are a senior SRE agent triaging a live incident.
Use tools to investigate. Start with fetch_logs, then fetch_cloudtrail if needed.
When you know the root cause, call generate_runbook.
Severity: P1=outage, P2=degraded, P3=elevated errors, P4=anomaly."""

TOOLS = [
    {
        "name": "fetch_logs",
        "description": "Fetch recent CloudWatch logs for the affected Lambda. Use this first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string"},
                "minutes": {"type": "integer"}
            },
            "required": ["function_name"]
        }
    },
    {
        "name": "fetch_cloudtrail",
        "description": "Fetch recent AWS API events. Use if you suspect a config change caused the errors.",
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer"}
            }
        }
    },
    {
        "name": "generate_runbook",
        "description": "Emit the final structured runbook when you have identified the root cause.",
        "input_schema": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["P1","P2","P3","P4"]},
                "root_cause": {"type": "string"},
                "blast_radius": {"type": "string"},
                "remediation_steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_number": {"type": "integer"},
                            "title": {"type": "string"},
                            "command": {"type": "string"},
                            "rollback_command": {"type": "string"},
                            "automation_candidate": {"type": "boolean"}
                        },
                        "required": ["step_number","title","command","automation_candidate"]
                    }
                }
            },
            "required": ["severity","root_cause","blast_radius","remediation_steps"]
        }
    }
]

def fetch_logs(function_name, minutes=30):
    end = int(time.time() * 1000)
    start = end - minutes * 60 * 1000
    try:
        resp = logs_client.filter_log_events(
            logGroupName=f"/aws/lambda/{function_name}",
            startTime=start, endTime=end,
            filterPattern="?ERROR ?Exception ?Timeout",
            limit=100
        )
        events = resp.get('events', [])
        if not events:
            return "No error logs found."
        return "\n".join(e['message'] for e in events)
    except Exception as e:
        return f"Could not fetch logs: {str(e)}"

def fetch_cloudtrail(minutes=30):
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    try:
        resp = trail_client.lookup_events(StartTime=start, EndTime=end, MaxResults=20)
        events = resp.get('Events', [])
        if not events:
            return "No CloudTrail events found."
        return "\n".join(
            f"{e['EventTime'].isoformat()} {e['EventName']} by {e.get('Username','unknown')}"
            for e in events
        )
    except Exception as e:
        return f"Could not fetch CloudTrail: {str(e)}"

def run_agent(fn_name):
    messages = [{
        "role": "user",
        "content": f"Investigate incident for Lambda: {fn_name}. Start now."
    }]

    for _ in range(10):
        response = bedrock.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "system": SRE_PROMPT,
                "tools": TOOLS,
                "messages": messages
            })
        )
        out = json.loads(response['body'].read())
        messages.append({"role": "assistant", "content": out['content']})

        if out['stop_reason'] == 'end_turn':
            return None, messages

        tool_uses = [b for b in out['content'] if b['type'] == 'tool_use']
        if not tool_uses:
            return None, messages

        # Check if runbook is ready
        for t in tool_uses:
            if t['name'] == 'generate_runbook':
                print(f"Agent calling: generate_runbook")
                return t['input'], messages

        # Run ALL tools Claude asked for, collect ALL results
        tool_results = []
        for t in tool_uses:
            print(f"Agent calling: {t['name']}({t['input']})")
            if t['name'] == 'fetch_logs':
                result = fetch_logs(**t['input'])
            elif t['name'] == 'fetch_cloudtrail':
                result = fetch_cloudtrail(**t['input'])
            else:
                result = f"Unknown tool: {t['name']}"

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": t['id'],
                "content": str(result)
            })

        # Send ALL results back in ONE message
        messages.append({"role": "user", "content": tool_results})

    return None, messages

def save(incident_id, runbook):
    ddb.put_item(TableName=TABLE, Item={
        'incident_id': {'S': incident_id},
        'timestamp':   {'S': datetime.now(timezone.utc).isoformat()},
        'severity':    {'S': runbook['severity']},
        'root_cause':  {'S': runbook['root_cause']},
        'blast_radius':{'S': runbook['blast_radius']},
        'runbook':     {'S': json.dumps(runbook)},
        'automation_candidates': {'N': str(
            sum(1 for s in runbook['remediation_steps']
                if s.get('automation_candidate'))
        )}
    })

def notify(incident_id, runbook):
    icon = {"P1":"🔴","P2":"🟠","P3":"🟡","P4":"🔵"}.get(runbook['severity'], "⚪")
    steps = "\n".join(
        f"{s['step_number']}. {s['title']}\n   `{s['command']}`"
        for s in runbook['remediation_steps']
    )
    payload = {"text": (
        f"{icon} *[{runbook['severity']}] {incident_id}*\n"
        f"*Root cause:* {runbook['root_cause']}\n"
        f"*Blast radius:* {runbook['blast_radius']}\n\n"
        f"*Steps:*\n{steps}"
    )}
    req = urllib.request.Request(
        SLACK_WEBHOOK,
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'}
    )
    urllib.request.urlopen(req)

def lambda_handler(event, context):
    alarm   = json.loads(event['Records'][0]['Sns']['Message'])
    fn_name = alarm['Trigger']['Dimensions'][0]['value']
    incident_id = f"INC-{int(time.time())}"

    print(f"Starting agent investigation for {fn_name}")

    runbook, messages = run_agent(fn_name)

    if not runbook:
        print("Agent failed to produce a runbook")
        return {"error": "no runbook produced"}

    save(incident_id, runbook)
    notify(incident_id, runbook)

    print(f"Done. {incident_id} | {runbook['severity']}")
    return {"incident_id": incident_id, "severity": runbook['severity']}
