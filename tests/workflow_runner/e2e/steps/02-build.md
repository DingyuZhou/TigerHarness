---
id: 02-build
persona: akagi
role: developer
on_approve: 03-review
on_revise: 01-plan
on_block: __escalate__
max_iters: 3
timeout_sec: 30
---

You are implementing the plan that was just approved. Produce a
short summary of what you built and any decisions you had to make
along the way.

If REVISE is the right call, the rewind goes back to the planning
step -- the plan itself needs to change, not your work.

End your reply with exactly one of:
    WORKFLOW: APPROVE
    WORKFLOW: REVISE: <one-line summary of what must change>
    WORKFLOW: BLOCK: <one-line summary of why we can't proceed>
