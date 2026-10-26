"""Run the agent end-to-end on synthetic leads. No API keys needed.

    python run_demo.py
"""
import json
from pathlib import Path

from sdr_agent import InMemoryCRM, Lead, SDRAgent, default_client

leads = [Lead(**row) for row in json.loads(Path("data/sample_leads.json").read_text())]
crm = InMemoryCRM()
agent = SDRAgent(crm, default_client(), mode="draft")

print("=== Step 1: new inbound leads ===")
for lead in leads:
    d = agent.handle_new_lead(lead)
    print(f"{lead.lead_id}  score={lead.score:>3}  -> {d.action:<10} {d.reason}")

print("\n=== Step 2: simulated replies ===")
replies = {
    "L-1003": "Sounds good, can we schedule a call Thursday?",
    "L-1005": "Is it FDA cleared and how long does a treatment take?",
    "L-1002": "Please remove me from your list.",
}
for lead_id, body in replies.items():
    d = agent.handle_reply(lead_id, body)
    print(f"{lead_id}  intent={d.intent.value:<12} -> {d.action:<10} {d.reason}")

print("\n=== SDR task queue ===")
for t in crm.tasks:
    print(f"- {t['owner']}: {t['title']}")
print(f"\nSuppression list: {sorted(crm.suppression)}")
