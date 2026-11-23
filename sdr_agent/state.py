"""Lead state machine. The agent may only move a lead along these edges.

OPTED_OUT and DISQUALIFIED are terminal for the agent: no reply, retry or
re-submitted form can move a lead out of them. A person can still change the
status in the CRM by hand.

QUALIFIED means an SDR owns the lead. The agent will not email it, nurture it,
send it to review, or create a second task for it. It can still record an
opt-out or a hard bounce.

A blocked transition is not applied. The pipeline logs it in `SDRAgent.blocked`.
"""
from __future__ import annotations

from .models import LeadStatus as S

TERMINAL = {S.OPTED_OUT, S.DISQUALIFIED}

_OPEN = frozenset({S.PENDING_APPROVAL, S.CONTACTED, S.QUALIFIED, S.NURTURE,
                   S.DISQUALIFIED, S.OPTED_OUT, S.NEEDS_REVIEW})

ALLOWED: dict[S, frozenset[S]] = {
    S.NEW: _OPEN,
    S.PENDING_APPROVAL: _OPEN,
    S.CONTACTED: _OPEN,
    S.NURTURE: _OPEN,
    S.NEEDS_REVIEW: _OPEN,
    S.QUALIFIED: frozenset({S.DISQUALIFIED, S.OPTED_OUT}),
    S.OPTED_OUT: frozenset(),
    S.DISQUALIFIED: frozenset(),
}


def can_transition(current: S, target: S) -> bool:
    return target in ALLOWED[current]
