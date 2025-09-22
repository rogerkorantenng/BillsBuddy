import json
from datetime import datetime, timedelta, timezone
import boto3

ddb = boto3.resource('dynamodb')

# Your Dynamo DB created
table = ddb.Table('YOUR_DYNAMODB_TABLE')

ISO = "%Y-%m-%dT%H:%M:%SZ"

def lambda_handler(event, context):
    try:
        body = json.loads(event.get('body') or '{}')
        due_date = body.get('due_date')
        user_id  = body.get('userId') or 'demo'
        bill_id  = body.get('billId') or 'bill-001'
        offsets  = body.get('offsets_days', [7,3,0])
        hour     = int(body.get('hour', 9))

        if not due_date:
            return {"statusCode":400,"body":json.dumps({"error":"due_date required"})}

        if 'T' in due_date:
            dt = datetime.fromisoformat(due_date.replace('Z','+00:00')).astimezone(timezone.utc)
        else:
            y,m,d = [int(x) for x in due_date.split('-')]
            dt = datetime(y,m,d,hour,0,0, tzinfo=timezone.utc)

        plan = []
        for off in offsets:
            off = int(off)
            when = dt - timedelta(days=off)
            plan.append({
                "when": when.strftime(ISO),
                "type": "due-day" if off==0 else "reminder",
                "offset_days": off
            })

        pk = f"{user_id}#{bill_id}"
        table.put_item(Item={
            "pk": pk, "sk": "v1",
            "dueDate": dt.date().isoformat(),
            "plan": plan
        })

        return {"statusCode":200,"headers":{"content-type":"application/json"},"body":json.dumps({"plan":plan})}
    except Exception as e:
        return {"statusCode":500,"body":json.dumps({"error":str(e)})}
