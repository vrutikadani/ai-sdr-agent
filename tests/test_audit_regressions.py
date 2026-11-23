"""Regression tests for bugs found in an audit. Each test failed before its fix.

New modules are imported inside the tests so one missing module does not
stop the rest of the file from running.
"""
import math

import pytest

from sdr_agent import InMemoryCRM, Lead, LeadStatus, ReplyIntent, RuleBasedClient, SDRAgent
from sdr_agent.guardrails import check_draft


def make(**kw):
    base = dict(lead_id="L1", first_name="Ana", last_name="Diaz", email="ana@diazderm.example",
                company="Diaz Derm", specialty="Dermatology", form_message="Tell me more.")
    base.update(kw)
    return Lead(**base)


STRONG = "Need pricing and financing, adding a new service asap"


@pytest.fixture
def agent():
    return SDRAgent(InMemoryCRM(), RuleBasedClient(), mode="draft")


@pytest.fixture
def auto():
    return SDRAgent(InMemoryCRM(), RuleBasedClient(), mode="autonomous")


# --- 1. lead state machine ---------------------------------------------------

def test_reply_after_opt_out_is_blocked(auto):
    auto.handle_new_lead(make())
    auto.handle_reply("L1", "Please unsubscribe me", message_id="<m1@x.example>")
    d = auto.handle_reply("L1", "Yes book a call", message_id="<m2@x.example>")
    assert d.action == "blocked"
    assert auto.crm.leads["L1"].status == LeadStatus.OPTED_OUT
    assert not auto.crm.tasks
    assert auto.blocked and auto.blocked[-1]["to"] == "qualified"


def test_reply_after_disqualified_is_blocked(auto):
    auto.handle_new_lead(make())
    auto.handle_reply("L1", "Not interested, we already have one", message_id="<m1@x.example>")
    d = auto.handle_reply("L1", "Actually, can we book a demo?", message_id="<m2@x.example>")
    assert d.action == "blocked"
    assert auto.crm.leads["L1"].status == LeadStatus.DISQUALIFIED
    assert not auto.crm.tasks


def test_late_reply_after_handoff_does_not_move_lead_or_add_task(auto):
    auto.handle_new_lead(make())
    auto.handle_reply("L1", "Yes let's book a call", message_id="<m1@x.example>")
    late = auto.handle_reply("L1", "Not right now, maybe next quarter", message_id="<m0@x.example>")
    again = auto.handle_reply("L1", "Sounds good, call me", message_id="<m3@x.example>")
    assert late.action == "blocked" and again.action == "blocked"
    assert auto.crm.leads["L1"].status == LeadStatus.QUALIFIED
    assert len(auto.crm.tasks) == 1


def test_terminal_states_have_no_exits():
    from sdr_agent.state import ALLOWED, TERMINAL, can_transition
    assert TERMINAL == {LeadStatus.OPTED_OUT, LeadStatus.DISQUALIFIED}
    for t in TERMINAL:
        assert ALLOWED[t] == frozenset()
        assert not any(can_transition(t, s) for s in LeadStatus)


# --- 2. duplicate form webhook -----------------------------------------------

def test_duplicate_form_webhook_creates_one_task(agent):
    first = agent.handle_new_lead(make(form_message=STRONG), submission_id="sub-1")
    again = agent.handle_new_lead(make(form_message=STRONG), submission_id="sub-1")
    assert first.action == "handoff" and again.action == "skip"
    assert len(agent.crm.tasks) == 1


def test_duplicate_form_webhook_keeps_thread_history(auto):
    auto.handle_new_lead(make(), submission_id="sub-1")
    thread = list(auto.crm.leads["L1"].thread)
    assert len(thread) == 1
    auto.handle_new_lead(make(), submission_id="sub-1")
    assert auto.crm.leads["L1"].thread == thread


# --- 3. idempotency is recorded after side effects ---------------------------

class FlakyClassifier(RuleBasedClient):
    """Raises on the first call, like a timeout from the model API."""
    def __init__(self):
        self.calls = 0

    def classify_reply(self, body):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("model timeout")
        return super().classify_reply(body)


def test_classifier_error_does_not_lose_the_reply():
    agent = SDRAgent(InMemoryCRM(), FlakyClassifier(), mode="autonomous")
    agent.handle_new_lead(make())
    with pytest.raises(RuntimeError):
        agent.handle_reply("L1", "Yes let's book a call", message_id="<m1@x.example>")
    retry = agent.handle_reply("L1", "Yes let's book a call", message_id="<m1@x.example>")
    assert retry.action == "handoff"
    assert len(agent.crm.tasks) == 1


def test_reply_without_message_id_is_deduped(auto):
    auto.handle_new_lead(make())
    email = {"from": "ana@diazderm.example", "body": "Yes, book a call please"}
    first = auto.handle_inbound(dict(email))
    again = auto.handle_inbound(dict(email, body="  yes, BOOK a call   please "))
    assert first.action == "handoff" and again.action == "skip"
    assert len(auto.crm.tasks) == 1


def test_idempotency_survives_restart_with_file_store(tmp_path):
    from sdr_agent.idempotency import IdempotencyStore
    path = tmp_path / "events.db"
    crm = InMemoryCRM()
    a1 = SDRAgent(crm, RuleBasedClient(), mode="autonomous", store=IdempotencyStore(path))
    a1.handle_new_lead(make())
    a1.handle_reply("L1", "Yes let's book a call", message_id="<m1@x.example>")
    a2 = SDRAgent(crm, RuleBasedClient(), mode="autonomous", store=IdempotencyStore(path))
    assert a2.handle_reply("L1", "Yes let's book a call", message_id="<m1@x.example>").action == "skip"
    assert len(crm.tasks) == 1


# --- 4. CRM writes are failure-aware ------------------------------------------

def _failing_crm(fail_ops, times=None):
    from sdr_agent.crm import CRMWriteError

    class FailingCRM(InMemoryCRM):
        remaining = times

        def _maybe_fail(self, op, lead):
            if op in fail_ops and (self.remaining is None or self.remaining > 0):
                if self.remaining is not None:
                    self.remaining -= 1
                raise CRMWriteError(op, lead.lead_id, "HTTP 503")

        def create_task(self, lead, owner, title, context):
            self._maybe_fail("create_task", lead)
            return super().create_task(lead, owner, title, context)

        def opt_out(self, lead):
            self._maybe_fail("opt_out", lead)
            return super().opt_out(lead)

    return FailingCRM()


def test_failed_crm_write_goes_to_review_not_success():
    agent = SDRAgent(_failing_crm({"create_task"}), RuleBasedClient(), mode="autonomous")
    agent.handle_new_lead(make())
    d = agent.handle_reply("L1", "Yes let's book a call", message_id="<m1@x.example>")
    assert d.action == "review"
    assert agent.crm.leads["L1"].status == LeadStatus.NEEDS_REVIEW
    assert not agent.crm.tasks
    assert agent.outbox and agent.outbox[0]["op"] == "create_task"


def test_failed_crm_write_can_be_retried():
    agent = SDRAgent(_failing_crm({"create_task"}, times=1), RuleBasedClient(), mode="autonomous")
    agent.handle_new_lead(make())
    agent.handle_reply("L1", "Yes let's book a call", message_id="<m1@x.example>")
    retry = agent.handle_reply("L1", "Yes let's book a call", message_id="<m1@x.example>")
    assert retry.action == "handoff"
    assert len(agent.crm.tasks) == 1


def test_failed_opt_out_write_still_never_emails_again():
    agent = SDRAgent(_failing_crm({"opt_out"}), RuleBasedClient(), mode="autonomous")
    agent.handle_new_lead(make())
    d = agent.handle_reply("L1", "Please unsubscribe me", message_id="<m1@x.example>")
    assert d.action == "review" and agent.outbox[0]["op"] == "opt_out"
    q = agent.handle_reply("L1", "What does it cost?", message_id="<m2@x.example>")
    assert q.action != "send_email"


class FakeResponse:
    def __init__(self, status):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"id": "1"}


class FakeSession:
    """Stands in for requests.Session. No network."""
    def __init__(self, status=200):
        self.status, self.calls, self.headers = status, [], {}

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json))
        return FakeResponse(self.status)


def test_hubspot_adapter_raises_on_http_error():
    from sdr_agent.crm import CRMWriteError, HubSpotCRM
    crm = HubSpotCRM(token="test", session=FakeSession(status=500))
    lead = make()
    crm.upsert(lead)
    with pytest.raises(CRMWriteError):
        crm.update_status(lead, LeadStatus.CONTACTED)
    assert lead.status == LeadStatus.NEW  # local copy not changed on a failed write


def test_opt_out_is_written_through_the_crm_adapter():
    from sdr_agent.crm import HubSpotCRM
    session = FakeSession()
    agent = SDRAgent(HubSpotCRM(token="test", session=session), RuleBasedClient(), mode="autonomous")
    agent.handle_new_lead(make())
    agent.handle_reply("L1", "Please unsubscribe me", message_id="<m1@x.example>")
    opt_out_writes = [c for c in session.calls if c[2] and "ai_agent_opted_out" in c[2].get("properties", {})]
    assert opt_out_writes
    assert not any("/crm/v3/objects/tasks" in c[1] for c in session.calls)
    assert any("/crm/v3/objects/notes" in c[1] for c in session.calls)


# --- 5. draft mode does not mark the lead contacted --------------------------

def test_draft_mode_queues_without_marking_contacted(agent):
    d = agent.handle_new_lead(make())
    lead = agent.crm.leads["L1"]
    assert d.action == "queue_for_approval" and d.requires_approval
    assert lead.status == LeadStatus.PENDING_APPROVAL
    assert lead.thread == []  # nothing was sent, so no Message-ID exists


def test_approve_sends_the_draft(agent):
    d = agent.handle_new_lead(make())
    sent = agent.approve(d.draft_id, approver="rep@example.com")
    lead = agent.crm.leads["L1"]
    assert sent.action == "send_email" and not sent.requires_approval
    assert lead.status == LeadStatus.CONTACTED
    assert lead.thread[0]["message_id"]
    assert agent.approve(d.draft_id, approver="rep@example.com").action == "skip"
    assert len(agent.sent) == 1


def test_approve_after_opt_out_does_not_send(agent):
    d = agent.handle_new_lead(make())
    agent.handle_reply("L1", "Please unsubscribe me", message_id="<m1@x.example>")
    assert agent.approve(d.draft_id, approver="rep@example.com").action == "blocked"
    assert agent.sent == []


# --- 6. guardrails --------------------------------------------------------------

def test_wrong_recipient_check_matches_whole_first_name():
    problems = check_draft(make(), "Hi Anabel, thanks for reaching out.", set())
    assert any("wrong-recipient" in p for p in problems)


@pytest.mark.parametrize("claim", ["It is FDA cleared.", "The device is $9,000.", "It is covered by insurance."])
def test_unapproved_claims_are_blocked(claim):
    problems = check_draft(make(), f"Hi Ana, {claim}", set())
    assert any("unapproved claim" in p for p in problems)


def test_approved_fact_passes_the_claims_check():
    problems = check_draft(make(), "Hi Ana, the list price is $9,000.", set(),
                           approved_facts=["the list price is $9,000"])
    assert problems == []


class ClaimingWriter(RuleBasedClient):
    def write_email(self, prompt):
        return prompt + "\n\nP.S. It is FDA cleared and covered by insurance."


def test_pipeline_blocks_draft_with_unapproved_claim():
    agent = SDRAgent(InMemoryCRM(), ClaimingWriter(), mode="autonomous")
    d = agent.handle_new_lead(make())
    assert d.action == "review" and "unapproved claim" in d.reason


# --- 7. live model is opt-in; classifier output is validated ------------------

def test_api_key_alone_does_not_switch_to_live_model(monkeypatch):
    import sdr_agent.llm as llm

    class FakeLive:
        pass

    monkeypatch.setattr(llm, "ClaudeClient", FakeLive)  # would be returned if picked
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("SDR_LIVE_LLM", raising=False)
    assert isinstance(llm.default_client(), RuleBasedClient)
    monkeypatch.setenv("SDR_LIVE_LLM", "1")
    assert isinstance(llm.default_client(), FakeLive)


class BadOutput(RuleBasedClient):
    def __init__(self, out):
        self.out = out

    def classify_reply(self, body):
        return self.out


@pytest.mark.parametrize("out", [("interested", 1.7), ("yes", 0.9), (ReplyIntent.INTERESTED, math.nan),
                                 (ReplyIntent.INTERESTED, -0.1), (ReplyIntent.INTERESTED, "high")])
def test_invalid_classifier_output_goes_to_review(out):
    agent = SDRAgent(InMemoryCRM(), BadOutput(out), mode="autonomous")
    agent.handle_new_lead(make())
    d = agent.handle_reply("L1", "Thursday works", message_id="<m1@x.example>")
    assert d.action == "review"
    assert not agent.crm.tasks


# --- 8. real errors, constrained actions --------------------------------------

def test_bad_mode_raises_value_error():
    with pytest.raises(ValueError):
        SDRAgent(InMemoryCRM(), RuleBasedClient(), mode="auto")


def test_decision_action_is_constrained():
    from sdr_agent.models import Decision
    with pytest.raises(ValueError):
        Decision("L1", "sned_email", "typo")
