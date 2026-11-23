# Design context

This repo is a first-touch agent for inbound leads.
It scores a form fill with rules, drafts one short email from a template, reads the reply, and hands real buyers to an SDR with the reasons attached.
The model has two narrow jobs: it labels the reply, and it may personalize a filled template.
Rules decide everything else.
In the GTM stack it covers the step between a new lead arriving and a person owning it.
It is a synthetic reference build.
The CRM, the mail provider and the live model are not exercised here.

## Where it sits in the four GTM engineering layers

The four layers come from Everett Berry's (Clay) talk "GTM Engineering: The Technical Bits."

| Layer | What this repo implements |
|---|---|
| Data | Thin. The lead is taken as given from the form (`models.py:Lead`). `qualify.py:score_lead` scores it from the form fields and writes a reason for every point. There is no enrichment, no waterfall across providers and no refresh. Entity resolution is limited to email threads: `threads.py:ThreadIndex.resolve` matches a reply by In-Reply-To, then References, then a sender that matches exactly one lead. |
| Orchestration | One process, event by event. `pipeline.py:SDRAgent.handle_new_lead` and `SDRAgent.handle_reply` are the entry points. `idempotency.py:IdempotencyStore` marks an event done only after its CRM writes succeed, with keys from `form_key` and `reply_key`. A failed write lands in `SDRAgent.outbox`. Nothing replays the outbox, and there is no scheduler or fan-out. |
| Agents | Narrow. `llm.py:ClaudeClient.classify_reply` (opt-in) or `llm.py:RuleBasedClient` labels the reply. `llm.py:validate_classification` turns a bad label or confidence into `unclear`. `drafter.py:first_touch` fills a fixed template and passes it to `LLMClient.write_email`, and the result still goes through `guardrails.py:check_draft`. The `OPT_OUT` regex in `pipeline.py` runs before the model. There is no long-running agent per account and no persistent agent memory. |
| Execution | `guardrails.py:check_draft` blocks wrong names, unfilled `{{variables}}`, banned phrases and unapproved claims. In draft mode `SDRAgent.approve` re-checks guardrails and state before `SDRAgent._send`. `state.py:can_transition` makes opted-out and disqualified terminal, and keeps the agent off an SDR-owned lead. Sending is mocked (`SDRAgent.sent`). |

Berry's points that this repo takes seriously:

- **Multi-channel suppression.** Once a lead is handed off (`SDRAgent._handoff`), the state machine refuses any further email, nurture or second task.
  That is the "a booked meeting must stop the sequence" rule, applied to a handoff.
  There is only one channel here, so there is nothing to coordinate across channels.
- **High bar for error.** The output is customer email, so every draft passes guardrails, and in draft mode a person approves it.
- **The human-agent interface.** The SDR task carries `Lead.score_reasons`, so the rep can see why the agent acted.
  `SDRAgent._record` logs each decision as a CRM note tagged "[AI agent]".
  Refused moves are kept in `SDRAgent.blocked`.
- **Agent fields apart from human fields.** The HubSpot sketch (`crm.py:HubSpotCRM`) writes only to its own properties (`ai_agent_status`, `ai_lead_score`, `ai_agent_opted_out`, `ai_agent_suppression_reason`).
  It does not overwrite rep-owned fields.
  This is tested only against a fake session.

Berry's points it does not cover: a virtual copy of accounts, waterfall enrichment, selective refresh, and deliverability or domain reputation.

## Comparison with LangChain's published GTM agents

- **"How we built LangChain's GTM Agent": trigger, check, gather context, draft for approval.** Same shape.
  A new lead triggers `handle_new_lead`, `SDRAgent._intake` decides whether to reach out, and draft mode stores a `pending_approval` draft for a person to approve.
  The difference: the draft goes to an approval call, not a Slack message, and it carries the agent's reason, not cited sources.
- **Same post: learn rep style by diffing rep edits.** Not done.
  `SDRAgent.approve` takes the draft as written.
  There is no edit capture and no per-rep memory.
- **Same post: subagents with constrained tools and structured output.** Similar idea, smaller scale.
  The model has no tools.
  Reply labels come back as one (intent, confidence) pair, checked by `validate_classification`.
  Email text comes back as a string, checked by `check_draft`.
- **"How we built LangChain's Paid Media Agent": the model judges, code computes.** Same.
  Scoring, opt-out detection, state changes and the send decision are code.
  The model only labels the reply and polishes template text.
- **Same post: a path from analysis to action, with approval and post-apply checks.** Partly.
  Approval exists and re-checks state before sending.
  There is no read-back after a CRM write.
  A failed write raises `CRMWriteError` and goes to review.
- **"How We Built Our Design Agent": hard ordering rules in code, judgment in the prompt.** Same.
  Opt-out runs before the model, guardrails run before any send, and the state machine runs before any status change.
  The post's advice to delete rules as models improve has not been tested here.
  The opt-out regex is kept on purpose, because a missed opt-out is a legal problem, not a model weakness.

This repo does not use LangGraph, Deep Agents or LangSmith.
The imports are the standard library plus optional `anthropic` and `requests`.
The flow is a small deterministic pipeline with at most one model call per step.
The hard problems are side effects, identity and idempotency: a duplicate webhook, a reply from the wrong address, a CRM write that fails halfway.
A framework does not solve those on its own.

## Not built here

- No per-rep learned memory, and no capture of how reps edit drafts.
- No tracing or LangSmith.
  The record is `SDRAgent.log`, `SDRAgent.blocked` and `SDRAgent.outbox`, all in memory, plus a CRM note per decision.
- No live integrations tested.
  `HubSpotCRM` runs only against a fake session, and `ClaudeClient` is not run in CI.
- No mail provider, deliverability checks or domain warm-up.
- No enrichment, refresh or account-level view.
  It works one lead at a time.
- Single process.
  The idempotency store has no claim step, so two workers could handle the same event.
- Pending drafts and the outbox live in memory and are lost on restart.

These comparisons are my reading of public posts; they are not claims about how those systems work internally beyond what the posts say.
