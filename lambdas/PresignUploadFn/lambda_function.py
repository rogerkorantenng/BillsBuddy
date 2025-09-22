# lambda_function.py
import json, boto3, logging, traceback, base64

log = logging.getLogger()
log.setLevel(logging.INFO)

s3 = boto3.client("s3")

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
        # Handle CORS preflight if routed
        method = event.get("requestContext", {}).get("http", {}).get("method")
        if method == "OPTIONS":
            return _resp(204, {})

        body_raw = event.get("body") or "{}"
        # Some API Gateway configs base64-encode body
        if isinstance(body_raw, str) and event.get("isBase64Encoded"):
            try:
                body_raw = base64.b64decode(body_raw).decode("utf-8", "ignore")
            except Exception:
                pass

        body = json.loads(body_raw) if isinstance(body_raw, str) else (body_raw or {})
        bucket = body.get("bucket")
        key = body.get("key")
        content_type = body.get("content_type") or "application/octet-stream"

        if not bucket or not key:
            return _resp(400, {"error": "bucket and key required"})

        if ".." in key or not key.strip():
            return _resp(400, {"error": "invalid key"})

        url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=3600,
        )
        return _resp(200, {"url": url})

    except Exception as e:
        log.error("Presign error: %s", e)
        log.error("Event snippet: %s", json.dumps({k: event.get(k) for k in ['version','routeKey','rawPath','isBase64Encoded']}, default=str))
        log.error(traceback.format_exc())
        return _resp(500, {"error": str(e)})
