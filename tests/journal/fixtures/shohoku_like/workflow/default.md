# Default Playbook (fixture)

A pared-down playbook used by the team-defaults integration test.
The HTML-comment block carries `default_captain:` (a journal-side
key) AND `workflow_config:` (a runner-side block) -- the
whitelisted parser keeps the captain and drops the runner block.

<!--
default_captain: Mitsui
workflow_config:
  human_gate: true
  max_loop_iters: 5
-->

## Roles

- Anzai plans.
- Akagi reviews execution.
- Mumu reviews QA.
- Mitsui implements.
