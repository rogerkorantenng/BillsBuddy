# app.py
# Gradio front-end for Bills & Subscriptions Buddy (AWS Bedrock + Lambdas)
# - Upload a bill image/PDF (S3 presigned upload) or paste raw text
# - Extract fields via /tools/extract
# - Schedule reminders via /tools/schedule
# - Generate mock payment link via /tools/pay
#
# Configure these environment variables before running:
#   BB_API_BASE   -> e.g., https://abc123.execute-api.us-east-1.amazonaws.com/prod
#   BB_BUCKET     -> uploads bucket name output from CDK (for presigned uploads)
#   BB_REGION     -> e.g., us-east-1
#
# Run:
#   pip install -r requirements.txt
#   python app.py

import os
import io
import json
import time
import hashlib
import mimetypes
import os, mimetypes

from datetime import datetime

import requests
import gradio as gr

API_BASE = "https://2uv2ia4lt1.execute-api.us-east-1.amazonaws.com/prod"
UPLOAD_BUCKET = "billsbuddy-uploads-test"
REGION = "us-east-1"

# --- Helper: presigned URL (inline gateway-less version) ---
# If you didn't create a Lambda for presigning, you can use this simple helper
# that presumes you added an API route /tools/presign in CDK. If not available,
# fallback will upload via your own backend or ask the user to paste text.

PRESIGN_URL = f"{API_BASE}/tools/presign" if API_BASE else None
EXTRACT_URL = f"{API_BASE}/tools/extract" if API_BASE else None
SCHEDULE_URL = f"{API_BASE}/tools/schedule" if API_BASE else None
PAY_URL = f"{API_BASE}/tools/pay" if API_BASE else None


def _pretty_json(o):
    try:
        return json.dumps(o, indent=2, ensure_ascii=False)
    except Exception:
        return str(o)


def _hash_name(name: str) -> str:
    base = os.path.basename(name)
    stamp = int(time.time())
    h = hashlib.sha1(f"{base}-{stamp}".encode()).hexdigest()[:10]
    return f"uploads/{stamp}-{h}-{base}"


def api_health():
    if not API_BASE:
        return False, "Set BB_API_BASE env var first."
    try:
        # lightweight check against extract route (OPTIONS often blocked)
        r = requests.post(EXTRACT_URL, json={"raw_text":"ping"}, timeout=10)
        ok = r.status_code in (200, 400)
        return ok, f"API reachable: {r.status_code}"
    except Exception as e:
        return False, f"API error: {e}"


# --- Optional: presign (requires /tools/presign Lambda). If missing, we show a hint. ---

def presign_upload(key: str, content_type: str):
    if not PRESIGN_URL:
        raise RuntimeError("No presign endpoint configured (BB_API_BASE/tools/presign)")
    r = requests.post(PRESIGN_URL, json={"bucket": UPLOAD_BUCKET, "key": key, "content_type": content_type}, timeout=20)
    r.raise_for_status()
    return r.json()  # {url, fields} (form) OR {url} (PUT)


def upload_bytes_to_presigned(presigned, data: bytes, content_type: str):
    # Support two styles: S3 POST form or single PUT url
    if "fields" in presigned:
        url = presigned["url"]
        fields = presigned["fields"]
        files = {"file": (fields.get("key", "file"), data, content_type)}
        r = requests.post(url, data=fields, files=files, timeout=60)
    else:
        url = presigned["url"]
        r = requests.put(url, data=data, headers={"Content-Type": content_type}, timeout=60)
    if not (200 <= r.status_code < 300):
        raise RuntimeError(f"Upload failed: {r.status_code} {r.text[:200]}")
    return True


# --- Tool calls ---

def call_extract(raw_text: str = None, bucket: str = None, key: str = None):
    payload = {}
    if raw_text:
        payload["raw_text"] = raw_text
    if bucket and key:
        payload.update({"bucket": bucket, "key": key})
    r = requests.post(EXTRACT_URL, json=payload, timeout=60)
    if r.status_code != 200:
        try:
            return None, r.json()
        except Exception:
            return None, {"error": r.text}
    return r.json(), None


def call_schedule(due_date: str, user_id: str, bill_id: str, offsets=(7,3,0), hour=9):
    payload = {
        "due_date": due_date,
        "userId": user_id or "demo",
        "billId": bill_id or "bill-001",
        "offsets_days": list(offsets),
        "hour": hour,
    }
    r = requests.post(SCHEDULE_URL, json=payload, timeout=30)
    if r.status_code != 200:
        try:
            return None, r.json()
        except Exception:
            return None, {"error": r.text}
    return r.json(), None


def call_pay(provider: str, amount: float, currency: str):
    payload = {"provider": provider, "amount": amount, "currency": currency}
    r = requests.post(PAY_URL, json=payload, timeout=20)
    if r.status_code != 200:
        try:
            return None, r.json()
        except Exception:
            return None, {"error": r.text}
    return r.json(), None


# --- Gradio logic ---


def understand_bill(file, text_input):
    if not API_BASE:
        return "", "Configure BB_API_BASE first.", ""

    if file is None and (not text_input or not text_input.strip()):
        return "", "Provide a file or paste bill text.", ""

    # --- FILE PATH MODE (Gradio returns a path-like NamedString) ---
    if file is not None:
        try:
            # file may be a string path or a NamedString wrapper with .name
            filepath = file if isinstance(file, str) else getattr(file, "name", None) or str(file)
            filename = os.path.basename(filepath)
            with open(filepath, "rb") as f:
                content = f.read()

            key = _hash_name(filename)
            ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"

            ps = presign_upload(key, ctype)           # POST {BB_API_BASE}/tools/presign
            upload_bytes_to_presigned(ps, content, ctype)

            data, err = call_extract(bucket=UPLOAD_BUCKET, key=key)
            if err:
                return "", _pretty_json(err), ""
            summary = {
                "provider": data.get("provider"),
                "amount": data.get("amount"),
                "currency": data.get("currency"),
                "due_date": data.get("due_date"),
            }
            return _pretty_json(summary), "", _pretty_json(data)
        except Exception as e:
            return "", f"Upload/extract error: {e}", ""

    # --- RAW TEXT MODE ---
    data, err = call_extract(raw_text=text_input)
    if err:
        return "", _pretty_json(err), ""
    summary = {
        "provider": data.get("provider"),
        "amount": data.get("amount"),
        "currency": data.get("currency"),
        "due_date": data.get("due_date"),
    }
    return _pretty_json(summary), "", _pretty_json(data)



def schedule_plan(due_date, user_id, bill_id, offsets_str, hour):
    if not API_BASE:
        return "Configure BB_API_BASE first."
    try:
        offsets = [int(x.strip()) for x in offsets_str.split(',') if x.strip()]
    except Exception:
        offsets = [7,3,0]
    data, err = call_schedule(due_date, user_id, bill_id, tuple(offsets), int(hour or 9))
    return _pretty_json(err or data)


def make_payment_link(provider, amount, currency):
    if not API_BASE:
        return "Configure BB_API_BASE first."
    try:
        amt = float(amount)
    except Exception:
        return "Amount must be a number"
    data, err = call_pay(provider or "UNKNOWN", amt, currency or "USD")
    return _pretty_json(err or data)


with gr.Blocks(title="Bills Buddy (Gradio)") as demo:
    gr.Markdown("""
    # Bills & Subscriptions Buddy (Gradio)
    Extract bill fields → schedule reminders → get a mock payment link.
    Set env vars **BB_API_BASE**, **BB_BUCKET**, **BB_REGION** before running.
    """)

    with gr.Row():
        ok, msg = api_health()
        gr.Markdown(f"**API status:** {'✅' if ok else '❌'} {msg}")

    with gr.Tab("Understand Bill"):
        with gr.Row():
            file = gr.File(label="Upload bill (image/PDF)")
            text_input = gr.Textbox(label="Or paste bill text", lines=8)
        btn = gr.Button("Understand")
        with gr.Row():
            summary = gr.Textbox(label="Summary (key fields)", lines=6)
            error = gr.Textbox(label="Errors", lines=6)
        full_json = gr.Textbox(label="Full Extracted JSON", lines=12)
        btn.click(understand_bill, inputs=[file, text_input], outputs=[summary, error, full_json])

    with gr.Tab("Schedule Reminders"):
        with gr.Row():
            due_date = gr.Textbox(label="Due date (YYYY-MM-DD)")
            user_id = gr.Textbox(label="User ID", value="demo")
            bill_id = gr.Textbox(label="Bill ID", value="bill-001")
        with gr.Row():
            offsets = gr.Textbox(label="Offsets (days, comma)", value="7,3,0")
            hour = gr.Number(label="Hour (0-23)", value=9, precision=0)
        btn2 = gr.Button("Create Plan")
        plan_out = gr.Textbox(label="Plan JSON", lines=10)
        btn2.click(schedule_plan, inputs=[due_date, user_id, bill_id, offsets, hour], outputs=[plan_out])

    with gr.Tab("Pay (Mock)"):
        with gr.Row():
            provider = gr.Textbox(label="Provider", value="GWCL")
            amount = gr.Textbox(label="Amount", value="120.50")
            currency = gr.Textbox(label="Currency (ISO)", value="GHS")
        btn3 = gr.Button("Get Payment Link")
        pay_out = gr.Textbox(label="Payment", lines=6)
        btn3.click(make_payment_link, inputs=[provider, amount, currency], outputs=[pay_out])

if __name__ == "__main__":
    # For local dev
    demo.launch(server_name="0.0.0.0", server_port=7860)
