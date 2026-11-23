"""Run the agent end-to-end on synthetic leads. No API keys needed.

    python run_demo.py
"""
import json
from pathlib import Path

from sdr_agent import InMemoryCRM, Lead, SDRAgent, default_client

leads = [Lead(**row) for row in json.loads(Path("data/sample_leads.json").read_text())]
crm = InMemoryCRM()
agent = SDRAgent(crm, default_client(), mode="draft")

print("=== Step 1: new inbound leads (form webhooks) ===")
for lead in leads:
    d = agent.handle_new_lead(lead, submission_id=f"sub-{lead.lead_id}")
    print(f"{lead.lead_id}  score={lead.score:>3}  -> {d.action:<18} {d.reason}")

print("\n=== Step 1 again: the webhook is retried ===")
d = agent.handle_new_lead(leads[0], submission_id=f"sub-{leads[0].lead_id}")
print(f"{leads[0].lead_id}  -> {d.action:<18} {d.reason}")

print("\n=== Step 2: a person approves the queued drafts ===")
for draft_id, draft in list(agent.drafts.items()):
    if draft["state"] == "pending":
        d = agent.approve(draft_id, approver="reviewer@example.com")
        print(f"{draft['lead_id']}  -> {d.action:<18} {d.reason}")

print("\n=== Step 3: simulated replies ===")
replies = {
    "L-1003": "Sounds good, can we schedule a call Thursday?",
    "L-1005": "Is it FDA cleared and how long does a treatment take?",
    "L-1002": "Please remove me from your list.",
}
for i, (lead_id, body) in enumerate(replies.items()):
    d = agent.handle_reply(lead_id, body, message_id=f"<reply-{i}@example.com>")
    print(f"{lead_id}  intent={d.intent.value:<12} -> {d.action:<18} {d.reason}")

print("\n=== Step 4: the opted-out lead replies again ===")
d = agent.handle_reply("L-1002", "Actually, yes, book a call", message_id="<reply-9@example.com>")
print(f"L-1002  -> {d.action:<18} {d.reason}")

print("\n=== SDR task queue ===")
for t in crm.tasks:
    print(f"- {t['owner']}: {t['title']}")
print(f"\nDrafts waiting for approval: {[k for k, v in agent.drafts.items() if v['state'] == 'pending']}")
print(f"Suppression list: {sorted(crm.suppression)}")
