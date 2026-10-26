"""AI first-touch SDR agent: qualify inbound leads by email and hand qualified ones to humans."""
from .crm import InMemoryCRM
from .llm import RuleBasedClient, default_client
from .models import Lead, LeadStatus, ReplyIntent
from .pipeline import SDRAgent

__all__ = ["InMemoryCRM", "RuleBasedClient", "default_client", "Lead", "LeadStatus", "ReplyIntent", "SDRAgent"]
