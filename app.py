# app.py
# Bills & Subscriptions Buddy (Gradio)
# - Upload bill (image/PDF) → S3 via presign → /tools/extract (OCR+LLM)
# - Schedule reminders via /tools/schedule
# - Get mock payment link via /tools/pay
# - Chat with Bedrock Agent via Lambda endpoint /agent/chat (no local AWS creds)
#   Agent tab supports attaching a file: we upload it to S3 and include an s3:// hint.

import os
import json
import time
import uuid
import hashlib
import mimetypes
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
import gradio as gr

# --- Optional: load .env (if present) ---
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)
except Exception:
    pass

# --- Config (adjust to your API Gateway stage) ---
API_BASE = "https://2uv2ia4lt1.execute-api.us-east-1.amazonaws.com/prod"
UPLOAD_BUCKET = "billsbuddy-uploads-test"
REGION = "us-east-1"

PRESIGN_URL    = f"{API_BASE}/tools/presign" if API_BASE else None
EXTRACT_URL    = f"{API_BASE}/tools/extract"  if API_BASE else None
SCHEDULE_URL   = f"{API_BASE}/tools/schedule" if API_BASE else None
PAY_URL        = f"{API_BASE}/tools/pay"      if API_BASE else None
AGENT_CHAT_URL = f"{API_BASE}/agent/chat"     if API_BASE else None

AGENT_SESSION = str(uuid.uuid4())  # one chat session per app run

# --- Helpers ---

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

SUMMARY_TMPL = (
    """
### 🧾 Bill Summary
- **Provider:** {provider}
- **Amount:** {amount} {currency}
- **Due date:** {due_date}
- **Account #:** {account_number}
- **Period:** {period_start} → {period_end}
- **Penalties:** {penalties}

> {notes}
"""
)

def make_summary_md(data: dict) -> str:
    safe = {
        'provider': data.get('provider') or '—',
        'amount': data.get('amount') if data.get('amount') is not None else '—',
        'currency': data.get('currency') or '—',
        'due_date': data.get('due_date') or '—',
        'account_number': data.get('account_number') or '—',
        'period_start': data.get('period_start') or '—',
        'period_end': data.get('period_end') or '—',
        'penalties': data.get('penalties') if data.get('penalties') is not None else '—',
        'notes': data.get('notes') or '—',
    }
    return SUMMARY_TMPL.format(**safe)

# Health check (use /tools/pay so health works even if extract is WIP)
def api_health():
    if not API_BASE:
        return False, "Set BB_API_BASE env var first."
    try:
        r = requests.post(PAY_URL, json={"provider":"PING","amount":1,"currency":"USD"}, timeout=10)
        return (r.status_code == 200), f"API reachable: {r.status_code}"
    except Exception as e:
        return False, f"API error: {e}"

# Presign/upload helpers
def presign_upload(key: str, content_type: str):
    if not PRESIGN_URL:
        raise RuntimeError("No presign endpoint configured (BB_API_BASE/tools/presign)")
    r = requests.post(
        PRESIGN_URL,
        json={"bucket": UPLOAD_BUCKET, "key": key, "content_type": content_type},
        timeout=20
    )
    if r.status_code != 200:
        # bubble up the lambda's error body so you can see what's wrong
        try:
            detail = r.json()
        except Exception:
            detail = {"raw": r.text}
        raise RuntimeError(f"Presign failed {r.status_code}: {detail}")
    return r.json()


def upload_bytes_to_presigned(presigned, data: bytes, content_type: str):
    if "fields" in presigned:  # POST form style
        url = presigned["url"]
        fields = presigned["fields"]
        files = {"file": (fields.get("key", "file"), data, content_type)}
        r = requests.post(url, data=fields, files=files, timeout=60)
    else:                        # PUT style
        url = presigned["url"]
        r = requests.put(url, data=data, headers={"Content-Type": content_type}, timeout=60)
    if not (200 <= r.status_code < 300):
        raise RuntimeError(f"Upload failed: {r.status_code} {r.text[:200]}")
    return True

# Tool calls
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

# .ics export (calendar event on due date at 09:00 UTC)
def make_ics(provider: str, due_iso_date: str, amount: str, currency: str):
    if not due_iso_date:
        raise ValueError("due date is missing")
    dt = datetime.fromisoformat(due_iso_date)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    start = dt.strftime('%Y%m%dT090000Z')
    end   = dt.strftime('%Y%m%dT093000Z')
    uid = hashlib.md5(f"{provider}-{due_iso_date}-{amount}-{currency}".encode()).hexdigest()[:12]
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//BillsBuddy//EN
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{start}
DTSTART:{start}
DTEND:{end}
SUMMARY:Pay {provider} bill ({amount} {currency})
DESCRIPTION:Auto-generated by BillsBuddy
END:VEVENT
END:VCALENDAR
"""
    fd, path = tempfile.mkstemp(prefix="billsbuddy_", suffix=".ics")
    with os.fdopen(fd, 'w') as f:
        f.write(ics)
    return path

# --- Gradio callbacks ---
HISTORY = []  # session-scoped list of summaries

def understand_bill(file, text_input, state):
    if not API_BASE:
        return "", "Configure BB_API_BASE first.", {}, state

    if file is None and (not text_input or not text_input.strip()):
        return "", "Provide a file or paste bill text.", {}, state

    # File path mode (Gradio returns a path-like object)
    if file is not None:
        try:
            filepath = file if isinstance(file, str) else getattr(file, "name", None) or str(file)
            filename = os.path.basename(filepath)
            with open(filepath, "rb") as f:
                content = f.read()
            key = _hash_name(filename)
            ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            ps = presign_upload(key, ctype)
            upload_bytes_to_presigned(ps, content, ctype)
            data, err = call_extract(bucket=UPLOAD_BUCKET, key=key)
            if err:
                return "", _pretty_json(err), {}, state
        except Exception as e:
            return "", f"Upload/extract error: {e}", {}, state
    else:
        data, err = call_extract(raw_text=text_input)
        if err:
            return "", _pretty_json(err), {}, state

    summary_md = make_summary_md(data)
    state = data
    HISTORY.append({
        "provider": data.get("provider"),
        "due_date": data.get("due_date"),
        "amount": data.get("amount"),
        "currency": data.get("currency"),
    })
    return summary_md, "", data, state

def smart_schedule(state, user_id, bill_id, offsets_str, hour):
    if not state:
        return "Run Understand first."
    due = state.get("due_date")
    if not due:
        return "No due date extracted. Enter it manually on the Schedule tab."
    try:
        offsets = [int(x.strip()) for x in offsets_str.split(',') if x.strip()]
    except Exception:
        offsets = [7,3,0]
    data, err = call_schedule(due, user_id or "demo", bill_id or "bill-001", tuple(offsets), int(hour or 9))
    return _pretty_json(err or data)

def export_ics(state):
    if not state:
        return None
    try:
        path = make_ics(
            provider=state.get("provider") or "Provider",
            due_iso_date=state.get("due_date") or "",
            amount=str(state.get("amount") or ""),
            currency=state.get("currency") or ""
        )
        return path
    except Exception:
        return None

def pay_markdown(provider, amount, currency):
    try:
        amt = float(amount)
    except Exception:
        return "❌ Amount must be a number (e.g., 120.50)."
    data, err = call_pay(provider or "UNKNOWN", amt, currency or "USD")
    if err:
        # <<< fixed: no ```json fence >>>
        return f"❌ Payment API error:\n\n```\n{_pretty_json(err)}\n```"
    url = data.get("url"); ref = data.get("reference")
    return (
        f"**Payment Link:** [{url}]({url})\n\n"
        f"**Reference:** `{ref}`\n\n"
        f"**Amount:** {data.get('amount')} {data.get('currency')}\n\n"
        f"**Provider:** {data.get('provider')}"
    )

# Agent chat via Lambda endpoint (with optional file attachment)

def agent_chat(message: str):
    if not AGENT_CHAT_URL:
        return "Configure API_BASE/agent/chat first."
    try:
        r = requests.post(AGENT_CHAT_URL, json={"message": message, "sessionId": AGENT_SESSION}, timeout=60)
        if r.status_code != 200:
            try:
                err = r.json()
            except Exception:
                err = {"error": r.text}
            # <<< fixed: no ```json fence >>>
            return f"❌ Agent API error:\n\n```\n{_pretty_json(err)}\n```"
        return r.json().get("reply", "(no reply)")
    except Exception as e:
        return f"❌ Agent request failed: {e}"

def agent_chat_with_optional_file(user_msg, file_path):
    """If a file is attached, upload to S3 and append an instruction with s3:// location.
    The agent (via action group) should then call /tools/extract with that bucket+key.
    """
    message = (user_msg or "").strip()
    if file_path:
        try:
            filepath = file_path if isinstance(file_path, str) else getattr(file_path, "name", None) or str(file_path)
            filename = os.path.basename(filepath)
            with open(filepath, "rb") as f:
                content = f.read()
            key = _hash_name(filename)
            ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            ps = presign_upload(key, ctype)
            upload_bytes_to_presigned(ps, content, ctype)
            s3_uri = f"s3://{UPLOAD_BUCKET}/{key}"
            # Nudge the agent to use the extract tool with bucket/key
            hint = (
                f"\n\nAttachment: {s3_uri}\n"
                f"Use the extract tool by calling POST /tools/extract with JSON "
                f"{{\"bucket\":\"{UPLOAD_BUCKET}\",\"key\":\"{key}\"}}."
            )
            message = (message + hint).strip()
        except Exception as e:
            return f"❌ Upload failed: {e}"
    return agent_chat(message)

# --- UI ---
with gr.Blocks(title="Bills Buddy (Gradio)") as demo:
    gr.Markdown(
        """
        # Bills & Subscriptions Buddy
        Extract bill fields → schedule reminders → get a mock payment link.
        Polished UI: summary card, JSON viewer, Smart Schedule, calendar export, and Agent chat.
        """
    )

    with gr.Row():
        ok, msg = api_health()
        gr.Markdown(f"**API status:** {'✅' if ok else '❌'} {msg}")

    state = gr.State({})

    with gr.Tab("Understand Bill"):
        with gr.Row():
            with gr.Column(scale=1):
                file = gr.File(label="Upload bill (image/PDF)", type="filepath")
                text_input = gr.Textbox(label="Or paste bill text", lines=8)
                btn = gr.Button("Understand", variant="primary")
            with gr.Column(scale=1):
                summary_md = gr.Markdown(value="")
                error_md = gr.Markdown(value="")
        with gr.Accordion("Full extracted JSON", open=False):
            full_json = gr.JSON(value={})
        btn.click(understand_bill, inputs=[file, text_input, state], outputs=[summary_md, error_md, full_json, state])

    with gr.Tab("Schedule & Calendar"):
        with gr.Row():
            user_id = gr.Textbox(label="User ID", value="demo")
            bill_id = gr.Textbox(label="Bill ID", value="bill-001")
        with gr.Row():
            offsets = gr.Textbox(label="Offsets (days, comma)", value="7,3,0")
            hour = gr.Number(label="Hour (0-23)", value=9, precision=0)
        with gr.Row():
            smart_btn = gr.Button("Smart Schedule (use extracted due date)")
            plan_out = gr.Textbox(label="Plan JSON", lines=10)
        smart_btn.click(smart_schedule, inputs=[state, user_id, bill_id, offsets, hour], outputs=[plan_out])

        with gr.Row():
            ics_btn = gr.Button("Download .ics for due date")
            ics_file = gr.File(label="ICS", interactive=False)
        ics_btn.click(export_ics, inputs=[state], outputs=[ics_file])

    with gr.Tab("Pay (Mock)"):
        with gr.Row():
            provider = gr.Textbox(label="Provider", value="GWCL")
            amount = gr.Number(label="Amount", value=120.50, precision=2)
            currency = gr.Textbox(label="Currency (ISO)", value="GHS")
        btn3 = gr.Button("Get Payment Link")
        pay_out = gr.Markdown(label="Payment")
        btn3.click(pay_markdown, inputs=[provider, amount, currency], outputs=[pay_out])

    with gr.Tab("History"):
        gr.Markdown("Recent extractions (this session only)")
        hist = gr.Dataframe(headers=["provider","due_date","amount","currency"], row_count=(0, "dynamic"))
        def _refresh_hist(_1,_2,_3,_state):
            return HISTORY
        btn.click(_refresh_hist, inputs=[file, text_input, full_json, state], outputs=[hist])

    with gr.Tab("Chat with Agent"):
        gr.Markdown("Talk to your BillsBuddy agent (Lambda). Attach a bill or just type.")
        chat = gr.Chatbot(height=360)
        msg = gr.Textbox(label="Message", placeholder="Say: 'Extract this and schedule reminders' or leave blank and only attach a file")
        agent_file = gr.File(label="Attach bill (optional)", type="filepath")
        send = gr.Button("Send to Agent", variant="primary")
        clear = gr.Button("Clear")

        def _agent_handle(user_msg, file_path, history):
            history = history or []
            bot = agent_chat_with_optional_file(user_msg, file_path)
            return history + [[user_msg or "(file only)", bot]]

        send.click(_agent_handle, inputs=[msg, agent_file, chat], outputs=[chat])
        clear.click(lambda: None, None, [chat])

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
