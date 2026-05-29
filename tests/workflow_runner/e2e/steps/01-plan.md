---
id: 01-plan
persona: anzai
role: planner
on_approve: 02-build
on_revise: 01-plan
on_block: __escalate__
max_iters: 3
timeout_sec: 30
---

You are the planner for the synthetic e2e workflow. Read the task
brief and draft a one-paragraph plan covering scope, the rough
implementation outline, and the QA strategy.

If your previous attempt was rejected, take the feedback prologue
seriously and reduce scope or sharpen the plan accordingly.

End your reply with exactly one of:
    WORKFLOW: APPROVE
    WORKFLOW: REVISE: <one-line summary of what must change>
    WORKFLOW: BLOCK: <one-line summary of why we can't proceed>
