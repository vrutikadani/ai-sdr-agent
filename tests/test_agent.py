import pytest

from sdr_agent import InMemoryCRM, Lead, LeadStatus, ReplyIntent, RuleBasedClient, SDRAgent
from sdr_agent.guardrails import check_draft
from sdr_agent.qualify import score_lead


def make(**kw):
    base = dict(lead_id="L1", first_name="Ana", last_name="Diaz", email="ana@clinic.com",
                company="Diaz Derm", specialty="Dermatology", form_message="Tell me more.")
    base.update(kw)
    return Lead(**base)


@pytest.fixture
def agent():
    return SDRAgent(InMemoryCRM(), RuleBasedClient(), mode="draft")


def test_strong_lead_goes_straight_to_sdr(agent):
    d = agent.handle_new_lead(make(form_message="Need pricing and financing, adding a new service asap"))
    assert d.action == "handoff"
    assert agent.crm.leads["L1"].status == LeadStatus.QUALIFIED
    assert agent.crm.tasks and agent.crm.tasks[0]["context"]  # SDR sees the reasons


def test_medium_lead_gets_first_touch_queued_for_approval(agent):
    d = agent.handle_new_lead(make())
    assert d.action == "send_email" and d.requires_approval
    assert "Ana" in d.draft


def test_job_seekers_are_disqualified(agent):
    d = agent.handle_new_lead(make(email="x@gmail.com", specialty="", form_message="student looking for a job"))
    assert d.action == "stop"


def test_unsubscribe_is_always_honored(agent):
    agent.handle_new_lead(make())
    d = agent.handle_reply("L1", "Please unsubscribe me")
    assert d.intent == ReplyIntent.UNSUBSCRIBE
    assert "ana@clinic.com" in agent.crm.suppression
    # a new form fill from the same address must not be emailed
    d2 = agent.handle_new_lead(make(lead_id="L2"))
    assert d2.action == "stop"


class ConfidentlyWrongModel(RuleBasedClient):
    """A model that labels everything 'interested' with high confidence."""
    def classify_reply(self, body):
        return ReplyIntent.INTERESTED, 0.99


def test_opt_out_rule_runs_before_the_model():
    agent = SDRAgent(InMemoryCRM(), ConfidentlyWrongModel(), mode="draft")
    agent.handle_new_lead(make())
    d = agent.handle_reply("L1", "Sounds interesting but please remove me from this list")
    assert d.intent == ReplyIntent.UNSUBSCRIBE and d.action == "stop"
    assert not agent.crm.tasks  # no SDR handoff despite the model's label


def test_interested_reply_hands_off(agent):
    agent.handle_new_lead(make())
    assert agent.handle_reply("L1", "Yes let's book a call").action == "handoff"


def test_unclear_reply_goes_to_human(agent):
    agent.handle_new_lead(make())
    assert agent.handle_reply("L1", "hmm").action == "review"


def test_bounce_suppresses_address(agent):
    agent.handle_new_lead(make())
    agent.handle_reply("L1", "Delivery has failed to these recipients")
    assert "ana@clinic.com" in agent.crm.suppression


def test_guardrail_catches_wrong_recipient_draft():
    lead = make()
    problems = check_draft(lead, "Hi Maria, thanks for reaching out.", set())
    assert any("wrong-recipient" in p for p in problems)


def test_guardrail_catches_banned_claims_and_templates():
    lead = make()
    problems = check_draft(lead, "Hi Ana, guaranteed results {{discount}}", set())
    assert any("banned" in p for p in problems)
    assert any("template" in p for p in problems)


def test_scoring_is_explainable():
    lead = score_lead(make(form_message="can I get a demo and a quote"))
    assert lead.score >= 50
    assert any("pricing" in r for r in lead.score_reasons)


def test_duplicate_delivery_does_not_double_handoff(agent):
    agent.handle_new_lead(make())
    first = agent.handle_reply("L1", "Yes let's book a call", message_id="<msg-1@clinic.com>")
    again = agent.handle_reply("L1", "Yes let's book a call", message_id="<msg-1@clinic.com>")
    assert first.action == "handoff" and again.action == "skip"
    assert len(agent.crm.tasks) == 1


def _sent_id(agent, lead_id):
    return agent.crm.leads[lead_id].thread[0]["message_id"]


def test_reply_from_colleague_matches_by_thread(agent):
    agent.handle_new_lead(make())
    d = agent.handle_inbound({"from": "office.manager@clinic.com", "body": "Yes, book a call please",
                              "message_id": "<r1@clinic.com>", "in_reply_to": _sent_id(agent, "L1")})
    assert d.action == "handoff" and d.lead_id == "L1"


def test_forwarded_thread_matches_by_references(agent):
    agent.handle_new_lead(make())
    refs = f"<unrelated@x.com> {_sent_id(agent, 'L1')}"
    d = agent.handle_inbound({"from": "dr.diaz@othermail.com", "body": "Is it cleared for estheticians?",
                              "message_id": "<r2@x.com>", "in_reply_to": "<fwd@x.com>", "references": refs})
    assert d is not None and d.lead_id == "L1"


def test_unknown_sender_without_thread_goes_to_a_person(agent):
    agent.handle_new_lead(make())
    assert agent.handle_inbound({"from": "stranger@else.com", "body": "pricing?", "message_id": "<r3@e.com>"}) is None
    assert len(agent.unmatched) == 1 and not agent.crm.tasks


def test_shared_address_is_ambiguous_not_guessed(agent):
    agent.handle_new_lead(make(lead_id="L1", email="front.desk@group.com"))
    agent.handle_new_lead(make(lead_id="L2", email="front.desk@group.com", first_name="Ana"))
    assert agent.handle_inbound({"from": "front.desk@group.com", "body": "yes call us", "message_id": "<r4@g.com>"}) is None
