"""The agent loop: score -> first touch -> classify replies -> hand off or stop.

Modes:
  * "draft"      : every email is queued as PENDING_APPROVAL; a person calls approve()
  * "autonomous" : clean drafts send on their own; anything a guardrail flags still waits

Every inbound event (form webhook, reply) goes through the same three rules:
  1. Skip it if the idempotency store says it was already fully processed.
  2. Only move the lead along an edge in `state.ALLOWED`; log anything else as blocked.
  3. Mark the event done only after its CRM writes succeeded. A CRMWriteError sends
     the lead to review and records the failure in `outbox`. Any other error (for
     example the classifier timing out) is raised, nothing is marked, and a retry
     of the same event runs again.
"""
from __future__ import annotations

import re
from typing import Optional

from .crm import CRMWriteError, InMemoryCRM
from .drafter import answer_question, first_touch
from .guardrails import check_draft
from .idempotency import IdempotencyStore, form_key, reply_key
from .llm import LLMClient, validate_classification
from .models import Action, Decision, Lead, LeadStatus, ReplyIntent
from .qualify import is_qualified, score_lead
from .state import can_transition
from .threads import ThreadIndex

OPT_OUT = re.compile(
    r"\b(unsubscribe|remove me|stop emailing|opt[- ]?out|take me off"
    r"|(do not|don'?t) (contact|email|e-mail|message))\b"
    r"|^\s*stop[.!]?\s*$",            # a bare "STOP" is the most common opt-out of all
    re.I,
)
CONFIDENCE_FLOOR = 0.6
DISQUALIFY_FLOOR = 10  # below this, do not spend an email
MODES = frozenset({"draft", "autonomous"})
_FORM_FIELDS = ("first_name", "last_name", "email", "company", "specialty", "state", "form_message", "source")


class SDRAgent:
    def __init__(self, crm: InMemoryCRM, llm: LLMClient, mode: str = "draft",
                 store: Optional[IdempotencyStore] = None):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {sorted(MODES)}, got {mode!r}")
        self.crm, self.llm, self.mode = crm, llm, mode
        self.store = store if store is not None else IdempotencyStore()
        self.log: list[Decision] = []
        self.blocked: list[dict] = []     # transitions the state machine refused
        self.outbox: list[dict] = []      # failed CRM writes, for a person or a replay
        self.drafts: dict[str, dict] = {}  # draft_id -> draft (pending | sent | void)
        self.sent: list[dict] = []        # stand-in for a mail provider: what was "sent"
        self.threads = ThreadIndex()
        self.unmatched: list[dict] = []   # inbound mail no lead could be matched to
        self._sent_count = 0
        self._draft_count = 0

    # --- bookkeeping ------------------------------------------------------------
    def _record(self, d: Decision) -> Decision:
        self.log.append(d)
        lead = self.crm.leads.get(d.lead_id)
        if lead is not None:
            try:
                self.crm.log_note(lead, f"[AI agent] {d.action}: {d.reason}")
            except CRMWriteError as e:  # the decision stands; only the audit note is missing
                self.outbox.append({"lead_id": d.lead_id, "op": e.op, "error": e.detail, "event": None})
        return d

    def _skip(self, lead_id: str) -> Decision:
        return Decision(lead_id, Action.SKIP, "duplicate delivery of an already-processed event",
                        requires_approval=False)

    def _blocked(self, lead: Lead, target: LeadStatus, why: str, intent=None) -> Decision:
        self.blocked.append({"lead_id": lead.lead_id, "from": lead.status.value, "to": target.value, "why": why})
        return self._record(Decision(lead.lead_id, Action.BLOCKED,
                                     f"blocked transition {lead.status.value} -> {target.value} ({why})",
                                     intent=intent, requires_approval=False))

    def _crm_failed(self, lead: Lead, err: CRMWriteError, event: str, payload: dict) -> Decision:
        self.outbox.append({"lead_id": lead.lead_id, "op": err.op, "error": err.detail,
                            "event": event, "payload": payload})
        if can_transition(lead.status, LeadStatus.NEEDS_REVIEW):
            lead.status = LeadStatus.NEEDS_REVIEW  # local only: the CRM is what just failed
        d = Decision(lead.lead_id, Action.REVIEW, f"CRM write failed ({err.op}): {err.detail}")
        self.log.append(d)
        return d

    def _go(self, lead: Lead, target: LeadStatus, action: Action, reason: str, intent=None,
            draft: Optional[str] = None, requires_approval: bool = False) -> Decision:
        """Move the lead to `target` through the CRM, or log a blocked transition."""
        if not can_transition(lead.status, target):
            return self._blocked(lead, target, reason, intent)
        self.crm.update_status(lead, target)
        return self._record(Decision(lead.lead_id, action, reason, draft, intent, requires_approval))

    # --- side effects -------------------------------------------------------------
    def _handoff(self, lead: Lead, reason: str, intent=None) -> Decision:
        if not can_transition(lead.status, LeadStatus.QUALIFIED):
            return self._blocked(lead, LeadStatus.QUALIFIED, reason, intent)
        owner, before = self.crm.next_owner(), lead.status
        self.crm.update_status(lead, LeadStatus.QUALIFIED)  # repeatable write first
        try:                                                 # the write that must not repeat goes last
            self.crm.create_task(lead, owner, f"Call {lead.first_name} {lead.last_name} ({lead.company})"
                                 " - qualified by AI agent", lead.score_reasons)
        except CRMWriteError:
            lead.status = before  # no task exists, so the lead is not really handed off
            raise
        lead.owner = owner
        return self._record(Decision(lead.lead_id, Action.HANDOFF, f"{reason} - assigned {owner}",
                                     intent=intent, requires_approval=False))

    def _email(self, lead: Lead, draft: str, reason: str, intent=None) -> Decision:
        target = LeadStatus.PENDING_APPROVAL if self.mode == "draft" else LeadStatus.CONTACTED
        if not can_transition(lead.status, target):
            return self._blocked(lead, target, reason, intent)
        problems = check_draft(lead, draft, self.crm.suppression)
        if problems:
            return self._go(lead, LeadStatus.NEEDS_REVIEW, Action.REVIEW, "; ".join(problems), intent,
                            draft=draft, requires_approval=True)
        if self.mode == "autonomous":
            return self._send(lead, draft, reason, intent)
        self.crm.update_status(lead, LeadStatus.PENDING_APPROVAL)
        self._draft_count += 1
        draft_id = f"draft-{lead.lead_id}-{self._draft_count}"
        self.drafts[draft_id] = {"lead_id": lead.lead_id, "body": draft, "reason": reason,
                                 "intent": intent, "state": "pending"}
        return self._record(Decision(lead.lead_id, Action.QUEUE_FOR_APPROVAL, reason, draft, intent,
                                     requires_approval=True, draft_id=draft_id))

    def _send(self, lead: Lead, draft: str, reason: str, intent=None, approved_by: Optional[str] = None) -> Decision:
        if not can_transition(lead.status, LeadStatus.CONTACTED):
            return self._blocked(lead, LeadStatus.CONTACTED, reason, intent)
        self.crm.update_status(lead, LeadStatus.CONTACTED)  # if this fails, nothing was sent
        self._sent_count += 1
        message_id = f"<{lead.lead_id}.{self._sent_count}@agent.local>"
        self.sent.append({"lead_id": lead.lead_id, "to": lead.email, "message_id": message_id,
                          "body": draft, "approved_by": approved_by})
        lead.thread.append({"role": "agent", "body": draft, "message_id": message_id})
        self.threads.record_sent(message_id, lead.lead_id)
        if approved_by:
            reason = f"{reason}; approved by {approved_by}"
        return self._record(Decision(lead.lead_id, Action.SEND_EMAIL, reason, draft, intent,
                                     requires_approval=False))

    # --- step 1: new inbound lead -------------------------------------------------
    def handle_new_lead(self, lead: Lead, submission_id: Optional[str] = None) -> Decision:
        """Process one form submission. Pass the form's submission id so a webhook retry
        can't create a second SDR task or a second email."""
        key = form_key(lead.lead_id, submission_id, lead.email, lead.form_message)
        if self.store.is_done(key):
            return self._skip(lead.lead_id)
        existing = self.crm.leads.get(lead.lead_id)
        if existing is not None:  # re-submission: keep thread, status and owner
            for name in _FORM_FIELDS:
                setattr(existing, name, getattr(lead, name))
            lead = existing
        else:
            self.crm.upsert(lead)
        self.threads.register_lead(lead.lead_id, lead.email)
        score_lead(lead)
        try:
            d = self._intake(lead)
        except CRMWriteError as e:
            return self._crm_failed(lead, e, key, {"submission_id": submission_id})
        self.store.mark_done(key, d.action.value)
        return d

    def _intake(self, lead: Lead) -> Decision:
        if lead.email.lower() in self.crm.suppression:
            return self._go(lead, LeadStatus.OPTED_OUT, Action.STOP, "on suppression list")
        if lead.score < DISQUALIFY_FLOOR:
            return self._go(lead, LeadStatus.DISQUALIFIED, Action.STOP, f"score {lead.score} below floor")
        if is_qualified(lead):
            return self._handoff(lead, f"score {lead.score} - strong buying signals")
        return self._email(lead, first_touch(lead, self.llm), f"first touch (score {lead.score})")

    # --- step 1b: a person approves a queued draft -------------------------------
    def approve(self, draft_id: str, approver: str) -> Decision:
        """Send a PENDING_APPROVAL draft. Raises KeyError for an unknown draft id."""
        entry = self.drafts[draft_id]
        lead = self.crm.leads[entry["lead_id"]]
        if entry["state"] != "pending":
            return Decision(lead.lead_id, Action.SKIP, f"draft already {entry['state']}", requires_approval=False)
        if lead.status != LeadStatus.PENDING_APPROVAL:
            entry["state"] = "void"  # the lead moved on (opted out, handed off, ...)
            return self._blocked(lead, LeadStatus.CONTACTED, f"draft {draft_id} is stale", entry["intent"])
        problems = check_draft(lead, entry["body"], self.crm.suppression)  # suppression may have changed
        try:
            if problems:
                entry["state"] = "void"
                return self._go(lead, LeadStatus.NEEDS_REVIEW, Action.REVIEW, "; ".join(problems),
                                entry["intent"], draft=entry["body"], requires_approval=True)
            d = self._send(lead, entry["body"], entry["reason"], entry["intent"], approved_by=approver)
        except CRMWriteError as e:  # draft stays pending; approving again retries
            return self._crm_failed(lead, e, f"approve:{draft_id}", {"draft_id": draft_id})
        entry["state"] = "sent"
        return d

    # --- step 2a: an email arrived; find whose it is -----------------------------
    def handle_inbound(self, email: dict) -> Optional[Decision]:
        """email: {"from", "body", "message_id"?, "in_reply_to"?, "references"?}.
        Returns the reply decision, or None if the email couldn't be matched to a lead
        (it goes to `unmatched` for a person to sort)."""
        r = self.threads.resolve(email["from"], email.get("in_reply_to", ""), email.get("references", ""))
        if r.lead_id is None:
            self.unmatched.append({**email, "reason": "no thread match and sender is not exactly one known lead"})
            return None
        return self.handle_reply(r.lead_id, email["body"], message_id=email.get("message_id"),
                                 sender=email["from"])

    # --- step 2b: the lead replied ------------------------------------------------
    def handle_reply(self, lead_id: str, body: str, message_id: Optional[str] = None,
                     sender: str = "") -> Decision:
        """Process one inbound reply. Keyed on Message-ID, or on a hash of lead + sender +
        normalized body when there is none, so a webhook retry is skipped."""
        key = reply_key(lead_id, message_id, sender, body)
        if self.store.is_done(key):
            return self._skip(lead_id)
        lead = self.crm.leads[lead_id]
        entry = {"role": "lead", "body": body, "message_id": message_id}
        lead.thread.append(entry)
        try:
            d = self._classify_and_act(lead, body)
        except CRMWriteError as e:
            lead.thread.remove(entry)
            return self._crm_failed(lead, e, key, {"body": body, "message_id": message_id, "sender": sender})
        except Exception:
            lead.thread.remove(entry)  # not marked done; the retry starts clean
            raise
        self.store.mark_done(key, d.action.value)
        return d

    def _classify_and_act(self, lead: Lead, body: str) -> Decision:
        # Opt-out language is matched by rule before the model sees the reply, so a
        # confident-but-wrong label can never override an unsubscribe.
        if OPT_OUT.search(body):
            intent, conf = ReplyIntent.UNSUBSCRIBE, 1.0
        else:
            intent, conf = validate_classification(self.llm.classify_reply(body))

        if intent == ReplyIntent.UNSUBSCRIBE:  # always honored, whatever the lead's status
            self.crm.suppression.add(lead.email.lower())  # local send gate first, even if the CRM write fails
            self.crm.opt_out(lead)
            if lead.status == LeadStatus.OPTED_OUT:
                return self._record(Decision(lead.lead_id, Action.STOP, "opt-out already recorded",
                                             intent=intent, requires_approval=False))
            if not can_transition(lead.status, LeadStatus.OPTED_OUT):
                self._blocked(lead, LeadStatus.OPTED_OUT, "opt-out recorded; status left as is", intent)
                return self._record(Decision(lead.lead_id, Action.STOP, "opt-out honored", intent=intent,
                                             requires_approval=False))
            return self._go(lead, LeadStatus.OPTED_OUT, Action.STOP, "opt-out honored", intent)
        if conf < CONFIDENCE_FLOOR or intent == ReplyIntent.UNCLEAR:
            return self._go(lead, LeadStatus.NEEDS_REVIEW, Action.REVIEW, f"low confidence ({conf:.2f})",
                            intent, requires_approval=True)
        if intent == ReplyIntent.INTERESTED:
            return self._handoff(lead, "interested reply", intent)
        if intent == ReplyIntent.QUESTION:
            return self._email(lead, answer_question(lead, self.llm), "answered question, asked for a call", intent)
        if intent == ReplyIntent.NOT_NOW:
            return self._go(lead, LeadStatus.NURTURE, Action.NURTURE, "timing objection", intent)
        if intent == ReplyIntent.NOT_A_FIT:
            return self._go(lead, LeadStatus.DISQUALIFIED, Action.STOP, "not a fit", intent)
        if intent == ReplyIntent.BOUNCE:
            self.crm.suppress(lead, "hard_bounce")
            return self._go(lead, LeadStatus.DISQUALIFIED, Action.STOP, "hard bounce - suppressed", intent)
        # out of office: do nothing, wait for the real reply
        return self._record(Decision(lead.lead_id, Action.WAIT, "auto-reply", intent=intent, requires_approval=False))
