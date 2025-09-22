# lambda_function.py
import json, time, re, base64, logging, traceback
from datetime import datetime
import boto3

# ====== HARD-CODED CONFIG ======


REGION   = "YOUR_AWS_REGION"
MODEL_ID = "YOUR_BEDROCK_MODEL"  # Bedrock (Anthropic Claude 3.5 Sonnet)

# Logging toggles
DEBUG_MODEL = True                 # log model input/output (truncated)
DEBUG_MODEL_MAX_CHARS = 2000       # truncate large blobs in logs
DEBUG_REDACT_IDS = True            # redact long digit strings in logs

# ====== CLIENTS ======
textract = boto3.client("textract", region_name=REGION)
bedrock  = boto3.client("bedrock-runtime", region_name=REGION)

# ====== LOGGING HELPERS ======
log = logging.getLogger()
log.setLevel(logging.INFO)

def _jlog(evt, **kw):
    rec = {"evt": evt, **kw}
    try:
        log.info(json.dumps(rec, ensure_ascii=False))
    except Exception:
        log.info(f"{evt} | {kw}")

def _truncate(s: str, n: int):
    if s is None: return None
    if len(s) <= n: return s
    head = s[: int(n*0.6)]
    tail = s[-int(n*0.3):]
    return head + "\n...\n" + tail

def _redact_ids(s: str):
    if not s or not DEBUG_REDACT_IDS:
        return s
    def repl(m):
        d = m.group(0)
        return "X"*max(0, len(d)-4) + d[-4:]
    return re.sub(r'(?<!\d)\d{9,}(?!\d)', repl, s)

# ====== HTTP RESPONSES ======
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

# ====== OCR ======
def _ext(name: str) -> str:
    name = (name or "").lower()
    for ext in (".pdf", ".tif", ".tiff", ".png", ".jpg", ".jpeg"):
        if name.endswith(ext):
            return ext
    return ""

def _collect_text_from_blocks(blocks):
    lines, pages = [], set()
    for b in blocks or []:
        if b.get("BlockType") == "LINE" and "Text" in b:
            lines.append(b["Text"])
        if "Page" in b: pages.add(b["Page"])
    return "\n".join(lines), (max(pages) if pages else 1)

def _textract_sync(bucket, key):
    r = textract.detect_document_text(Document={"S3Object": {"Bucket": bucket, "Name": key}})
    return _collect_text_from_blocks(r.get("Blocks"))

def _textract_async(bucket, key, max_wait_sec=60):
    start = textract.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}}
    )
    job_id = start["JobId"]
    waited, sleep = 0, 2
    while True:
        status_res = textract.get_document_text_detection(JobId=job_id, MaxResults=1)
        status = status_res["JobStatus"]
        if status == "SUCCEEDED": break
        if status in ("FAILED", "PARTIAL_SUCCESS"):
            raise RuntimeError(f"Textract job ended with status: {status} (jobId={job_id})")
        time.sleep(sleep); waited += sleep
        if waited >= max_wait_sec:
            raise TimeoutError(f"Textract async job not finished after {max_wait_sec}s (jobId={job_id}).")
    blocks_all, nt = [], None
    while True:
        args = {"JobId": job_id, "MaxResults": 1000}
        if nt: args["NextToken"] = nt
        page = textract.get_document_text_detection(**args)
        blocks_all.extend(page.get("Blocks", []))
        nt = page.get("NextToken")
        if not nt: break
    return _collect_text_from_blocks(blocks_all)

# ====== NORMALIZATION ======
MONTHS = {'jan':1,'january':1,'feb':2,'february':2,'mar':3,'march':3,'apr':4,'april':4,
          'may':5,'jun':6,'june':6,'jul':7,'july':7,'aug':8,'august':8,'sep':9,'sept':9,'september':9,
          'oct':10,'october':10,'nov':11,'november':11,'dec':12,'december':12}

def _iso_or_none(y, m, d):
    try: return datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d")
    except Exception: return None

def _parse_yyyy_sep_dd(s: str):
    m = re.search(r'\b(\d{4})[\/\-.](\d{1,2})[\/\-.](\d{1,2})\b', s)
    if not m: return None
    return _iso_or_none(m.group(1), m.group(2), m.group(3))

def _parse_dd_mon_yyyy(s: str):
    m = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})[,\s]+(\d{4})\b', s)
    if not m: return None
    mo = MONTHS.get(m.group(2).lower().strip('.'))
    return _iso_or_none(m.group(3), mo, m.group(1)) if mo else None

def _parse_mon_dd_yyyy(s: str):
    m = re.search(r'\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?[,\s]+(\d{4})\b', s)
    if not m: return None
    mo = MONTHS.get(m.group(1).lower().strip('.'))
    return _iso_or_none(m.group(3), mo, m.group(2)) if mo else None

def _parse_date_iso_any(s: str):
    return _parse_yyyy_sep_dd(s) or _parse_dd_mon_yyyy(s) or _parse_mon_dd_yyyy(s)

def _to_float(num_str: str):
    s = (num_str or "").strip()
    s = s.replace("GH₵","").replace("GH¢","").replace("GHS","").replace("GHC","")
    s = re.sub(r'\s', '', s)
    if ',' in s and '.' in s: s = s.replace(',', '')
    elif ',' in s and '.' not in s: s = s.replace(',', '.')
    try: return float(s)
    except Exception: return None

def _iso_date(v):
    if not v: return None
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(v)):
        y,m,d = v.split('-'); return _iso_or_none(y,m,d)
    return _parse_date_iso_any(str(v))

def _norm_currency(c):
    if not c: return None
    c = str(c).upper().strip()
    if c in ("USD","EUR","GBP","GHS","NGN","ZAR"): return c
    if c in ("$","USD$"): return "USD"
    if c in ("€","EUR€"): return "EUR"
    if c in ("£","GBP£"): return "GBP"
    if c in ("GH₵","GH¢","GHC"): return "GHS"
    if c in ("₦",): return "NGN"
    return c

# ====== LLM (Bedrock - Anthropic) ======
SCHEMA_KEYS = ["provider","amount","currency","due_date","account_number","invoice_number","period_start","period_end"]

def _clip(text: str, limit=8000):
    if not text: return ""
    if len(text) <= limit: return text
    head = text[: int(limit*0.6)]
    tail = text[-int(limit*0.3):]
    return head + "\n...\n" + tail

def _extract_json_anywhere(s: str):
    m = re.search(r'\{[\s\S]*\}', s)
    if not m: return None
    try: return json.loads(m.group(0))
    except Exception: return None

def _llm_extract_fields(text, req_id=None) -> dict:
    # Anthropic "messages" request (Bedrock)
    prompt = f"""
You extract structured fields from bills and invoices. Return **only JSON** (no prose) with these keys:
{', '.join(SCHEMA_KEYS)}

Rules:
- provider: issuer name (string).
- amount: numeric (e.g., 123.45).
- currency: ISO code if possible (USD, GHS, EUR, GBP, NGN, ZAR) else best guess.
- due_date, period_start, period_end: format YYYY-MM-DD.
- account_number, invoice_number: strings; strip spaces/dashes if not essential.
- If a field is missing, set it to null (but keep the key).
- Do not include any keys other than: {', '.join(SCHEMA_KEYS)}.

Bill text:
{_clip(text)}
""".strip()

    body = {
        "anthropic_version": "bedrock-2023-05-31",  # REQUIRED
        "messages": [
            {"role": "user", "content": [ {"type": "text", "text": prompt} ]}
        ],
        "max_tokens": 500,
        "temperature": 0
        # "response_format": {"type": "json_object"}  # (optional when supported)
    }

    if DEBUG_MODEL:
        _jlog("bedrock_invoke_start",
              req_id=req_id, model_id=MODEL_ID,
              prompt_chars=len(prompt), bill_chars=len(text))

    r = bedrock.invoke_model(
        modelId=MODEL_ID,
        accept="application/json",
        contentType="application/json",
        body=json.dumps(body)
    )
    raw = r["body"].read().decode("utf-8", "ignore")

    if DEBUG_MODEL:
        _jlog("bedrock_invoke_raw", req_id=req_id,
              sample=_truncate(_redact_ids(raw), DEBUG_MODEL_MAX_CHARS))

    # Parse Anthropic response: content is a list of blocks with type "text"
    parsed = {}
    try:
        out = json.loads(raw)
        if isinstance(out, dict) and isinstance(out.get("content"), list):
            all_text = []
            for c in out["content"]:
                if isinstance(c, dict) and c.get("type") == "text":
                    all_text.append(c.get("text", ""))
            text_block = "\n".join(all_text)
            j = _extract_json_anywhere(text_block)
            if isinstance(j, dict):
                parsed = j
        if not parsed:
            j = _extract_json_anywhere(raw)
            if isinstance(j, dict):
                parsed = j
    except Exception:
        j = _extract_json_anywhere(raw)
        if isinstance(j, dict):
            parsed = j

    if DEBUG_MODEL:
        _jlog("bedrock_parsed",
              req_id=req_id,
              keys=list(parsed.keys()) if isinstance(parsed, dict) else None,
              sample=_truncate(_redact_ids(json.dumps(parsed, ensure_ascii=False)) if isinstance(parsed, dict) else None,
                               DEBUG_MODEL_MAX_CHARS))

    return parsed if isinstance(parsed, dict) else {}

# ====== RULE FALLBACK (best-effort) ======
def _rule_fallback(text: str) -> dict:
    res = {k: None for k in SCHEMA_KEYS}
    if re.search(r'\bGHS\b|GH₵|GH¢|GHC', text, re.I): res["currency"]="GHS"
    elif re.search(r'\bUSD\b|\$', text): res["currency"]="USD"
    elif re.search(r'\bEUR\b|€', text):  res["currency"]="EUR"
    elif re.search(r'\bGBP\b|£', text):  res["currency"]="GBP"
    for m in re.finditer(r'(amount\s*(?:due|payable)|total\s*(?:due|amount|payable)|balance\s*due|grand\s*total)', text, re.I):
        tail = text[m.end(): m.end()+160]
        n = re.search(r'([0-9]{1,3}(?:[,\s][0-9]{3})*(?:[.,][0-9]{2})|[0-9]+(?:[.,][0-9]{2})?)', tail)
        if n: res["amount"] = _to_float(n.group(1)); break
    res["due_date"] = _parse_date_iso_any(text)
    head = '\n'.join(text.splitlines()[:10])
    head = re.sub(r'\b(invoice|bill|statement)\b', '', head, flags=re.I)
    lines = [ln.strip() for ln in head.splitlines() if re.search(r'[A-Za-z]', ln)]
    lines.sort(key=len, reverse=True)
    if lines: res["provider"] = lines[0]
    return res

# ====== HANDLER ======
def lambda_handler(event, context):
    req_id = (event.get("requestContext",{}).get("requestId")
              or getattr(context, "aws_request_id", None))

    try:
        # CORS preflight
        if event.get("requestContext",{}).get("http",{}).get("method")=="OPTIONS":
            return _resp(204, {})

        # Parse body
        body_raw = event.get("body") or "{}"
        if isinstance(body_raw, str) and event.get("isBase64Encoded"):
            try: body_raw = base64.b64decode(body_raw).decode("utf-8","ignore")
            except Exception: pass
        body = json.loads(body_raw) if isinstance(body_raw, str) else (body_raw or {})

        raw = (body.get("raw_text") or "").strip()
        bucket, key = body.get("bucket"), body.get("key")

        # OCR / source
        if raw:
            text, pages = raw, 1
            source = {"mode":"raw"}
        else:
            if not bucket or not key:
                return _resp(400, {"error":"Provide raw_text or {bucket,key}."})
            ext = _ext(key)
            if ext in (".pdf",".tif",".tiff"):
                text, pages = _textract_async(bucket, key, max_wait_sec=60)
            else:
                text, pages = _textract_sync(bucket, key)
            source = {"mode":"s3","bucket":bucket,"key":key}

        _jlog("extract_request", req_id=req_id, source=source, pages=pages, text_chars=len(text))

        # LLM-first
        fields = {}
        try:
            fields = _llm_extract_fields(text, req_id)
        except Exception as e:
            _jlog("error", req_id=req_id, where="llm_primary", error=str(e))
            fields = {}

        # Fallback if LLM empty
        if not isinstance(fields, dict) or not fields:
            fields = _rule_fallback(text)
            _jlog("rule_fallback_used", req_id=req_id)

        # Normalize outputs
        for k in SCHEMA_KEYS:
            fields.setdefault(k, None)
        if fields.get("amount") is not None:
            try: fields["amount"] = float(fields["amount"])
            except Exception:
                fields["amount"] = _to_float(str(fields["amount"]))
        fields["currency"] = _norm_currency(fields.get("currency"))
        for dk in ("due_date","period_start","period_end"):
            fields[dk] = _iso_date(fields.get(dk))
        fields["period"] = (f"{fields['period_start']} to {fields['period_end']}"
                            if fields.get("period_start") and fields.get("period_end") else None)

        _jlog("extract_result", req_id=req_id,
              keys=[k for k,v in fields.items() if v is not None],
              preview=_truncate(_redact_ids(json.dumps(fields, ensure_ascii=False)), DEBUG_MODEL_MAX_CHARS))

        return _resp(200, {
            **fields,
            "pages": pages,
            "source": source,
            "text_preview": text[:4000],
            "found_by": {"extraction": "llm_primary_with_rule_fallback"}
        })

    except TimeoutError as e:
        _jlog("error", req_id=req_id, where="timeout", error=str(e))
        return _resp(504, {"error": str(e)})
    except Exception as e:
        _jlog("error", req_id=req_id, where="handler", error=str(e),
              trace=traceback.format_exc()[:1500])
        return _resp(500, {"error": str(e)})
