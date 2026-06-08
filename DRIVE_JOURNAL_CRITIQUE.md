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

## Round 2 — skill/OPERATING divergence + edge-case clarity

### MAJOR-4 (fixed) — the rewritten skill dropped the busy-blocks-pending guard

The lazy-loaded skill rewrite collapsed step 2's pick logic from **three**
branches to **two**. OPERATING.md (the contract) still resolves candidates
as: (a) resumable `in_progress` → resume; (b) **else if a *busy*
`in_progress` task exists → do NOT start new work, exit cleanly** (soft
lease, finish-before-start — a later task may depend on the in-flight one);
(c) else oldest `pending`. The skill's step 2 became just (a) resumable →
(b) pending, **silently deleting branch (b)** — the busy-blocks-pending
guard.

Why it bites despite "OPERATING.md wins": the whole point of the lazy-load
design is that OPERATING.md is read in **step 3**, *after* step 2 has
already picked **and `claim`ed**. So at pick time the skill's logic is
authoritative. With `[1 busy + 1 pending]`, the step-1 cheap-exit does
**not** fire (a pending exists), and the truncated step 2 would `claim` the
pending task and drive it **concurrently** with the busy one — exactly the
"never multiple tasks in parallel" / finish-before-start invariant the
design protects, and a direct contradiction of the user's own optimization
#2 ("if a task is running and healthy, do nothing else").

**Fix:** restored branch (b) verbatim-in-spirit in the skill's step 2
(busy `in_progress` present → exit cleanly), so the checklist now matches
OPERATING.md's three-branch order. Applied to both the bundled skill and
its byte-identical repo mirror (`skills/drive-journal/SKILL.md`); verified
identical by sha256. No `_PRIOR_SKILL_HASHES` bump needed — the only
*shipped* prior version is origin/main's `e9fabddd…` (still recorded), and
the interim redesign skill was never released to any team.

### MINOR-5 (note, no code) — the propagation transient is benign

An existing team adopts the new skill (via `--refresh-skills`) before the
new OPERATING.md (refreshed on the next `journal new`), so briefly runs
**new skill + origin/main OPERATING**. That's safe: the pick logic now
*agrees* (both are three-branch — this was the whole point of MAJOR-4), and
the skill's only net-new guidance (step-1 cheap-exit, step-7 compaction) is
something the old OPERATING is **silent** on, not contradicted — and
"OPERATING.md wins" resolves *contradictions*, it doesn't suppress guidance
OPERATING simply omits. So no anti-cascade regression during the window. No
fix; deliberately did **not** wire OPERATING refresh into `--refresh-skills`
(it's journal-scoped, not team-scoped — a team can host several journals).
