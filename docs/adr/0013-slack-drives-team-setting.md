# ADR 0013 — Slack-triggered drives are a team setting

- Status: accepted
- Date: 2026-09-13
- Deciders: Operator ("Slack drives should be permitted in Shohoku; there
  is no more subscription limitation for `claude -p`", 2026-09-13),
  Shohoku (execution)

## Context

The rail rule "Slack schedules, never drives" was written when a
bridge-spawned `claude -p` turn was believed to bill token-metered API
([subscription-backend.md](../subscription-backend.md)). `journal claim`
enforces it mechanically: the bridge exports `TIGERHARNESS_SLACK_THREAD_TS`
into every turn, and a claim carrying that marker is refused unless
`--allow-api-drive` is passed.

The Operator lifted the rule for Shohoku on 2026-06-25 (the billing
concern did not apply), re-imposed it as "doctrine v2" on 2026-08-03 as a
rail rather than a cost claim, and on 2026-09-13 stated the current
policy: Slack drives are permitted on Shohoku, and `claude -p` has no
subscription limitation. Each swing was carried by a hand-edited block in
the team's copy of the `drive-journal` skill, which (a) contradicted the
shipped skill and OPERATING.md that a persona is told "wins", and (b)
made that copy un-refreshable by the hash gate, so every skill update had
to be hand-merged.

## Decision

Make it a **team setting**, read by the guard itself:

- `TIGERHARNESS_JOURNAL_SLACK_DRIVES=1` in the team's `configs/.env` (or the
  process env; flag > env > team file, the same precedence and the same
  dependency-free reader as the autodrive knobs). When truthy, `journal
  claim` accepts a bridge session without `--allow-api-drive` and logs
  that team policy allowed it.
- Off by default. A team that never opted in keeps "Slack schedules, never
  drives" exactly as before, and `--allow-api-drive` remains the deliberate
  one-off override there.
- The shipped `drive-journal` and `journal-new` skills and the OPERATING.md
  template state the rule conditionally, so no team needs a forked skill to
  hold its policy, and the hash gate can refresh every team's skills.

## Consequences

- Shohoku sets the knob; its charter and knowledge page record the policy
  and its history (lift, re-imposition, setting). Other teams are
  unaffected until they opt in.
- The guard's log line distinguishes the two allowed paths ("allowed by
  team policy" vs the explicit override), so an audit can still tell them
  apart.
- Autodrive is unchanged: its sanctioned drives always pass
  `--allow-api-drive`, and are never bridge sessions anyway.
- Lean scheduling (brief verbatim, one scaffold command, report the id,
  stop) is still the rule when scheduling is all that was asked; the knob
  says a Slack session *may* drive, not that it must.
