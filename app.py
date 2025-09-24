# app.py
# Bills & Subscriptions Buddy (Gradio)
# UI polish + improved History (table + picker + details + CSV export)
# Smart Schedule now renders both legacy shape and {"plan":[...]} nicely.

import os
import json
import time
import uuid
import hashlib
import mimetypes
import tempfile
import re
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
API_BASE = os.getenv("API_BASE")
UPLOAD_BUCKET = os.getenv("UPLOAD_BUCKET")
REGION = os.getenv("REGION")

PRESIGN_URL    = f"{API_BASE}/tools/presign" if API_BASE else None
EXTRACT_URL    = f"{API_BASE}/tools/extract"  if API_BASE else None
SCHEDULE_URL   = f"{API_BASE}/tools/schedule" if API_BASE else None
PAY_URL        = f"{API_BASE}/tools/pay"      if API_BASE else None
AGENT_CHAT_URL = f"{API_BASE}/agent/chat"     if API_BASE else None

AGENT_SESSION = str(uuid.uuid4())  # one chat session per app run

# --- Minimal CSS for a nicer UI ---
CUSTOM_CSS = """
:root {
  --bb-card-bg: #f9fafb;
  --bb-muted:   #6b7280;
  --bb-border:  #d1d5db;
  --bb-text:    #111827;
  --bb-accent:  #2563eb;
}

/* dark-mode fallback */
@media (prefers-color-scheme: dark) {
  :root {
    --bb-card-bg: #111827;
    --bb-border:  #374151;
    --bb-text:    #f3f4f6;
  }
}

.bb-card{
  border:1px solid var(--bb-border);
  border-radius:14px;
  padding:16px;
  background:var(--bb-card-bg);
  box-shadow:0 1px 2px rgba(0,0,0,.06);
  color:var(--bb-text);
  min-height: 96px;
}

/* ensure inner text isn't inheriting invisible theme colors */
.bb-card, .bb-card * {
  color: var(--bb-text) !important;
}

.bb-grid{display:grid; grid-template-columns:1fr 1fr; gap:10px;}
.bb-kv{display:flex; justify-content:space-between; gap:12px; padding:6px 0; border-bottom:1px dashed var(--bb-border);}
.bb-kv span:first-child{color:var(--bb-muted) !important;}
.bb-badge{display:inline-block; font-size:12px; padding:3px 8px; border-radius:999px; border:1px solid var(--bb-border);}
.bb-badge.ok{background: black;}
.bb-badge.err{background: black;}
.bb-mono{font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono","Courier New", monospace; font-size:12px;}
.bb-section-title{font-weight:600; margin:8px 0 4px;}
a.bb-btn{display:inline-block; padding:8px 12px; border:1px solid var(--bb-border); border-radius:10px; text-decoration:none;}

/* History table container cap */
#hist_table { max-height: 220px; overflow: auto; }
"""

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

def _fmt_amount(a, c):
    if a is None: return "—"
    try:
        return f"{float(a):,.2f} {c or ''}".strip()
    except Exception:
        return f"{a} {c or ''}".strip()

def _fmt_date(d):
    return d or "—"

SUMMARY_TMPL_HTML = """
<div class="bb-card">
  <div class="bb-grid">
    <div>
      <div class="bb-section-title">🧾 Bill Summary</div>
      <div class="bb-kv"><span>Provider</span><span><strong>{provider}</strong></span></div>
      <div class="bb-kv"><span>Amount</span><span><strong>{amount_fmt}</strong></span></div>
      <div class="bb-kv"><span>Due date</span><span><strong>{due_date_fmt}</strong></span></div>
      <div class="bb-kv"><span>Account #</span><span>{account_number}</span></div>
    </div>
    <div>
      <div class="bb-section-title">Details</div>
      <div class="bb-kv"><span>Period</span><span>{period_start_fmt} → {period_end_fmt}</span></div>
      <div class="bb-kv"><span>Penalties</span><span>{penalties}</span></div>
      <div class="bb-kv"><span>Currency</span><span>{currency}</span></div>
    </div>
  </div>
  <div style="margin-top:10px; color:var(--bb-muted);">{notes}</div>
</div>
"""

def make_summary_html(data: dict) -> str:
    safe = {
        'provider': data.get('provider') or '—',
        'amount_fmt': _fmt_amount(data.get('amount'), data.get('currency')),
        'currency': data.get('currency') or '—',
        'due_date_fmt': _fmt_date(data.get('due_date')),
        'account_number': data.get('account_number') or '—',
        'period_start_fmt': _fmt_date(data.get('period_start')),
        'period_end_fmt': _fmt_date(data.get('period_end')),
        'penalties': '—' if data.get('penalties') is None else data.get('penalties'),
        'notes': data.get('notes') or '',
    }
    return SUMMARY_TMPL_HTML.format(**safe)

# --- Plan rendering (supports old shape and {"plan":[...]}) ---

def _parse_iso(s: str):
    if not s:
        return None
    try:
        if isinstance(s, str) and s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(str(s))
    except Exception:
        return None

def _fmt_dt(s: str) -> str:
    """Best-effort ISO → friendly. Falls back to raw string."""
    if not s:
        return "—"
    dt = _parse_iso(s)
    if not dt:
        # try date-only like YYYY-MM-DD
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(s)):
                dt2 = datetime.fromisoformat(str(s))
                return dt2.strftime("%a, %d %b %Y")
        except Exception:
            pass
        return str(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M %Z")

def render_plan_html(plan: dict) -> str:
    """Render schedule plan from either:
       - legacy shape with due_date/hour/offsets/reminders
       - new shape with {'plan': [{when,type,offset_days}, ...]}
    """
    if not isinstance(plan, dict):
        return (
            "<div class='bb-card'><div class='bb-section-title'>Plan</div>"
            f"<pre class='bb-mono'>{_pretty_json(plan)}</pre></div>"
        )

    if "error" in plan:
        return (
            "<div class='bb-card'>"
            "<div class='bb-section-title'>Plan</div>"
            "<div class='bb-kv'><span>Status</span><span>❌ Error</span></div>"
            f"<pre class='bb-mono'>{_pretty_json(plan['error'])}</pre>"
            "</div>"
        )

    # 1) List of items under common keys (incl. 'plan')
    reminders = None
    for k in ("reminders", "schedules", "items", "events", "plan"):
        if isinstance(plan.get(k), list):
            reminders = plan.get(k)
            break
    reminders = reminders or []

    # 2) Top-level metadata if present
    due   = plan.get("due_date") or plan.get("dueDate")
    user  = plan.get("userId") or plan.get("user") or "—"
    bill  = plan.get("billId") or plan.get("bill") or "—"
    hour  = plan.get("hour")
    offs  = plan.get("offsets_days") or plan.get("offsetsDays") or plan.get("offsets")

    # 3) Derive missing fields
    due_dt = None
    if not due and reminders:
        zero_items = [r for r in reminders
                      if isinstance(r, dict) and (r.get("offset_days") == 0 or str(r.get("type","")).lower() in ("due-day","due","final"))]
        candidates = zero_items or reminders
        dts = []
        for r in candidates:
            ts = None
            if isinstance(r, dict):
                ts = r.get("when") or r.get("datetime") or r.get("time") or r.get("scheduled_for")
            dt = _parse_iso(ts)
            if dt:
                dts.append(dt)
        if dts:
            due_dt = max(dts)
            due = due_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    if hour is None:
        src_dt = due_dt
        if not src_dt and reminders:
            first_ts = reminders[0].get("when") if isinstance(reminders[0], dict) else None
            src_dt = _parse_iso(first_ts)
        if src_dt:
            hour = src_dt.hour

    if offs is None:
        offs_set = []
        for r in reminders:
            if isinstance(r, dict) and r.get("offset_days") is not None:
                try:
                    v = int(r.get("offset_days"))
                    if v not in offs_set:
                        offs_set.append(v)
                except Exception:
                    pass
        offs = sorted(offs_set) if offs_set else None

    # 4) Build rows
    rows_html_parts = []
    def _pick_ts(d):
        if not isinstance(d, dict):
            return None
        for k in ("when","runAt","schedule_time","scheduleTime","execute_at",
                  "scheduled_for","date","datetime","ts","time"):
            if k in d:
                return d.get(k)
        for v in d.values():
            if isinstance(v, str) and re.search(r"\d{4}-\d{2}-\d{2}", v):
                return v
        return None

    for idx, r in enumerate(reminders, 1):
        ts = _pick_ts(r)
        nice = _fmt_dt(ts)
        rid = (r.get("id") or r.get("pk") or r.get("name") or f"#{idx}") if isinstance(r, dict) else f"#{idx}"
        status = (r.get("status") if isinstance(r, dict) else "") or ""
        rtype  = (r.get("type") if isinstance(r, dict) else "") or ""
        odst   = None
        if isinstance(r, dict) and r.get("offset_days") is not None:
            try:
                odst = int(r["offset_days"])
            except Exception:
                pass

        rid_html    = f" &nbsp; <span class='bb-mono'>{rid}</span>" if rid else ""
        status_html = f" &nbsp; ({status})" if status else ""
        type_html   = f" &nbsp; <span class='bb-mono'>{rtype}</span>" if rtype else ""
        off_html    = f" &nbsp; <span class='bb-mono'>[{odst:+}d]</span>" if isinstance(odst, int) else ""

        rows_html_parts.append(
            f"<div class='bb-kv'><span>Reminder {idx}</span>"
            f"<span><strong>{nice}</strong>{rid_html}{type_html}{off_html}{status_html}</span></div>"
        )

    offs_txt = ", ".join(str(x) for x in offs) if isinstance(offs, (list, tuple)) and offs else "—"
    hour_txt = f"{int(hour):02d}:00" if isinstance(hour, (int, float)) else (hour or "—")
    rows_block = "".join(rows_html_parts) if rows_html_parts else "<div style='margin-top:8px; color:var(--bb-muted)'>No individual reminder rows returned.</div>"

    return (
        "<div class='bb-card'>"
        "<div class='bb-section-title'>🔔 Reminder Plan</div>"
        f"<div class='bb-kv'><span>Due date</span><span><strong>{_fmt_dt(due)}</strong></span></div>"
        f"<div class='bb-kv'><span>User</span><span>{user}</span></div>"
        f"<div class='bb-kv'><span>Bill</span><span>{bill}</span></div>"
        f"<div class='bb-kv'><span>Daily hour</span><span>{hour_txt}</span></div>"
        f"<div class='bb-kv'><span>Offsets (days)</span><span>{offs_txt}</span></div>"
        f"{rows_block}"
        "</div>"
    )

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

# --- History (session-scoped) ---
HISTORY = []  # list of {id, ts, provider, due_date, amount, currency, data}

def _push_history(data: dict):
    entry = {
        "id": str(uuid.uuid4())[:8],
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "provider": data.get("provider"),
        "due_date": data.get("due_date"),
        "amount": data.get("amount"),
        "currency": data.get("currency"),
        "data": data
    }
    HISTORY.insert(0, entry)  # newest first

def _history_rows_and_choices():
    rows = [{"when": e["ts"], "provider": e["provider"], "due_date": e["due_date"],
             "amount": e["amount"], "currency": e["currency"], "id": e["id"]} for e in HISTORY]
    choices = [ (e["id"], f'{e["ts"]} — {e.get("provider") or "?"} — { _fmt_amount(e.get("amount"), e.get("currency")) }')
               for e in HISTORY ]
    return rows, choices

def _history_get(id_):
    for e in HISTORY:
        if e["id"] == id_:
            return e
    return None

# --- Gradio callbacks ---

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

    summary_html = make_summary_html(data)
    state = data

    missing_all = not any([data.get("provider"), data.get("amount"), data.get("currency"), data.get("due_date")])
    if missing_all and data.get("text_preview"):
        preview = data["text_preview"][:1200]
        return summary_html, "⚠️ Couldn’t find key fields. OCR saw:\n\n```\n" + preview + "\n```", data, state

    _push_history(data)
    return summary_html, "", data, state

def smart_schedule(state, user_id, bill_id, offsets_str, hour):
    if not state:
        return "<div class='bb-card'>Run <strong>Understand</strong> first.</div>"
    due = state.get("due_date")
    if not due:
        return "<div class='bb-card'>No due date extracted. Enter it manually on the Schedule tab.</div>"
    try:
        offsets = [int(x.strip()) for x in offsets_str.split(',') if x.strip()]
    except Exception:
        offsets = [7,3,0]

    data, err = call_schedule(due, user_id or "demo", bill_id or "bill-001", tuple(offsets), int(hour or 9))
    plan = err or data or {}
    return render_plan_html(plan)

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
        return f"❌ Payment API error:\n\n```\n{_pretty_json(err)}\n```"
    url = data.get("url"); ref = data.get("reference")
    return (
        f"<div class='bb-card'>"
        f"<div class='bb-section-title'>Payment</div>"
        f"<div class='bb-kv'><span>Link</span><span><a class='bb-btn' href='{url}' target='_blank' rel='noopener'>Open payment page</a></span></div>"
        f"<div class='bb-kv'><span>Reference</span><span class='bb-mono'>{ref}</span></div>"
        f"<div class='bb-kv'><span>Amount</span><span><strong>{_fmt_amount(data.get('amount'), data.get('currency'))}</strong></span></div>"
        f"<div class='bb-kv'><span>Provider</span><span>{data.get('provider')}</span></div>"
        f"</div>"
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
            return f"❌ Agent API error:\n\n```\n{_pretty_json(err)}\n```"
        return r.json().get("reply", "(no reply)")
    except Exception as e:
        return f"❌ Agent request failed: {e}"

def agent_chat_with_optional_file(user_msg, file_path):
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
with gr.Blocks(title="Bills Buddy (Gradio)", css=CUSTOM_CSS) as demo:
    gr.Markdown("# Bills & Subscriptions Buddy")

    # Status strip
    ok, msg = api_health()
    status_badge = f"<span class='bb-badge {'ok' if ok else 'err'}'>{'✅' if ok else '❌'} {msg}</span>"
    gr.HTML(f"<div>API status: {status_badge}</div>")

    state = gr.State({})

    with gr.Tab("Understand Bill"):
        with gr.Row():
            with gr.Column(scale=1):
                file = gr.File(label="Upload bill (image/PDF)", type="filepath")
                text_input = gr.Textbox(label="Or paste bill text", lines=8, placeholder="Paste bill text here...")
                btn = gr.Button("Understand", variant="primary")
            with gr.Column(scale=1):
                summary_html = gr.HTML(value="<div class='bb-card'>Bill summary will appear here after extraction.</div>")
                error_md = gr.Markdown(value="")
        with gr.Accordion("Full extracted JSON", open=False):
            full_json = gr.JSON(value={})

        # Main flow
        ev = btn.click(understand_bill, inputs=[file, text_input, state],
                       outputs=[summary_html, error_md, full_json, state])

    with gr.Tab("Schedule & Calendar"):
        with gr.Row():
            user_id = gr.Textbox(label="User ID", value="demo")
            bill_id = gr.Textbox(label="Bill ID", value="bill-001")
        with gr.Row():
            offsets = gr.Textbox(label="Offsets (days, comma)", value="7,3,0")
            hour = gr.Number(label="Hour (0-23)", value=9, precision=0)
        with gr.Row():
            smart_btn = gr.Button("Smart Schedule (use extracted due date)")
            plan_out = gr.HTML(label="Plan")
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
        pay_out = gr.HTML(label="Payment")
        btn3.click(pay_markdown, inputs=[provider, amount, currency], outputs=[pay_out])

    # -------- Improved History --------
    with gr.Tab("History"):
        gr.Markdown("Session history of extractions")
        hist_table = gr.Dataframe(
            headers=["when","provider","due_date","amount","currency","id"],
            row_count=(0, "dynamic"),
            interactive=False, wrap=False,
            elem_id="hist_table"   # CSS scroll cap
        )
        hist_pick = gr.Dropdown()
        hist_view = gr.HTML(label="Selected summary")
        hist_json = gr.JSON(label="Selected JSON", value={})

        def _refresh_hist():
            rows, choices = _history_rows_and_choices()
            return rows, choices

        def _on_pick(item_id):
            if not item_id:
                return "", {}
            e = _history_get(item_id)
            if not e:
                return "", {}
            return make_summary_html(e["data"]), e["data"]

        # After each Understand, refresh history table + picker
        ev.then(_refresh_hist, inputs=None, outputs=[hist_table, hist_pick])
        # When a user picks an item, show summary+json
        hist_pick.change(_on_pick, inputs=[hist_pick], outputs=[hist_view, hist_json])

        # Export CSV
        def _export_csv():
            import csv
            fd, path = tempfile.mkstemp(prefix="billsbuddy_history_", suffix=".csv")
            cols = ["when","provider","due_date","amount","currency","id"]
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for r in _history_rows_and_choices()[0]:
                    w.writerow([r.get(c,"") for c in cols])
            return path

        csv_btn = gr.Button("Download history (.csv)")
        csv_file = gr.File(label="CSV", interactive=False)
        csv_btn.click(_export_csv, inputs=None, outputs=[csv_file])

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
