"""Evaluate the reply loop at two levels.

  label accuracy    : did the classifier pick the right intent?
  workflow outcome  : did the agent take the right action (handoff, email, stop, wait, review)?

The second number is the one that matters. A wrong label that still routes to human
review is cheap; a wrong label that emails someone who opted out is not. Failures are
graded by cost, and any critical failure exits non-zero so CI blocks the change.

    python evals/run_eval.py              # offline rule-based classifier
    ANTHROPIC_API_KEY=... python evals/run_eval.py   # same set against Claude
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdr_agent import InMemoryCRM, Lead, SDRAgent, default_client  # noqa: E402

EXPECTED_ACTION = {
    "interested": "handoff",
    "question": "send_email",
    "not_now": "nurture",
    "not_a_fit": "stop",
    "unsubscribe": "stop",
    "bounce": "stop",
    "out_of_office": "wait",
    "unclear": "review",
}


def severity(label: str, action: str, opted_out: bool, handed_off: bool) -> str:
    """How bad is this outcome? critical > costly > safe-miss > ok."""
    if label == "unsubscribe" and not opted_out:
        return "critical"            # kept emailing someone who asked us to stop
    if handed_off and label != "interested":
        return "costly"              # an SDR spends time on a non-buyer
    if action == EXPECTED_ACTION[label]:
        return "ok"
    if action == "review":
        return "safe-miss"           # wrong, but a person catches it
    if label == "interested" and action != "handoff":
        return "costly"              # a buyer waits
    return "costly"


def run(path: Path) -> dict:
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    llm = default_client()
    label_hits, results = 0, []
    confusion: dict[str, Counter] = defaultdict(Counter)

    for row in rows:
        crm = InMemoryCRM()
        agent = SDRAgent(crm, llm, mode="autonomous")
        lead = Lead(lead_id=row["id"], first_name="Ana", last_name="Diaz", email=f"{row['id']}@clinic.com",
                    company="Diaz Derm", specialty="Dermatology", form_message="Tell me more.")
        agent.handle_new_lead(lead)
        d = agent.handle_reply(row["id"], row["reply"])
        predicted = d.intent.value if d.intent else "none"
        confusion[row["label"]][predicted] += 1
        label_hits += predicted == row["label"]
        sev = severity(row["label"], d.action, lead.email in crm.suppression, bool(crm.tasks))
        results.append({**row, "predicted": predicted, "action": d.action, "severity": sev})

    counts = Counter(r["severity"] for r in results)
    return {"n": len(rows), "label_accuracy": label_hits / len(rows),
            "workflow_ok": counts["ok"] / len(rows), "severity": counts,
            "results": results, "confusion": confusion, "client": type(llm).__name__}


def main() -> int:
    r = run(Path(__file__).with_name("replies.jsonl"))
    print(f"client: {r['client']}   replies: {r['n']}")
    print(f"label accuracy:     {r['label_accuracy']:.0%}")
    print(f"correct action:     {r['workflow_ok']:.0%}")
    print("outcomes:           " + ", ".join(f"{k} {v}" for k, v in sorted(r['severity'].items())))
    misses = [x for x in r["results"] if x["severity"] != "ok"]
    if misses:
        print("\nnot ok:")
        for x in misses:
            print(f"  [{x['severity']:<9}] {x['id']} expected {x['label']:<13} got {x['predicted']:<13} -> {x['action']:<10} {x['reply'][:60]}")
    if r["severity"]["critical"]:
        print("\nFAIL: critical outcomes present")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
