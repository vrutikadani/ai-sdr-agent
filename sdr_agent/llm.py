"""LLM client layer.

The agent talks to an `LLMClient` interface, so the same pipeline runs:
  * offline with `RuleBasedClient` (deterministic, used in tests and the demo), or
  * live with `ClaudeClient` (Anthropic Messages API) when ANTHROPIC_API_KEY is set.
"""
from __future__ import annotations

import json
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
            return ReplyIntent(data["intent"]), float(data["confidence"])
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


def default_client() -> LLMClient:
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            return ClaudeClient()
        except ImportError:
            pass
    return RuleBasedClient()
