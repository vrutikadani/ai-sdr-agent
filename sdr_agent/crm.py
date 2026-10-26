"""CRM adapter. `InMemoryCRM` mirrors the HubSpot calls the production agent made.

Swap in `HubSpotCRM` (same method names) to run against a real portal:
  PATCH /crm/v3/objects/contacts/{id}      -> update_lead
  POST  /crm/v3/objects/notes + associate  -> log_note
  POST  /crm/v3/objects/tasks              -> create_task
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .models import Lead, LeadStatus


@dataclass
class InMemoryCRM:
    leads: dict[str, Lead] = field(default_factory=dict)
    notes: list[dict] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)
    suppression: set[str] = field(default_factory=set)
    sdr_round_robin: list[str] = field(default_factory=lambda: ["SDR-East", "SDR-Central", "SDR-West"])
    _rr: int = 0

    def upsert(self, lead: Lead) -> None:
        self.leads[lead.lead_id] = lead

    def update_status(self, lead: Lead, status: LeadStatus) -> None:
        lead.status = status
        self.upsert(lead)

    def log_note(self, lead: Lead, body: str) -> None:
        self.notes.append({"lead_id": lead.lead_id, "body": body})

    def opt_out(self, lead: Lead) -> None:
        self.suppression.add(lead.email.lower())
        self.update_status(lead, LeadStatus.OPTED_OUT)

    def assign_sdr(self, lead: Lead) -> str:
        owner = self.sdr_round_robin[self._rr % len(self.sdr_round_robin)]
        self._rr += 1
        lead.owner = owner
        self.tasks.append({
            "lead_id": lead.lead_id, "owner": owner,
            "title": f"Call {lead.first_name} {lead.last_name} ({lead.company}) - qualified by AI agent",
            "context": lead.score_reasons,
        })
        self.update_status(lead, LeadStatus.QUALIFIED)
        return owner


class HubSpotCRM(InMemoryCRM):  # pragma: no cover - needs a live portal
    """Thin live adapter. Requires HUBSPOT_TOKEN (private app token)."""

    BASE = "https://api.hubapi.com"

    def __init__(self):
        super().__init__()
        import requests
        self._s = requests.Session()
        self._s.headers["Authorization"] = f"Bearer {os.environ['HUBSPOT_TOKEN']}"

    def update_status(self, lead: Lead, status: LeadStatus) -> None:
        super().update_status(lead, status)
        self._s.patch(f"{self.BASE}/crm/v3/objects/contacts/{lead.lead_id}",
                      json={"properties": {"ai_agent_status": status.value, "ai_lead_score": lead.score}})
