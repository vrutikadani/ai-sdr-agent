"""Core data types for the first-touch SDR agent."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ReplyIntent(str, Enum):
    INTERESTED = "interested"          # wants a call, pricing, demo
    QUESTION = "question"              # asks something; agent can answer
    NOT_NOW = "not_now"                # timing objection, nurture later
    NOT_A_FIT = "not_a_fit"            # wrong person / no need
    UNSUBSCRIBE = "unsubscribe"        # opt-out, must be honored
    OUT_OF_OFFICE = "out_of_office"    # auto-reply, ignore and wait
    BOUNCE = "bounce"                  # delivery failure
    UNCLEAR = "unclear"                # route to a human


class LeadStatus(str, Enum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"            # handed to an SDR
    NURTURE = "nurture"
    DISQUALIFIED = "disqualified"
    OPTED_OUT = "opted_out"
    NEEDS_REVIEW = "needs_review"      # human must look


@dataclass
class Lead:
    lead_id: str
    first_name: str
    last_name: str
    email: str
    company: str
    specialty: str = ""                # e.g. Dermatology, Med Spa
    state: str = ""
    form_message: str = ""
    source: str = "web_form"
    status: LeadStatus = LeadStatus.NEW
    score: int = 0
    score_reasons: list[str] = field(default_factory=list)
    thread: list[dict] = field(default_factory=list)  # [{"role": "agent"|"lead", "body": str}]
    owner: Optional[str] = None


@dataclass
class Decision:
    """What the agent decided to do next, and why. Every decision is logged."""
    lead_id: str
    action: str                        # send_email | handoff | nurture | stop | review
    reason: str
    draft: Optional[str] = None
    intent: Optional[ReplyIntent] = None
    requires_approval: bool = True
