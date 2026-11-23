"""Safety checks that run before any email can leave the system.

The two failures that matter most are a wrong recipient and a wrong draft.
These checks make both hard to ship.
"""
from __future__ import annotations

import re
from typing import Iterable

from .models import Lead

BANNED_PHRASES = ["guarantee", "100% results", "risk-free", "act now", "limited time"]
MAX_WORDS = 160

# Statements a sales email may only make if the exact wording is on an approved
# fact sheet (regulatory status, prices, insurance, clinical outcomes).
CLAIM_PATTERNS: dict[str, str] = {
    "regulatory": r"\bfda\b|\bcleared\b|\bclinically (?:proven|tested)\b",
    "price": r"\$\s?\d[\d,]*(?:\.\d+)?\s?k?\b|\b\d[\d,]*\s?(?:dollars|usd)\b",
    "insurance": r"\binsurance\b|\bcovered by\b|\breimburs\w*",
    "outcome": r"\b\d{1,3}\s?%|\bproven\b|\bpermanent results?\b",
}

# Fill from a fact sheet that legal / marketing approved. Empty means every
# claim above is blocked. Each entry is matched as an exact phrase (case and
# spacing ignored), and a claim passes only if it sits inside one.
APPROVED_FACTS: tuple[str, ...] = ()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def unapproved_claims(draft: str, approved_facts: Iterable[str] = APPROVED_FACTS) -> list[str]:
    text = _norm(draft)
    allowed: list[tuple[int, int]] = []
    for fact in approved_facts:
        f = _norm(fact)
        if f:
            allowed += [(m.start(), m.end()) for m in re.finditer(re.escape(f), text)]
    found = []
    for kind, pattern in CLAIM_PATTERNS.items():
        for m in re.finditer(pattern, text):
            if not any(a <= m.start() and m.end() <= b for a, b in allowed):
                found.append(f"unapproved claim ({kind}): '{m.group(0).strip()}'")
    return found


def check_draft(lead: Lead, draft: str, suppression: set[str],
                approved_facts: Iterable[str] = APPROVED_FACTS) -> list[str]:
    """Return a list of problems. Empty list = safe to send (or to queue for approval)."""
    problems: list[str] = []

    if lead.email.lower() in suppression:
        problems.append("recipient is on the suppression (opt-out) list")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", lead.email.lower()):
        problems.append("recipient email is malformed")
    # Whole-word match: "Anabel" must not pass for "Ana".
    if lead.first_name and not re.search(rf"\b{re.escape(lead.first_name)}\b", draft, re.I):
        problems.append("draft does not greet the lead by name (possible wrong-recipient draft)")

    for other in re.findall(r"\{\{.*?\}\}", draft):
        problems.append(f"unfilled template variable {other}")

    lowered = draft.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            problems.append(f"banned claim: '{phrase}'")

    problems += unapproved_claims(draft, approved_facts)

    if len(draft.split()) > MAX_WORDS:
        problems.append(f"draft longer than {MAX_WORDS} words")
    return problems
