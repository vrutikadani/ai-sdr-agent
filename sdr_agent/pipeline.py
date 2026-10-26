"""The agent loop: score -> first touch -> classify replies -> hand off or stop.

Rollout modes (this is how it went live in production):
  * "draft"      : every email is queued for human approval (first 50+ drafts reviewed)
  * "autonomous" : clean drafts send on their own; anything a guardrail flags still waits
"""
from __future__ import annotations

import re
from typing import Optional

from .crm import InMemoryCRM
from .drafter import answer_question, first_touch
from .guardrails import check_draft
from .llm import LLMClient
from .models import Decision, Lead, LeadStatus, ReplyIntent
from .qualify import is_qualified, score_lead
from .threads import ThreadIndex

OPT_OUT = re.compile(
    r"\b(unsubscribe|remove me|stop emailing|opt[- ]?out|take me off"
    r"|(do not|don'?t) (contact|email|e-mail|message))\b"
    r"|^\s*stop[.!]?\s*$",            # a bare "STOP" is the most common opt-out of all
    re.I,
)
CONFIDENCE_FLOOR = 0.6
DISQUALIFY_FLOOR = 10  # below this, do not spend an email


class SDRAgent:
    def __init__(self, crm: InMemoryCRM, llm: LLMClient, mode: str = "draft"):
        assert mode in {"draft", "autonomous"}
        self.crm, self.llm, self.mode = crm, llm, mode
        self.log: list[Decision] = []
        self._processed: set[tuple[str, str]] = set()  # (lead_id, message_id) already handled
        self.threads = ThreadIndex()
        self.unmatched: list[dict] = []                 # inbound mail no lead could be matched to
        self._sent_count = 0

    def _record(self, d: Decision) -> Decision:
        self.log.append(d)
        self.crm.log_note(self.crm.leads[d.lead_id], f"[AI agent] {d.action}: {d.reason}")
        return d

    def _email(self, lead: Lead, draft: str, reason: str, intent=None) -> Decision:
        problems = check_draft(lead, draft, self.crm.suppression)
        if problems:
            self.crm.update_status(lead, LeadStatus.NEEDS_REVIEW)
            return self._record(Decision(lead.lead_id, "review", "; ".join(problems), draft, intent))
        self._sent_count += 1
        message_id = f"<{lead.lead_id}.{self._sent_count}@agent.local>"
        lead.thread.append({"role": "agent", "body": draft, "message_id": message_id})
        self.threads.record_sent(message_id, lead.lead_id)
        self.crm.update_status(lead, LeadStatus.CONTACTED)
        return self._record(Decision(lead.lead_id, "send_email", reason, draft, intent,
                                     requires_approval=(self.mode == "draft")))

    # --- step 1: new inbound lead -------------------------------------------------
    def handle_new_lead(self, lead: Lead) -> Decision:
        self.crm.upsert(lead)
        self.threads.register_lead(lead.lead_id, lead.email)
        score_lead(lead)
        if lead.email.lower() in self.crm.suppression:
            self.crm.update_status(lead, LeadStatus.OPTED_OUT)
            return self._record(Decision(lead.lead_id, "stop", "on suppression list", requires_approval=False))
        if lead.score < DISQUALIFY_FLOOR:
            self.crm.update_status(lead, LeadStatus.DISQUALIFIED)
            return self._record(Decision(lead.lead_id, "stop", f"score {lead.score} below floor",
                                         requires_approval=False))
        if is_qualified(lead):
            owner = self.crm.assign_sdr(lead)
            return self._record(Decision(lead.lead_id, "handoff",
                                         f"score {lead.score} - strong buying signals, sent straight to {owner}",
                                         requires_approval=False))
        return self._email(lead, first_touch(lead, self.llm), f"first touch (score {lead.score})")

    # --- step 2a: an email arrived; find whose it is -----------------------------
    def handle_inbound(self, email: dict) -> Optional[Decision]:
        """email: {"from", "body", "message_id", "in_reply_to"?, "references"?}.
        Returns the reply decision, or None if the email couldn't be matched to a lead
        (it goes to `unmatched` for a person to sort)."""
        r = self.threads.resolve(email["from"], email.get("in_reply_to", ""), email.get("references", ""))
        if r.lead_id is None:
            self.unmatched.append({**email, "reason": "no thread match and sender is not exactly one known lead"})
            return None
        return self.handle_reply(r.lead_id, email["body"], message_id=email.get("message_id"))

    # --- step 2b: the lead replied ------------------------------------------------
    def handle_reply(self, lead_id: str, body: str, message_id: str | None = None) -> Decision:
        """Process one inbound reply. Pass the email's Message-ID so a webhook retry or a
        re-run of the inbox sync can't create a second SDR task or a second email."""
        if message_id:
            key = (lead_id, message_id)
            if key in self._processed:
                return Decision(lead_id, "skip", "duplicate delivery of an already-processed reply",
                                requires_approval=False)
            self._processed.add(key)
        lead = self.crm.leads[lead_id]
        lead.thread.append({"role": "lead", "body": body})
        # Opt-out language is matched by rule before the model sees the reply, so a
        # confident-but-wrong label can never override an unsubscribe.
        if OPT_OUT.search(body):
            intent, conf = ReplyIntent.UNSUBSCRIBE, 1.0
        else:
            intent, conf = self.llm.classify_reply(body)

        if intent == ReplyIntent.UNSUBSCRIBE:  # always honored, regardless of confidence
            self.crm.opt_out(lead)
            return self._record(Decision(lead_id, "stop", "opt-out honored", intent=intent, requires_approval=False))
        if conf < CONFIDENCE_FLOOR or intent == ReplyIntent.UNCLEAR:
            self.crm.update_status(lead, LeadStatus.NEEDS_REVIEW)
            return self._record(Decision(lead_id, "review", f"low confidence ({conf:.2f})", intent=intent))
        if intent == ReplyIntent.INTERESTED:
            owner = self.crm.assign_sdr(lead)
            return self._record(Decision(lead_id, "handoff", f"interested reply - assigned {owner}",
                                         intent=intent, requires_approval=False))
        if intent == ReplyIntent.QUESTION:
            return self._email(lead, answer_question(lead, self.llm), "answered question, asked for a call", intent)
        if intent == ReplyIntent.NOT_NOW:
            self.crm.update_status(lead, LeadStatus.NURTURE)
            return self._record(Decision(lead_id, "nurture", "timing objection", intent=intent, requires_approval=False))
        if intent == ReplyIntent.NOT_A_FIT:
            self.crm.update_status(lead, LeadStatus.DISQUALIFIED)
            return self._record(Decision(lead_id, "stop", "not a fit", intent=intent, requires_approval=False))
        if intent == ReplyIntent.BOUNCE:
            self.crm.suppression.add(lead.email.lower())
            self.crm.update_status(lead, LeadStatus.DISQUALIFIED)
            return self._record(Decision(lead_id, "stop", "hard bounce - suppressed", intent=intent,
                                         requires_approval=False))
        # out of office: do nothing, wait for the real reply
        return self._record(Decision(lead_id, "wait", "auto-reply", intent=intent, requires_approval=False))
