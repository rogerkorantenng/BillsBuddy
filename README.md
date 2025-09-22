# 🧾 BillsBuddy

**BillsBuddy** is an AI agent that helps you **read any bill**, **extract key details**, **plan reminders**, and even generate a **mock payment link** — all wrapped in a friendly **Gradio** UI.  
The project is built end-to-end on **AWS** (S3, Lambda, API Gateway, Bedrock, Textract).

---

## ✨ Features

- **Understand bills** (PDF/image/text) → extracts provider, amount, currency, due date, account #, billing period, and notes.
- **Smart reminder plan** → generates reminders (7-day, 3-day, same-day) with:
  - Human-readable schedule
  - `.ics` calendar export
- **Mock payment link** → demo URL with reference number.
- **Agent chat (optional)** → talk to a Bedrock Agent, attach a bill, and let it orchestrate extraction.
- **Session history** → table view + CSV export.
- **Polished UI** → cards, high-contrast CSS, API health badge, error handling.

---

## 🧱 Architecture

- **Gradio UI** → local tester interface  
- **API Gateway** → routes requests to Lambdas  
- **Lambda functions** → handle presign, extract, schedule, pay, and chat (optional)  
- **S3** → secure bill storage  
- **Textract** → OCR for PDFs/images  
- **Bedrock** → Claude 3.5 Sonnet for structured extraction + chat  

---

## ✅ Prerequisites

- AWS account (**us-east-1** recommended)  
- Private **S3 bucket** (e.g., `billsbuddy-uploads-<suffix>`)  
- **Bedrock model access** → `anthropic.claude-3-5-sonnet-20240620-v1:0`  
- Python 3.10+ (3.12 recommended)  

---

## 🚀 Deployment Guide

### 1. Create Lambda Functions
Create the following functions in AWS Lambda (runtime: Python 3.12, handler: `lambda_function.lambda_handler`):

- `PresignUploadFn` (timeout: 10s)  
- `ExtractBillFn` (timeout: 60s, memory: 512–1024MB)  
- `ScheduleRemindersFn` (timeout: 10s)  
- `MockPaymentLinkFn` (timeout: 10s)  
- `AgentChatFn` *(optional, timeout: 60s)*  

Each Lambda needs an IAM role with S3 + Textract + Bedrock permissions.

### 2. Connect Lambdas to API Gateway

1. Create a **REST API** in API Gateway.  
2. Add routes and integrate them with your Lambda functions:  

| Route            | Lambda Function       |
|------------------|-----------------------|
| `/tools/presign` | PresignUploadFn       |
| `/tools/extract` | ExtractBillFn         |
| `/tools/schedule`| ScheduleRemindersFn   |
| `/tools/pay`     | MockPaymentLinkFn     |
| `/agent/chat`    | AgentChatFn *(opt.)*  |

3. Deploy your API and note the **Base URL** — this will be used by the Gradio app.

### 3. Run the App Locally

1. **Install dependencies**  
   Make sure you have Python 3.10+ (3.12 recommended), then install all required libraries:

   ```bash
   pip install -r requirements.txt
    ```
   
    ```bash
    python app.py
    ```
