---
id: 03-review
persona: rukawa
role: reviewer
on_approve: __done__
on_revise: 02-build
on_block: __escalate__
max_iters: 3
timeout_sec: 30
---

Review the implementation against the approved plan. Are the
behaviour changes complete? Is the QA strategy honoured? Are there
loose ends?

If REVISE, rewind goes to the build step. If APPROVE, the workflow
is done.

End your reply with exactly one of:
    WORKFLOW: APPROVE
    WORKFLOW: REVISE: <one-line summary of what must change>
    WORKFLOW: BLOCK: <one-line summary of why we can't proceed>
