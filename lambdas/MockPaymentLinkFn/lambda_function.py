import json, uuid

def lambda_handler(event, context):
    try:
        body = json.loads(event.get('body') or '{}')
        provider = body.get('provider','UNKNOWN')
        amount = float(body.get('amount', 0))
        currency = body.get('currency','USD')
        ref = str(uuid.uuid4())[:8]
        url = f"https://pay.example/tx/{ref}"
        return {
            "statusCode":200,
            "headers":{"content-type":"application/json"},
            "body": json.dumps({"url":url,"reference":ref,"provider":provider,"amount":amount,"currency":currency})
        }
    except Exception as e:
        return {"statusCode":500,"body":json.dumps({"error": str(e)})}
