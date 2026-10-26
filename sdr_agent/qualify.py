"""Lead scoring. Transparent, rule-based, and explainable to sales leaders.

Every point added is recorded as a reason, so an SDR can see *why* a lead was routed.
"""
from __future__ import annotations

import re

from .models import Lead

TARGET_SPECIALTIES = {"dermatology": 30, "plastic surgery": 30, "med spa": 20, "aesthetics": 20}
FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com"}
BUYING_SIGNALS = {
    r"\b(price|pricing|cost|quote)\b": (20, "asked about pricing"),
    r"\b(demo|trial|see it)\b": (15, "asked for a demo"),
    r"\b(buy|purchase|financ\w*|lease)\b": (20, "purchase or financing intent"),
    r"\b(this (month|quarter)|asap|soon)\b": (10, "near-term timeline"),
    r"\b(add(ing)? (a )?(new )?service|expand\w*)\b": (10, "expanding services"),
}
QUALIFY_THRESHOLD = 50


def score_lead(lead: Lead) -> Lead:
    score, reasons = 0, []

    spec = lead.specialty.strip().lower()
    for key, pts in TARGET_SPECIALTIES.items():
        if key in spec:
            score += pts
            reasons.append(f"+{pts} target specialty ({lead.specialty})")
            break

    domain = lead.email.split("@")[-1].lower()
    if domain in FREE_MAIL:
        score -= 10
        reasons.append("-10 free email domain")
    else:
        score += 10
        reasons.append("+10 business email domain")

    msg = lead.form_message.lower()
    for pattern, (pts, label) in BUYING_SIGNALS.items():
        if re.search(pattern, msg):
            score += pts
            reasons.append(f"+{pts} {label}")

    if re.search(r"\b(student|job|career|resume|hiring)\b", msg):
        score -= 40
        reasons.append("-40 not a buyer (job / student inquiry)")

    lead.score = max(0, min(100, score))
    lead.score_reasons = reasons
    return lead


def is_qualified(lead: Lead) -> bool:
    return lead.score >= QUALIFY_THRESHOLD
