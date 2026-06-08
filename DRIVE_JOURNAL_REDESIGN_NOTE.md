# drive-journal redesign — implementation note (2026-06-08, Anzai)

Worktree: `/home/tigerleap/projects/tigerharness-dj-redesign`
Branch: `work/2026-06-06... → work/2026-06-08-drive-journal-cascade` off
`origin/main` (cb38fd2). **Not pushed.** Main checkout untouched, so you
can keep working there in parallel.

## What I changed (your 4 optimizations)

1. **Lightweight ToDo entry point** — rewrote the `drive-journal` skill
   (`src/tigerharness/_bundled_skills/drive-journal/SKILL.md`, mirrored to
   `skills/drive-journal/SKILL.md`) into a tight, imperative 7-step
   checklist. The heavy detail (workflow compile/graph-walk, the long
   "what not to do") moved out to OPERATING.md, **lazy-loaded only after
   you've claimed real work** (step 3). A no-op fire never loads it.

2. **Busy-check first / do-nothing-if-running** — **step 1** is now the
   sweep + a *cheap exit*: if every `in_progress` task is **busy**
   (attached **and** heartbeat fresh within the stuck-timeout) and nothing
   is idle/crashed/pending, **stop in a few tokens** without reading
   anything else. Note we use the precise `busy` = *attached + fresh*
   (not heartbeat-alone), so an **idle** (cleanly-handed-off) task still
   resumes instantly — heartbeat-alone would have re-introduced the gap.
   The atomic `claim` is the real double-drive guard; the check is the
   cheap optimization. (Same promoted to OPERATING.md step 1d.)

3. **Short + lazy-load** — the skill is ~half the size and points to
   OPERATING.md for the procedure; kind-specific sub-protocols load only
   for that kind. Cheap, frequent fires.

4. **Compaction instead of "context heavy" hand-offs** — **step 7**:
   because every session checkpoints to `progress.md` + `next_action`, a
   compaction loses nothing — so the driver must NOT hand off early "to
   let the loop bridge"; it keeps cascading and relies on auto-compaction
   (recommended ~75% of the window), re-orienting from progress.md after.
   This is the structural fix for the bug that bit us.

Plus the **cascade is now a hard loop** (skill step 6 / OPERATING step 6):
"if work remains, go back to step 1 in the SAME turn — do NOT end your
turn / one-session-per-loop-fire." That anti-pattern is named explicitly
and **pinned by a test** (`tests/journal/test_operating_template.py::
test_continuity_contract_pinned`).

## Config knobs (where each lives)

- **Stuck-timeout (your "30 min, configurable")** — already supported:
  env `TIGERHARNESS_JOURNAL_STUCK_TIMEOUT` (seconds, default 1800) +
  `--stuck-timeout` on the sweep CLI. Now *documented* in the skill +
  OPERATING.md. No code change needed.
- **Loop cadence** — your call at invocation: `/loop 5m drive the journal`
  (or 10m). The new cheap busy-check makes frequent firing cheap.
- **Compact threshold (~75%)** — documented as a recommendation in the
  skill/OPERATING prose. I did **not** hard-wire a settings.json key,
  because I'm not certain of the exact Claude Code auto-compact-threshold
  key and didn't want to ship a guessed/invalid one. **→ Decision for
  you:** confirm the harness's auto-compact setting and wire it via the
  `update-config` skill, or rely on the built-in near-limit auto-compact
  (the driver's per-session checkpoint discipline already makes any
  compaction safe). Everything load-bearing (compaction-safe resume) is in.

## ⚠️ Deployment caveat — updates do NOT auto-reach existing teams

`install_bundled_skills` / `init --refresh-skills` and the OPERATING.md
installer are **install-if-missing, never overwrite** (so a team's
hand-edited skill is never clobbered). So:
- **New** teams / journals get the new behavior automatically. ✅
- **Existing** teams (e.g. Shohoku) keep their on-disk copies. To adopt:
  - Skill: `rm <team>/.claude/skills/drive-journal/SKILL.md` then
    `tigerharness init --refresh-skills` (or copy the new file).
  - OPERATING: `rm <journal>/OPERATING.md` then the next
    `tigerharness journal new` reinstalls the fresh template (or copy it).

**Recommended follow-up (not done — your call, it's an architecture
decision):** teach `init --refresh-skills` + the OPERATING installer to
*overwrite when the on-disk copy is byte-identical to a previously-shipped
version* (content-hash compare), so future protocol updates propagate to
existing teams without manual deletion, while still never clobbering
hand-edits. That's the clean long-term fix for "make it general."

## Tests

Full suite green in the worktree: **2964 passed, 100% line+branch
coverage**. `operating_template.py` landmark tests still pass; added the
continuity-contract pin. (No production logic changed — only the skill
markdown, the OPERATING template string, and the new test.)
