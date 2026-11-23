"""Regression tests for the second review round (approval retry, stale drafts,
bounce suppression). Each test failed before its fix."""
from sdr_agent import InMemoryCRM, Lead, LeadStatus, RuleBasedClient, SDRAgent
from sdr_agent.crm import CRMWriteError, HubSpotCRM


def make(**kw):
    base = dict(lead_id="L1", first_name="Ana", last_name="Diaz", email="ana@diazderm.example",
                company="Diaz Derm", specialty="Dermatology", form_message="Tell me more.")
    base.update(kw)
    return Lead(**base)


class ContactedFailsOnce(InMemoryCRM):
    """The first write of status CONTACTED fails; every other write succeeds."""
    failed = False

    def update_status(self, lead, status):
        if status == LeadStatus.CONTACTED and not self.failed:
            self.failed = True
            raise CRMWriteError("update_status", lead.lead_id, "HTTP 503")
        super().update_status(lead, status)


def test_approval_retry_sends_after_crm_recovers():
    agent = SDRAgent(ContactedFailsOnce(), RuleBasedClient(), mode="draft")
    d = agent.handle_new_lead(make())
    first = agent.approve(d.draft_id, approver="rep@example.com")
    assert first.action == "review" and agent.sent == []
    assert agent.outbox and agent.outbox[-1]["op"] == "update_status"
    retry = agent.approve(d.draft_id, approver="rep@example.com")
    assert retry.action == "send_email"
    assert agent.crm.leads["L1"].status == LeadStatus.CONTACTED
    assert len(agent.sent) == 1


def test_failed_approval_still_blocks_send_after_opt_out():
    agent = SDRAgent(ContactedFailsOnce(), RuleBasedClient(), mode="draft")
    d = agent.handle_new_lead(make())
    agent.approve(d.draft_id, approver="rep@example.com")
    agent.handle_reply("L1", "Please unsubscribe me", message_id="<m1@x.example>")
    assert agent.approve(d.draft_id, approver="rep@example.com").action != "send_email"
    assert agent.sent == []


def test_superseded_draft_cannot_be_approved():
    agent = SDRAgent(InMemoryCRM(), RuleBasedClient(), mode="draft")
    a = agent.handle_new_lead(make())
    agent.handle_reply("L1", "Not right now, maybe next quarter", message_id="<m1@x.example>")
    b = agent.handle_reply("L1", "What models do you have?", message_id="<m2@x.example>")
    assert b.action == "queue_for_approval" and b.draft_id != a.draft_id
    old = agent.approve(a.draft_id, approver="rep@example.com")
    assert old.action != "send_email" and agent.sent == []
    new = agent.approve(b.draft_id, approver="rep@example.com")
    assert new.action == "send_email"
    assert [m["body"] for m in agent.sent] == [b.draft]


class _Resp:
    def __init__(self, status):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"id": "1"}


class SuppressFailsSession:
    """Fake requests.Session: the suppression PATCH fails, everything else succeeds."""
    def __init__(self):
        self.headers = {}

    def request(self, method, url, json=None, timeout=None):
        props = (json or {}).get("properties", {})
        return _Resp(503 if "ai_agent_suppression_reason" in props else 200)


def test_failed_bounce_write_still_never_emails_again():
    agent = SDRAgent(HubSpotCRM(token="test", session=SuppressFailsSession()), RuleBasedClient(),
                     mode="autonomous")
    agent.handle_new_lead(make())
    sent_before = len(agent.sent)
    d = agent.handle_reply("L1", "Delivery failed: mailbox not found", message_id="<m1@x.example>")
    assert d.action == "review" and agent.outbox[-1]["op"] == "suppress"
    q = agent.handle_reply("L1", "What models do you have?", message_id="<m2@x.example>")
    assert q.action != "send_email"
    assert len(agent.sent) == sent_before


def test_failed_bounce_write_blocks_approval_in_draft_mode():
    agent = SDRAgent(HubSpotCRM(token="test", session=SuppressFailsSession()), RuleBasedClient(),
                     mode="draft")
    d = agent.handle_new_lead(make())
    agent.handle_reply("L1", "Delivery failed: mailbox not found", message_id="<m1@x.example>")
    q = agent.handle_reply("L1", "What models do you have?", message_id="<m2@x.example>")
    for draft_id in {d.draft_id, q.draft_id} - {None}:
        assert agent.approve(draft_id, approver="rep@example.com").action != "send_email"
    assert agent.sent == []
