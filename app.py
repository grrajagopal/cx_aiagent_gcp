import os
import json
import uuid
import datetime
import nest_asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from google.genai import client as genai_client, types
from google.cloud import bigquery
import google.cloud.dlp

app = FastAPI(title="ISP Autonomous Agent Controller")
ai_client = genai_client.Client(vertexai=True, location="us-central1")

os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"
os.environ["GOOGLE_CLOUD_PROJECT"] = "cxlens-grr"
os.environ["GOOGLE_CLOUD_LOCATION"] = "us-central1"

class AuditRequest(BaseModel):
    customer_id: str

def get_customer_interaction_logs(customer_id: str) -> str:
    bq = bigquery.Client()
    query = f"SELECT call_date, summary FROM `{bq.project}.isp_operations_dataset.call_summaries` WHERE customer_id = '{customer_id}'"
    try:
        rows = [dict(row) for row in bq.query(query).result()]
        return json.dumps(rows, default=str)
    except Exception as e:
        return f"Interaction query failure: {str(e)}"

def check_backend_system_ledger(customer_id: str, isolated_domain: str) -> str:
    bq = bigquery.Client()
    query = f"SELECT status, system_notes, promised_value, actual_value FROM `{bq.project}.isp_operations_dataset.system_state_ledger` WHERE customer_id = '{customer_id}' AND domain_type = '{isolated_domain}'"
    try:
        rows = [dict(row) for row in bq.query(query).result()]
        return json.dumps(rows)
    except Exception as e:
        return f"System state check failure: {str(e)}"

def write_escalation_alert_record(customer_id: str, case_domain: str, risk_level: str, mismatch_summary: str) -> str:
    bq = bigquery.Client()
    table_id = f"{bq.project}.isp_operations_dataset.operations_alerts"
    alert_row = [{
        "alert_id": str(uuid.uuid4())[:8],
        "customer_id": customer_id,
        "case_domain": case_domain,
        "repeat_risk_level": risk_level,
        "mismatch_summary": mismatch_summary,
        "escalated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }]
    try:
        bq.insert_rows_json(table_id, alert_row)
        return "Alert safely written to BigQuery table."
    except Exception as e:
        return f"Database logging failure: {str(e)}"

@app.post("/v1/audit-customer")
async def audit_customer(request: AuditRequest):
    try:
        customer_id = request.customer_id
        raw_call_history = get_customer_interaction_logs(customer_id)
        
        supervisor_instruction = (
            "You are the Enterprise Scalable Operations Director. Your job is to identify unresolved repeat issues "
            "and log alerts. Analyze the provided customer call history logs, isolate the primary underlying text issue intent, "
            "and map it strictly to: 'BILLING', 'FIELD_DISPATCH', or 'PROVISIONING'. "
            "Call check_backend_system_ledger to verify. If status is FAILED or PENDING and contradicts expectations, "
            "execute write_escalation_alert_record. Summarize your final answer."
        )
        
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Customer ID: {customer_id}\nCall History Logs:\n{raw_call_history}",
            config=types.GenerateContentConfig(
                system_instruction=supervisor_instruction,
                tools=[check_backend_system_ledger, write_escalation_alert_record],
                temperature=0.0
            )
        )
        return {"status": "COMPLETED", "analysis": response.text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
