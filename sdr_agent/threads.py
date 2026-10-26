"""Work out which lead an inbound email belongs to.

Calling handle_reply(lead_id, ...) assumes something already knows the lead. In a real
inbox that is the hard part: a colleague replies from another address, someone forwards
the thread, a reply lands on an old step of the sequence.

Resolution order, strongest evidence first:
  1. In-Reply-To matches a message the agent sent       -> that lead
  2. Any References header matches a sent message        -> that lead (forwards, long threads)
  3. Sender address belongs to exactly one known lead    -> that lead
  4. Anything else, including an address shared by two leads -> unresolved, a person decides

Never guess. A reply attached to the wrong lead is worse than a reply that waits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class Resolution:
    lead_id: Optional[str]
    via: str          # in_reply_to | references | sender | unresolved


class ThreadIndex:
    def __init__(self):
        self._sent: dict[str, str] = {}             # outbound Message-ID -> lead_id
        self._by_email: dict[str, set[str]] = {}    # address -> lead_ids

    def record_sent(self, message_id: str, lead_id: str) -> None:
        self._sent[message_id.strip()] = lead_id

    def register_lead(self, lead_id: str, email: str) -> None:
        self._by_email.setdefault(email.strip().lower(), set()).add(lead_id)

    def resolve(self, from_addr: str, in_reply_to: str = "", references: str = "") -> Resolution:
        if in_reply_to.strip() in self._sent:
            return Resolution(self._sent[in_reply_to.strip()], "in_reply_to")
        for ref in re.findall(r"<[^>]+>", references or ""):
            if ref in self._sent:
                return Resolution(self._sent[ref], "references")
        leads = self._by_email.get(from_addr.strip().lower(), set())
        if len(leads) == 1:
            return Resolution(next(iter(leads)), "sender")
        return Resolution(None, "unresolved")
