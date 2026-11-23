"""LLM client layer.

The agent talks to an `LLMClient` interface, so the same pipeline runs:
  * offline with `RuleBasedClient` (deterministic, used in tests and the demo), or
  * live with `ClaudeClient` (Anthropic Messages API), only when SDR_LIVE_LLM=1.

An API key in the environment is not enough to go live. Tests, the demo and the
eval stay offline unless live mode is asked for by name.

Whatever a client returns is checked by `validate_classification` before the
pipeline uses it. Bad output becomes ("unclear", 0.0), which goes to review.
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Protocol

from .models import ReplyIntent


class LLMClient(Protocol):
    def classify_reply(self, body: str) -> tuple[ReplyIntent, float]: ...
    def write_email(self, prompt: str) -> str: ...


CLASSIFY_SYSTEM = """You classify replies to a sales email.
Return JSON only: {"intent": one of %s, "confidence": 0-1}.
If unsure, use "unclear". Opt-out language always means "unsubscribe".""" % [i.value for i in ReplyIntent]


def validate_classification(raw) -> tuple[ReplyIntent, float]:
    """Accept only (known intent, confidence in [0, 1]). Anything else -> (UNCLEAR, 0.0)."""
    try:
        intent, conf = raw
        intent = ReplyIntent(intent)
    except (TypeError, ValueError):
        return ReplyIntent.UNCLEAR, 0.0
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        return ReplyIntent.UNCLEAR, 0.0
    conf = float(conf)
    if math.isnan(conf) or not 0.0 <= conf <= 1.0:
        return ReplyIntent.UNCLEAR, 0.0
    return intent, conf


class ClaudeClient:
    """Live client. Requires `pip install anthropic` and ANTHROPIC_API_KEY."""

    def __init__(self, model: str = "claude-sonnet-4-5"):
        import anthropic  # imported lazily so offline mode needs no dependency
        self._client = anthropic.Anthropic()
        self.model = model

    def _ask(self, system: str, user: str, max_tokens: int = 400) -> str:
        msg = self._client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text

    def classify_reply(self, body: str) -> tuple[ReplyIntent, float]:
        raw = self._ask(CLASSIFY_SYSTEM, body, max_tokens=60)
        try:
            data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
            return validate_classification((data["intent"], data["confidence"]))
        except Exception:
            return ReplyIntent.UNCLEAR, 0.0

    def write_email(self, prompt: str) -> str:
        return self._ask("You write short, warm, plain-language sales emails. No hype.", prompt)


class RuleBasedClient:
    """Deterministic stand-in. Good enough to exercise every branch of the pipeline."""

    RULES: list[tuple[ReplyIntent, str]] = [
        (ReplyIntent.UNSUBSCRIBE, r"\b(unsubscribe|remove me|stop emailing|opt out|do not contact)\b"),
        (ReplyIntent.BOUNCE, r"(delivery (has )?failed|undeliverable|mailbox not found|address rejected)"),
        (ReplyIntent.OUT_OF_OFFICE, r"(out of (the )?office|on vacation|limited access to email|auto-?reply)"),
        (ReplyIntent.NOT_A_FIT, r"(not interested|no need|wrong person|we already (have|own)|not a fit)"),
        (ReplyIntent.NOT_NOW, r"(next (quarter|year|month)|not right now|maybe later|circle back|too busy)"),
        (ReplyIntent.INTERESTED, r"(book|schedule|call|demo|pricing|price|quote|interested|sounds good|let'?s talk)"),
        (ReplyIntent.QUESTION, r"\?"),
    ]

    def classify_reply(self, body: str) -> tuple[ReplyIntent, float]:
        text = body.lower()
        for intent, pattern in self.RULES:
            if re.search(pattern, text):
                return intent, 0.9
        return ReplyIntent.UNCLEAR, 0.3

    def write_email(self, prompt: str) -> str:
        return prompt  # the drafter's template is already a complete email


LIVE_FLAG = "SDR_LIVE_LLM"


def default_client() -> LLMClient:
    """Offline unless SDR_LIVE_LLM=1. If live mode is asked for and cannot start, fail loudly."""
    if os.getenv(LIVE_FLAG) != "1":
        return RuleBasedClient()
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(f"{LIVE_FLAG}=1 but ANTHROPIC_API_KEY is not set")
    return ClaudeClient()  # ImportError here means `pip install anthropic`
