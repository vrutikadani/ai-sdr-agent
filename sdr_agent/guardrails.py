"""Safety checks that run before any email can leave the system.

In production the pause signal was simple: a wrong recipient or a wrong draft.
These checks make both of those hard to ship.
"""
from __future__ import annotations

import re

from .models import Lead

BANNED_PHRASES = ["guarantee", "100% results", "risk-free", "act now", "limited time"]
MAX_WORDS = 160


def check_draft(lead: Lead, draft: str, suppression: set[str]) -> list[str]:
    """Return a list of problems. Empty list = safe to send (or to queue for approval)."""
    problems: list[str] = []

    if lead.email.lower() in suppression:
        problems.append("recipient is on the suppression (opt-out) list")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", lead.email.lower()):
        problems.append("recipient email is malformed")
    if lead.first_name and lead.first_name.lower() not in draft.lower():
        problems.append("draft does not greet the lead by name (possible wrong-recipient draft)")

    # Make sure no *other* lead's name or company leaked into this draft.
    for other in re.findall(r"\{\{.*?\}\}", draft):
        problems.append(f"unfilled template variable {other}")

    lowered = draft.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            problems.append(f"banned claim: '{phrase}'")

    if len(draft.split()) > MAX_WORDS:
        problems.append(f"draft longer than {MAX_WORDS} words")
    return problems
