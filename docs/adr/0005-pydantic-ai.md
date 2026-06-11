# ADR 0005: do not adopt Pydantic AI now

Status: accepted. Date: 2026-06-11. Time-boxed evaluation (item 3 of
the Operator's TODO list); depth limits stated at the bottom.

## Context

Pydantic AI is the pydantic team's agent framework: typed agents
that OWN the model-call loop, structured-output validation with
automatic retries, broad multi-provider support, durable execution,
MCP/toolsets. (Sources: the Pydantic AI docs at pydantic.dev, the
pydantic/pydantic-ai GitHub repository, and the pydantic-ai PyPI
page, surveyed 2026-06-11.)

Three verified facts about THIS codebase decide the question:

1. `pyproject.toml` `dependencies = []` — zero hard dependencies is
   a design property; every integration lives behind an opt-in
   extra.
2. The project validates with dataclasses BY RECORDED CHOICE — the
   code says so in its own words
   (`journal/wfcore/trailer.py:61-63`: "Why frozen dataclasses (not
   pydantic)? ... the package has no pydantic";
   `journal/wfcore/models.py:3`). A premise that circulated in
   planning — "plain pydantic already covers the typed codebase" —
   is hereby corrected: the project uses NO pydantic at all, and
   that is deliberate, not an accident awaiting a framework.
3. Model calls exist ONLY at the `agent_sdk` backend edge
   (`anthropic_sdk` / `claude_p` / `openai_sdk`, each an opt-in
   extra). The billing doctrine keeps the journal drive/compile path
   free of API calls entirely — an agent framework has no seat
   there by design.

## Evaluation matrix

| Surface | Pydantic AI fit | Why |
|---|---|---|
| journal / tiger_memory / slack_bridge models | none | no model calls; dataclass validation is the documented pattern |
| drive / compile path | forbidden | billing doctrine: subscription sessions and Task sub-agents only |
| agent_sdk backends | the ONLY candidate | a `pydantic_ai` backend could sit behind an extra like the existing three — but no current consumer needs what it would add |

## Verdict

**Do not adopt now.** No core surface exists where Pydantic AI helps
without violating the zero-dependency design or the billing
doctrine, and the one coherent candidate (an optional agent_sdk
backend) has no present requirement behind it.

**Decision rule for revisiting** (so this ADR answers tomorrow's
question, not just today's): adopt narrowly — as an OPT-IN
`agent_sdk` backend extra only — when an agent_sdk consumer
concretely needs multi-provider structured-output-with-retries that
the existing backends would otherwise have to hand-roll. At that
point the follow-up ask to the Operator is: approve `pydantic-ai`
as an optional extra (yellow-light dependency change; never a hard
dep), implemented as a fourth backend with the same Protocol.

## Depth limits (what this evaluation did NOT do)

No hands-on trial, no benchmark, no API-surface audit beyond the
published docs — the verdict rests on structural facts about THIS
repo (verifiable by grep above) plus the framework's documented
purpose. If the decision rule ever triggers, the follow-up task
starts with the hands-on trial this time box excluded.
