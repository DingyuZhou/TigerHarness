# drive-journal redesign — implementation note (2026-06-08, Anzai)

Worktree: `<checkout-parent>/tigerharness-dj-redesign` (historical note; worktree long removed)
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

---

## Update (2026-06-08) — (a) compact threshold + (b) refresh auto-update IMPLEMENTED

Both follow-ups are now done in this worktree (commit follows). They
supersede the "recommended follow-up" + the manual-refresh caveat above.

### (a) Compact threshold = 50%, wired

The lever is the env var **`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`** (integer
1–100 = % of context window at which auto-compact triggers; Claude Code
default ~95). It is **env-only — there is no settings.json *key*** for it,
but it lives fine in the `env` block of settings.json.
- `init.py` now seeds new teams' `.claude/settings.json` with
  `"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"`.
- Skill + OPERATING docs updated from ~75% → **50%** and now name the env var.
- **Existing teams**: the env is now auto-merged — `_ensure_compact_env_in_file`
  additively sets the key (never clobbering an operator-chosen value, never
  touching a malformed/non-dict file) and is called from BOTH
  `_scaffold_claude_dir`'s existing-settings branch (re-init) AND the
  `tigerharness init --refresh-skills` path. So the one adoption command tops
  up skills **and** settings together. *(Env changes apply to NEW Claude
  sessions.)* — supersedes the earlier "one-line manual add" note.

### (b) Updates now propagate to existing teams (hash-gated, no clobber)

- **Skills** — `tigerharness init --refresh-skills` now: installs missing
  skills **+ refreshes any skill that is byte-identical to a previously
  shipped version** to the current bundled one, **+ leaves hand-edited
  skills untouched** (reports each bucket). Driven by `_PRIOR_SKILL_HASHES`
  in `init.py`.
- **OPERATING.md** — `_ensure_operating_md` now refreshes an unmodified
  prior-ship OPERATING.md to the current template **on the next
  `journal new`** (hash-gated via `_PRIOR_OPERATING_HASHES` in
  `scaffold.py`); hand-edited files are left alone. No init↔journal
  coupling, self-healing.
- Recorded the prior (origin/main) hashes — drive-journal SKILL.md
  `e9fabddd…`, OPERATING.md `fe942cf5…` — so **existing teams (Shohoku)
  auto-adopt** the new behavior: run `tigerharness init --refresh-skills`
  for the skill, and the next `journal new` refreshes OPERATING.md. No
  manual file deletion needed anymore.

### 🔧 MAINTENANCE RULE (important for future protocol edits)

When you next change the bundled `drive-journal/SKILL.md` or `OPERATING_MD`,
**append the OLD content's sha256 to the matching manifest**
(`init._PRIOR_SKILL_HASHES` / `scaffold._PRIOR_OPERATING_HASHES`) so the
then-current-but-now-prior version is recognized as "ours, safe to refresh"
on the next `--refresh-skills` / `journal new`. (Otherwise existing teams'
copies look "hand-edited" and won't auto-update.) Each is documented inline
at its definition.

### To adopt on Shohoku specifically (left for you / your parallel work)

1. `tigerharness init --refresh-skills --team-dir <…>/Shohoku`  → updates the
   skill **and** tops up `.claude/settings.json` (compact threshold +
   guard hook). One command now does both.
2. next `tigerharness journal new …`  → refreshes `journal/OPERATING.md`.
   *(The compact-env step from the old 3-step list is folded into step 1.)*

Tests: full suite green **2967 passed, 100% line+branch coverage**
(init.py / scaffold.py / operating_template.py all 100%).

---

## Update (2026-06-08) — merged `main`'s per-persona-memory revamp

`main` advanced (PRs #40–#44) with the **per-persona journal memory**
revamp (worklog/, `--driver`/`--output`, `journal step-done` gates,
`JournalWorklogAdapter`, chunk-and-reduce). Merged it into this branch and
reconciled the overlap — the two feature sets are orthogonal and now
coexist:

- **OPERATING.md template** — auto-merged cleanly: my edits (step-1d cheap
  exit, hardened step-6 cascade, step-7 compaction, heartbeat env) touch
  disjoint regions from the revamp's (worklog/, "Per-persona memory"
  section, `--driver`/`--output` at claim/release, graph-walk gate). Both
  sides' pin tests pass on the merged string.
- **drive-journal skill** — real 3-way conflict (the revamp also edited the
  skill, against the old long form, while I'd rewritten it). Hand-merged:
  kept the short cascade-first checklist and **wove the memory gates in** —
  `--driver` at claim (step 2), `step-done` at the workflow branch (step 4),
  `--driver`/`--output` + "the note is the ticket" at release (step 5), and
  the worklog/`done`-gate reminders. This closes a latent bug: the
  lazy-load skill `claim`s in step 2 *before* reading OPERATING.md in step
  3, so without the woven `--driver` a Slack drive would have silently
  skipped per-persona memory. Bundled + mirror kept byte-identical.
- **Hash manifests** — measured Shohoku's **actual on-disk** files: skill =
  the per-persona-memory ship (`25d2c223…`), OPERATING = the pre-redesign
  template (`fe942cf5…`). Added `25d2c223…` to `_PRIOR_SKILL_HASHES` and
  `7446e45e…` (pure-revamp OPERATING) to `_PRIOR_OPERATING_HASHES`, so a
  real `--refresh-skills` / `journal new` on Shohoku now refreshes **both**
  to the merged version (verified True at runtime). The footgun-guard tests
  still hold (current hashes are in neither prior set).

Tests after merge: **3190 passed, 100% line+branch coverage** held.
