import os, json, boto3, logging, traceback

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION = "YOUR_AWS_REGION"

# Your Bedrock Agent ID
AGENT_ID = "YOUR_BEDROCK_AGENT_ID"
AGENT_ALIAS_ID = "YOUR_BEDROCK_AGENT_ALIAS_ID"

br = boto3.client("bedrock-agent-runtime", region_name=REGION)

def _resp(code, obj):
    return {
        "statusCode": code,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
            "access-control-allow-headers": "content-type,authorization",
            "access-control-allow-methods": "POST,OPTIONS",
        },
        "body": json.dumps(obj),
    }

def lambda_handler(event, context):
    try:
        method = event.get("requestContext", {}).get("http", {}).get("method")
        if method == "OPTIONS":
            return _resp(204, {})

        body_raw = event.get("body") or "{}"
        body = json.loads(body_raw) if isinstance(body_raw, str) else (body_raw or {})
        message = body.get("message")
        session_id = body.get("sessionId") or context.aws_request_id  # stable enough per call

        if not message:
            return _resp(400, {"error": "message required"})

        resp = br.invoke_agent(
            agentId=AGENT_ID,
            agentAliasId=AGENT_ALIAS_ID,
            sessionId=session_id,
            inputText=message,
            enableTrace=False
        )

        # Collect streamed chunks into one string (simpler for HTTP APIs)
        text = ""
        for ev in resp.get("completion", []):
            if "chunk" in ev:
                text += ev["chunk"]["bytes"].decode("utf-8", "ignore")

        return _resp(200, {"sessionId": session_id, "reply": text})

    except Exception as e:
        log.error("Agent error: %s", e)
        log.error(traceback.format_exc())
        return _resp(500, {"error": str(e)})
