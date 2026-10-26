"""Email drafting. Templates keep the voice human; the LLM personalizes inside them."""
from __future__ import annotations

from .llm import LLMClient
from .models import Lead

FIRST_TOUCH = """Hi {first},

Thanks for reaching out about {topic}. I work with {specialty_phrase} practices like {company} that want to add a treatment patients ask for by name.

Two quick questions so I can point you to the right person:
1. Are you looking to add this in the next few months, or just exploring?
2. Would a short call with a specialist be helpful this week?

Best,
Alex
Practice Growth Team"""

ANSWER_FOLLOWUP = """Hi {first},

Great question. A specialist on our team can walk you through the details for {company}, including options that fit your budget.

Would Tuesday or Thursday work for a 15-minute call?

Best,
Alex"""


def _topic(lead: Lead) -> str:
    msg = lead.form_message.lower()
    if "pric" in msg or "cost" in msg:
        return "pricing"
    if "demo" in msg:
        return "a demo"
    return "our facial treatment platform"


def first_touch(lead: Lead, llm: LLMClient) -> str:
    template = FIRST_TOUCH.format(
        first=lead.first_name,
        topic=_topic(lead),
        specialty_phrase=(lead.specialty.lower() or "aesthetic"),
        company=lead.company,
    )
    return llm.write_email(template)


def answer_question(lead: Lead, llm: LLMClient) -> str:
    return llm.write_email(ANSWER_FOLLOWUP.format(first=lead.first_name, company=lead.company))
