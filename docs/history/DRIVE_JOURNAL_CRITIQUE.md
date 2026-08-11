# drive-journal redesign — critique & iterate (3 rounds, 2026-06-08)

> **Status:** HISTORICAL — a point-in-time self-critique record of the
> 2026-06-08 redesign. Rounds 1–3 below describe the code as it stood
> then, not current behavior. For the current contract see
> [`../journal.md`](../journal.md) and
> [`../subscription-backend.md`](../subscription-backend.md).

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

## Round 3 — test-quality hardening + invariant pins

### MINOR-3 (fixed) — the hand-maintained hash manifests had no CI guard

`_PRIOR_SKILL_HASHES` / `_PRIOR_OPERATING_HASHES` are appended-to by hand on
every protocol edit (the documented maintenance rule). The dangerous slip
is appending the **NEW** content's hash instead of the **OLD** one — which
both halts propagation (existing teams' unmodified copies then look
"hand-edited") *and* makes the set self-referential. Nothing caught it.

**Fix:** two cheap self-reference guards —
`test_current_bundled_hash_not_in_prior_manifest` (test_init) and
`test_current_template_not_in_prior_hash_manifest` (test_operating_template)
— each asserts the **currently shipped** content's sha256 is **not** in its
prior-hash set. Verified non-vacuous: current skill `de412e0d…` ≠ prior
`e9fabddd…`; current OPERATING `b05da60b…` ≠ prior `fe942cf5…`.

### Bonus pin — finish-before-start guarded at the contract level

To stop MAJOR-4 from recurring on the *other* side, added
`test_finish_before_start_guard_pinned`: it asserts OPERATING.md still
carries branch (b) ("do NOT start new" + "finish before any … pending").
Since the skill defers to OPERATING.md on conflict, pinning the invariant
in the contract is the durable backstop — a future edit can't silently
delete it from OPERATING the way the skill rewrite once dropped it.

---

## Summary — 3 rounds

| # | Finding | Severity | Outcome |
|---|---------|----------|---------|
| 1 | 50% compact env reached only NEW teams | MAJOR | **Fixed** — `_ensure_compact_env_in_file` wired into re-init **and** `--refresh-skills` (no-clobber) |
| 2 | env var name is research-sourced | MINOR | Noted — graceful degradation (compaction-safe resume is load-bearing, not the exact %) |
| 3 | hash manifests hand-maintained, no guard | MINOR | **Fixed** in R3 — two self-reference guard tests |
| 4 | skill dropped the busy-blocks-pending guard | MAJOR | **Fixed** — branch (b) restored; matches OPERATING.md |
| 5 | new-skill / stale-OPERATING propagation window | MINOR | Noted — benign (pick logic agrees; skill additions are silent-not-contradicted) |
| — | finish-before-start now pinned in the contract | — | **Added** — regression guard for #4 |

---

## Round 4 — post-merge integration review

After merging `main`'s per-persona-memory revamp (commit 3c20dff), a round
over the *integration itself*. The merge was sound (full suite 100%,
Shohoku refresh verified at runtime), so the findings are honest **MINOR**s
— clarity + test-quality, no correctness regression.

### MINOR-6 (fixed) — skill over-attributed `step-done` to the compile phase

When weaving the revamp's memory gates into the lazy-load checklist, step 4
said "for `kind=workflow` … end each step at the `journal step-done` gate"
for the *whole* workflow branch. But `step-done` is the **graph-walk** gate
only; the **compile** sub-protocol advances via `land-compile`, which writes
its own per-round worklogs. A driver in `compile_pending=true` could have
been misled into reaching for `step-done`. **Fix:** scoped step 4 — `step-done`
is named for the graph walk, and `land-compile` is named for compile. Low
blast radius (step 4 runs *after* OPERATING.md is loaded in step 3, which is
authoritative), but the skill shouldn't mislead.

### MINOR-7 (fixed) — no regression guard that the skill teaches the gates

The merge's load-bearing fix was putting `--driver`/`--output`/`step-done`
*in the skill* (it `claim`s in step 2 before reading OPERATING.md in step 3,
so a Slack drive would otherwise skip per-persona memory). Nothing pinned
that. **Fix:** `test_skill_teaches_per_persona_memory_gates` asserts the
bundled skill names `--driver` (and that it appears at the *claim* step, not
only release), `--output`, and `journal step-done`; plus
`test_step_done_scoped_to_graph_walk_not_compile` locks in MINOR-6's scoping
(`graph walk` + `land-compile` both present).

### MINOR-8 (fixed) — bundled↔repo-mirror equality was hand-only

The installed package data (`src/.../_bundled_skills/`) and the
repo-of-record mirror (`skills/`) carry byte-identical copies of every
shared skill, but the sync is by hand (a `cp` after each edit). A future
edit to only the discoverable repo copy would silently diverge from what
actually installs. **Fix:** `test_bundled_and_repo_mirror_skills_byte_identical`
walks the intersection of the two trees and asserts every shared `SKILL.md`
is byte-for-byte equal. (Confirmed all 5 shared skills identical today.)

**Round 4 verified:** 3 new tests; full suite **3193 passed, 100%
line+branch coverage** on the merged tree. No production behavior changed —
one skill-copy clarity edit + three guards.

---

## Round 5 — cascade × compaction × per-persona memory

A round on the **memory-system integration specifically**: how the
cascade-first / auto-compaction redesign interacts with the revamp's
`--driver` / `--output` / worklog gates. Grounded in the actual code
(`drive_sessions.py`, `cli.py` claim/release/step-done,
`tiger_memory/sources/journal_worklog.py`).

### Verified sound (no change)

- **Cascade does not double-count transcripts.** `drive_sessions.register`
  is keyed by `thread_ts` with an idempotent upsert, so one cascading drive
  (one thread, many `claim`s) registers **once** and refreshes
  `last_seen_at`; the `claude_transcript` adapter suppresses that one
  thread. No per-task leak, no N-fold registration.
- **Attribution is right.** `_write_task_work_entry` stamps
  `persona=status.persona` (the **assigned** persona, never the
  `--driver`); the thin "I drove this" claim trace stamps the driver. A
  task assigned to the driver simply gets both entries — no double-count
  (distinct worklog files), transcript still suppressed.
- **`--driver` consistency is already instructed.** Both OPERATING step 5
  and the skill already say "pass the **same** `--driver` you used at
  `claim`." (The hazard if you don't: `claim` registered the thread →
  transcript suppressed, but a `--driver`-less `release` skips the worklog
  → the work vanishes from memory. The instruction covers it; left as-is.)

### MINOR-9 (fixed) — compaction can thin a `kind=task`'s only memory record

tiger-memory's `journal_worklog` source ingests **only** `worklog/*.md` —
**never `progress.md`**. And for a `kind=task`, the substantive worklog
note is written **once, at done** (`release --output`), *deferred* to the
end of the whole task — unlike `kind=workflow`, where `step-done` writes
each step's note immediately. So in an aggressive cascade that auto-compacts
mid-task (exactly what the redesign encourages), the end-of-task note can be
assembled from **post-compaction** context that has elided earlier sessions
— silently thinning the assigned persona's *only* memory of the task.
`progress.md` survives compaction and is the natural source to rebuild from,
but it is **not itself ingested**, so it has to be *used to write the note*,
not relied on as the memory.

**Fix:** at the `kind=task` done-note (OPERATING step 5 + skill step 5) and
from the compaction guidance (OPERATING/skill step 6/7), instruct the driver
to **build the note from the durable record** (`progress.md` + `artifacts/`
+ prior worklog), naming why: it is written once, a long cascade may have
compacted earlier detail, and only `worklog/` is ingested. `kind=workflow`
is called out as inherently safe (per-step notes land immediately). Pinned
the landmark with `test_task_done_note_durable_record_guidance_pinned`
(`"durable record"` + `"ingests only"`) so it can't be silently dropped.

**Round 5 verified:** 1 new pin test; full suite **3194 passed, 100%
line+branch coverage**. Doc-only change to the protocol (OPERATING template
+ skill, mirror byte-identical) — no Python behavior change; the revamp's
gate code is correct as-is, the gap was in the *driver's* note-building
guidance under compaction.

**Net code change across the three rounds:** one new no-clobber helper
(`_ensure_compact_env_in_file`) wired into two adoption paths; the skill's
step-2 pick logic realigned to the contract; six new tests (the helper's 7
unit + 2 refresh, plus 3 guard/pin tests across two files). Throughout:
**100% line+branch coverage held** (final: 2979 passed). No production
*behavior* regressions introduced — the two MAJORs were both gaps in the
redesign caught and closed here.
