# drive-journal redesign — critique & iterate (3 rounds, 2026-06-08)

Self-critique over the two commits (b556052 cascade redesign + 6a200eb
compact/propagation). Each round: severity-ranked findings, a fix where
warranted, 100% line+branch coverage held.

## Round 1 — propagation + (a) generality

### MAJOR-1 (fixed) — the 50% compact env never reaches EXISTING teams

`(a)` claimed to be "general," but `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50` was
only seeded into a **fresh** team's settings.json (`create_team` else
branch). The two automated paths an *existing* team hits —
`_merge_journal_guard_into` (re-init) and `--refresh-skills` — top up the
journal-guard hook but **not** the compact env. So the very teams we built
the propagation (b) for would adopt the new skill/OPERATING but never the
compact threshold. That defeats the goal.

**Fix:** add `_ensure_compact_env_in_file` (symmetric to the guard merger:
read → set `env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="50"` **only if absent**
(never clobber an operator's chosen value) → write if changed). Call it
(a) in `create_team`'s existing-settings branch, and (b) in the
`--refresh-skills` path, so the one-command adoption (`--refresh-skills`)
now brings skills **and** the recommended settings current. Kept the guard
merger + its unit tests untouched (separate, well-tested concern).

### MINOR-2 (note, no code) — env var name is research-sourced

`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` came from the claude-code-guide agent's
doc research, not first-hand verification. If the key name is wrong in a
given Claude Code version, the threshold silently falls back to the
built-in default (~95%) — i.e. **graceful degradation**: the drive still
cascades and still compacts (just later), because the load-bearing piece
is the compaction-safe resume (progress.md), not the exact %. **Action:**
operator should confirm `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` is honored in
their version; no code change (can't verify beyond the cited docs here).

### MINOR-3 (deferred to round 3) — prior-hash manifests are hand-maintained

`_PRIOR_SKILL_HASHES` / `_PRIOR_OPERATING_HASHES` must be appended-to on
every future skill/OPERATING edit or propagation silently stops. Mitigated
by inline docs + the NOTE maintenance rule. A sanity guard test is worth
adding (round 3).

**Round 1 verified:** `_ensure_compact_env_in_file` landed + wired into both
paths; 7 unit tests (add/beside-env/respect-operator/missing/malformed/
non-object/non-dict-env), 2 refresh tests (tops-up-existing-team,
no-settings-file-is-fine), and the `_scaffold_claude_dir` additive test
updated. Full suite: **2976 passed, 100% line+branch coverage**. NOTE
reconciled (the "manual one-line add" caveat is gone).
