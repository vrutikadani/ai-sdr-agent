"""AI first-touch SDR agent: qualify inbound leads by email and hand qualified ones to humans."""
from .crm import CRMWriteError, InMemoryCRM
from .idempotency import IdempotencyStore
from .llm import RuleBasedClient, default_client
from .models import Action, Decision, Lead, LeadStatus, ReplyIntent
from .pipeline import SDRAgent

__all__ = ["Action", "CRMWriteError", "Decision", "IdempotencyStore", "InMemoryCRM", "RuleBasedClient",
           "default_client", "Lead", "LeadStatus", "ReplyIntent", "SDRAgent"]
