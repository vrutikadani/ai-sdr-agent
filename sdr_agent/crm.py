"""CRM adapters.

`InMemoryCRM` is what the tests, demo and eval use. It keeps leads, notes, tasks
and the suppression list in memory.

`HubSpotCRM` is an optional sketch of the same interface over the HubSpot REST
API. It has NOT been run against a live portal. Its HTTP calls are only
exercised in tests through a fake session (no network). It assumes custom
contact properties (`ai_agent_status`, `ai_lead_score`, `ai_agent_opted_out`,
`ai_agent_suppression_reason`) that a portal would have to create first.

Every write either succeeds or raises `CRMWriteError`. The pipeline treats a
raised error as "not done": the lead goes to review and the failure is kept in
the agent's outbox, so it is never counted as success.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import Lead, LeadStatus


class CRMWriteError(Exception):
    def __init__(self, op: str, lead_id: str, detail: str):
        super().__init__(f"{op} failed for {lead_id}: {detail}")
        self.op, self.lead_id, self.detail = op, lead_id, detail


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
        """Record the opt-out. Status is changed separately, through the state machine."""
        self.suppression.add(lead.email.lower())

    def suppress(self, lead: Lead, reason: str) -> None:
        """Stop all email to this address (e.g. a hard bounce)."""
        self.suppression.add(lead.email.lower())

    def next_owner(self) -> str:
        owner = self.sdr_round_robin[self._rr % len(self.sdr_round_robin)]
        self._rr += 1
        return owner

    def create_task(self, lead: Lead, owner: str, title: str, context: list[str]) -> dict:
        task = {"lead_id": lead.lead_id, "owner": owner, "title": title, "context": list(context)}
        self.tasks.append(task)
        return task


class HubSpotCRM(InMemoryCRM):
    """Optional live adapter (sketch, not tested against a real portal).

    Each method writes to HubSpot first and updates the local copy only if the
    write succeeded. Pass `session` to inject a fake for tests.
    Not covered: loading the suppression list from HubSpot at startup, HubSpot's
    subscription (communication preferences) API, mapping SDR names to owner ids.
    """

    BASE = "https://api.hubapi.com"
    NOTE_TO_CONTACT = 202   # HubSpot-defined association type ids
    TASK_TO_CONTACT = 204

    def __init__(self, token: str | None = None, session=None, timeout: float = 10.0):
        super().__init__()
        if session is None:
            import requests  # optional dependency, only for live use
            session = requests.Session()
        self._s = session
        self._s.headers["Authorization"] = f"Bearer {token or os.environ['HUBSPOT_TOKEN']}"
        self._timeout = timeout

    def _call(self, op: str, lead_id: str, method: str, path: str, payload: dict) -> dict:
        try:
            resp = self._s.request(method, f"{self.BASE}{path}", json=payload, timeout=self._timeout)
            resp.raise_for_status()
        except Exception as e:  # network error or non-2xx status
            raise CRMWriteError(op, lead_id, str(e)) from e
        try:
            return resp.json()
        except ValueError:
            return {}

    def _patch_contact(self, op: str, lead: Lead, props: dict) -> None:
        self._call(op, lead.lead_id, "PATCH", f"/crm/v3/objects/contacts/{lead.lead_id}", {"properties": props})

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _assoc(self, lead: Lead, type_id: int) -> list[dict]:
        return [{"to": {"id": lead.lead_id},
                 "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}]}]

    def update_status(self, lead: Lead, status: LeadStatus) -> None:
        self._patch_contact("update_status", lead, {"ai_agent_status": status.value, "ai_lead_score": lead.score})
        super().update_status(lead, status)

    def log_note(self, lead: Lead, body: str) -> None:
        self._call("log_note", lead.lead_id, "POST", "/crm/v3/objects/notes", {
            "properties": {"hs_note_body": body, "hs_timestamp": self._now()},
            "associations": self._assoc(lead, self.NOTE_TO_CONTACT)})
        super().log_note(lead, body)

    def opt_out(self, lead: Lead) -> None:
        self._patch_contact("opt_out", lead, {"ai_agent_opted_out": "true"})
        super().opt_out(lead)

    def suppress(self, lead: Lead, reason: str) -> None:
        self._patch_contact("suppress", lead, {"ai_agent_suppression_reason": reason})
        super().suppress(lead, reason)

    def create_task(self, lead: Lead, owner: str, title: str, context: list[str]) -> dict:
        self._call("create_task", lead.lead_id, "POST", "/crm/v3/objects/tasks", {
            "properties": {"hs_task_subject": title, "hs_task_status": "NOT_STARTED",
                           "hs_task_body": f"Suggested owner: {owner}\n" + "\n".join(context),
                           "hs_timestamp": self._now()},
            "associations": self._assoc(lead, self.TASK_TO_CONTACT)})
        return super().create_task(lead, owner, title, context)
