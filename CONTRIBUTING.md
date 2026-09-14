# Contributing to tigerharness

## Development setup

```bash
git clone https://github.com/DingyuZhou/TigerHarness.git
cd TigerHarness
uv sync --extra all
```

## Running tests

```bash
# All tests
uv run pytest

# With coverage report
uv run pytest --cov=tigerharness --cov-report=term-missing

# Single module
uv run pytest tests/tiger_memory/test_lifecycle_full.py -v
```

Coverage threshold: **100%** line + branch (enforced in `pyproject.toml`'s `[tool.coverage.report] fail_under = 100`). Current: **100.00%** (3,800+ tests).

> **Trap:** plain `uv run pytest` does NOT enforce the floor -- the
> gate only fires with `--cov` (second command above). A green plain
> run proves tests pass, not that coverage holds. CI runs the gated
> form.

> **Env-independent:** the suite is safe to run from inside a Slack
> bridge session -- a top-level `tests/conftest.py` autouse fixture
> scrubs the bridge-injected env vars (`TIGERHARNESS_SLACK_THREAD_TS`,
> `TIGERHARNESS_BRIDGES_CONFIG`) before each test, so you get CI's clean
> result without any manual `env -u ...` dance.

## Code review

Every change is reviewed against the
[code review standard](docs/code-review-standard.md) — numbered,
citable sections covering correctness, tests, error paths, atomicity,
docs, security, scope, dependencies, self-critique, commit hygiene,
and review verdicts. Read it before opening a PR; reviewers cite it
by section ("standard §2").

## Project structure

```
src/tigerharness/
    __init__.py              Top-level package
    cli.py                   Unified CLI entry point (init / dismiss / journal / tiger-memory / slack-bridge / autodrive)
    init.py                  Project scaffolding (tigerharness init; --refresh brings a team current)
    dismiss.py               Symmetric teardown (tigerharness dismiss)
    vendors.py               Model vendors (ADR 0011): vendor -> backend/CLI map and
                             per-persona vendor/model resolution from configs/personas.yaml
    _logging.py              Logging setup (TIGERHARNESS_LOG_LEVEL); tests/test_logging_audit.py enforces its use
    py.typed                 PEP 561 type stub marker
    _bundled_skills/         The SKILL.md set `tigerharness init` installs
                             into a team. Hash-gated by init.py's two
                             manifests; the only skill tree in this repo
    agent_sdk/               Backend-agnostic agent SDK (swappable runtimes)
        types.py             AgentConfig + AgentBackend Protocol (the interface)
        factory.py           get_backend / register_backend / list_backends
        retry.py             run_with_retry (exponential backoff)
        errors.py            Exception hierarchy
        backends/            claude_p (claude -p), codex_exec (codex exec),
                             anthropic_sdk (claude-agent-sdk), openai_sdk (stub)
    autodrive/               Periodic journal driver daemon (ADR 0010; one drive per lane, ADR 0012)
        cli.py               start / stop / status
        runner.py            The loop: non-AI probes, lane drives, rescue hold, auto-stop
        settings.py          Team knobs read from configs/.env (TIGERHARNESS_AUTODRIVE_*)
        notifier.py          Slack heartbeats + threaded drive summaries
    slack_bridge/            Slack Socket Mode bridge
        bridge.py            Event handler + dispatch (one backend per vendor, per persona)
        multi.py             Multi-team loader (lanes index -> per-team bridges)
        config.py            Env-var-driven config loader
        downloader.py        File attachment download
        notify.py            Outbound DM/file CLI
        notify_health.py     Transport-health sidecar for notify
        persistence.py       Thread -> session (+ backend) mapping
        idle_compact.py      Idle compaction pass (ADR 0004; claude_p sessions only)
        progress.py          Turn-progress heartbeats to the ops-log channel
        reconnect.py         Socket liveness watchdog + catch-up replay
        history.py           Thread-history fetch for untracked-thread joins
        router.py            One-shot LLM persona routing
        gen_service.py       systemd unit generator
        migrate.py           threads.json migration tool
    tiger_memory/            Persistent bounded memory (3 stores: skills / must_remember / topics)
        cli.py               CLI (init, rebuild, pin, migrate-to-topics, state, plan, ingest-*,
                             build-reduce-prompts, compact-*, card-check, team-events-compact-*,
                             sweep-*, check, search, forget, doctor)
        config.py            YAML config loader + validation
        lifecycle.py         Extraction -> ingest core + fresh-start rebuild
        bounded_store.py     Bounded-store engine + forget guard
        entries.py           Entry schemas (skill / must_remember / topic)
        briefing.py          Session-start briefing assembly
        compaction.py        Staged compaction for the three bounded stores
        indexes.py           Index + detail renderers (skills / topics)
        skills.py            Skill-importance scoring
        ranking.py           Recency / date-math helpers for keep-ranking
        prefilter.py         Transcript pre-filter
        executor.py          In-session sub-agent write-back to the stores
        cursor.py            Per-session incremental-sweep cursors
        sweep.py             Team-sweep gating (non-AI bookkeeping; per-lane restriction)
        state.py             State snapshot payload (tiger-memory state)
        check.py             Store-format validation gate
        inspect_tools.py     Operator read/fix loop (search / forget / doctor)
        team_events.py       Team-wide event log (lazy, self-compacting)
        migrate_topics.py    One-off migration to the topic-store model
        store.py             On-disk layout + atomic write + locking
        frontmatter.py       YAML frontmatter parser/writer
        sources/             Source adapters: claude_code + codex (shared base _transcripts.py),
                             journal_worklog; slack_thread rides the claude_code adapter;
                             docs has an unconstructed adapter, auto_memory none
        summarizers/         Summarizer backends (anthropic, mock)
        templates/           Briefing README template
    journal/                 File-based subscription backend (kind=task + kind=workflow)
        cli.py               20 verbs: new / list / status / sweep / claim / release / step-done /
                             defer / materialize / answer / abort / ... (see `journal --help`)
        compile_cli.py       Compile subcommands (compile-context ... validate-personas)
        models.py            status.json schema + state machine
        scaffold.py          Task / workflow scaffolding
        sweep.py             Lazy sweep (archive + idle/busy/crashed classify)
        lanes.py             Drive lanes (ADR 0012): work owner + vendor/model lane gate
        deferred.py          The deferred/ inbox (cheap Slack-side scheduling)
        drive_sessions.py    Registry of drive sessions (memory double-count suppression)
        paths.py             Journal root resolution (team root / TIGERHARNESS_JOURNAL_DIR)
        walk.py              Workflow graph-walk state
        worklog.py           Persona-stamped worklog notes
        schedule.py          Recurring schedule definitions (DEPRECATED, ADR 0010)
        operating_template.py  OPERATING.md contract shipped into each journal
        wfcore/              Workflow compile core (models, drafter, critique
                             prompts, Tier-1 validators, trailer parser)
tests/
    agent_sdk/               Agent SDK tests (incl. test_codex_exec.py)
    slack_bridge/            Slack bridge tests
    tiger_memory/            Tiger memory tests
    journal/                 Journal backend tests (incl. wfcore/ and lanes)
    test_autodrive*.py       Autodrive daemon + self-driving tests
    test_vendors.py          Vendor / policy resolution tests
    test_init.py             Scaffolder tests
    test_skill_hash_guard.py Bundled-skill manifest guard
    test_docs_refs_guard.py  Docs cross-reference guard
    test_main_modules.py     __main__.py entrypoint tests
examples/
    tigers/                  Sample team scaffolded by `tigerharness init --yes`
                             (regenerate after scaffold or skill changes; see "Examples" below)
    tiger-memory.config.yaml Standalone memory config reference
    env.example              Standalone team configs/.env template (Slack tokens + team knobs)
    slack-bridge-multi.service
                             Reference systemd unit for the multi-team bridge
docs/
    INDEX.md                 Docs home: one-hop router + must-not-miss rules (start here)
    overall.md               Redirect stub to INDEX.md
    agent_sdk.md             Agent SDK reference (backends, per-persona vendors)
    autodrive.md             The periodic journal driver (+ drive lanes)
    autodrive-notifications.md  Its Slack heartbeats and drive summaries
    journal.md               Journal / subscription backend, end to end
    journal-workflow-mode.md kind=workflow compile + graph walk
    journal-instant-resume.md  How a crashed / idle task resumes
    journal-operator-questions.md  Parking a task on an Operator question
    subscription-backend.md  Rails / billing + the status.json schema
    per-persona-journal-memory.md  The persona-stamped worklog memory rail
    slack-bridge.md          The 1..N-lane Socket Mode bridge
    tiger-memory.md          Bounded memory stores, CLI, config
    tiger-memory-sweep-protocol.md  The team-wide memory sweep
    DESIGN-memory.md         Memory design rationale
    code-review-standard.md  The review standard
    adr/                     Architecture Decision Records 0001-0013
                             (annotated list in docs/INDEX.md)
```

Skills live in exactly one place: `src/tigerharness/_bundled_skills/`.
That is the set `tigerharness init` installs into a team, and the one
`tests/test_skill_hash_guard.py` guards. (A top-level `skills/` tree
existed until 2026-08-14; it was packaged nowhere, drifted behind the
bundle, and was removed.)

## Examples

`examples/tigers/` is not hand-edited: it is the literal output of the
scaffolder, so a reader sees exactly what `tigerharness init` writes.
Regenerate it whenever a scaffold template, `.gitignore` template or
bundled skill changes (a bundled-skill edit also rolls its hash in
`init.py`, see "Project structure"):

```bash
rm -rf examples/tigers && tmp=$(mktemp -d) \
  && uv run tigerharness init --team tigers --persona chief --yes --dir "$tmp" \
  && uv run tigerharness init --team tigers --persona scout --yes --dir "$tmp" \
  && for p in chief scout; do \
       uv run tigerharness tiger-memory --config "$tmp/tigers/memories/$p/tiger-memory.config.yaml" init; done \
  && cp -a "$tmp/tigers" examples/tigers \
  && sed -i 's#^\(    project_path: \).*#\1~/.claude/projects/<encoded-team-root>/#' \
       examples/tigers/memories/*/tiger-memory.config.yaml \
  && git add -A examples/tigers
```

Two deliberate deviations from a raw scaffold, both in that recipe: the
memory store is initialized by hand (init's own auto-init is
intermittently flaky -- see the README's known limitations), which is
what leaves the tracked `memories/<persona>/journal/.gitkeep`; and
`project_path` -- a machine-specific encoding of the team root -- is
rewritten to a placeholder. `init` does not seed a `journal_worklog`
source; teams add one per `docs/per-persona-journal-memory.md`.
The nested `.gitignore` keeps the generated `configs/.env` out of git.
The standalone files next to it (`tiger-memory.config.yaml`,
`env.example`, `slack-bridge-multi.service`) are hand-maintained
references.

## Adding a new module

1. Create `src/tigerharness/<module>/` with `__init__.py`.
2. Add tests in `tests/<module>/`.
3. If it has CLI commands, add a `cli.py` and wire into `src/tigerharness/cli.py`.
4. If it has optional dependencies, add an extra in `pyproject.toml`.
5. Write a module README in `docs/<module>.md`.
6. Run `uv run pytest --cov=tigerharness --cov-report=term-missing` and verify coverage.
7. A new **model vendor** is registered in `src/tigerharness/vendors.py`
   (`VENDOR_BACKENDS`, the alias map, `VENDOR_CLIS`) with its backend under
   `src/tigerharness/agent_sdk/backends/`; everything else (bridge,
   autodrive lanes, idle compaction, `init`'s vendor menu) resolves through
   that module.

## Adding a custom persona

Run `tigerharness init` -- it walks you through picking (or creating) a
team and scaffolds the persona inside it. Non-interactive:

```bash
tigerharness init --persona analyst --team tigers --yes
export TIGERHARNESS_PERSONAS_CONFIG=./tigers/configs/personas.yaml
tigerharness journal new --kind task --persona analyst --prd brief.md
```

The team folder structure is documented in the [README](README.md) and
in `examples/tigers/`. Each persona lives at
`<team>/personas/<name>/prompt.md` (edit this) with optional memory
config at `<team>/memories/<name>/tiger-memory.config.yaml`. The
generated `<team>/configs/personas.yaml` is the team registry (the
yaml shape is documented in its own preamble comment).

`init` also writes `<team>/configs/repos.yaml` -- the team's
path-indirection map (`team_root: .` plus `project:`, the relative
path to the tigerharness checkout, auto-detected case-insensitively
from directories near the team dir -- the scan walks up to 3 levels,
checking each level's immediate children for a matching
`pyproject.toml`; a miss writes a commented placeholder to fill in). Team prose and config should reference paths
relative to the team root -- sessions launch there -- so the same
checked-in team repo works on any machine. Existing teams adopt
`repos.yaml` automatically the next time `init` adds a persona;
already-absolute `settings.json` env values are yours to relativize
by hand (init never rewrites user-owned settings).

## Adding a custom memory backend

Tiger-memory's summarizer is pluggable:

1. Subclass `tigerharness.tiger_memory.summarizers.base.Summarizer`.
2. Implement `summarize(prompt, max_words) -> str` and `cost_estimate_usd(...)`.
3. Register in `_build_summarizer()` in `lifecycle.py` (or propose a plugin hook).

## Code style

- Python 3.11+
- Type hints on all public APIs
- Docstrings on all modules and public functions
- No hardcoded paths -- everything via env vars or config
- Tests use `tmp_path` fixtures for filesystem isolation
- Mock external services (Slack, Anthropic, subprocess) in tests

## Commit conventions

```
<prefix>: <imperative summary, 72 chars max>

<body>
```

Prefix: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`

## Running a single sub-package

```bash
# Journal backend
tigerharness journal --help

# Slack bridge (daemon)
python -m tigerharness.slack_bridge

# Tiger memory
tiger-memory --help
# or: python -m tigerharness.tiger_memory.cli --help
```

## Building

```bash
uv build
# Creates dist/tigerharness-<version>-py3-none-any.whl
```

## Releasing

Releases are published to PyPI by [`.github/workflows/release.yml`](.github/workflows/release.yml),
which fires whenever a `v*` tag is pushed. It uses **PyPI Trusted
Publishing** (OIDC) — no API token in any secret, no manual
`twine upload`.

### One-time setup (per project)

Trusted Publishing only works after the publisher is registered on the
PyPI side. This is a one-time manual step.

1. Visit https://pypi.org/manage/project/tigerharness/settings/publishing/
   (must be logged in as a PyPI maintainer of the project).
2. Under **"Add a new publisher"** → **GitHub**, fill in *exactly*:
   - **PyPI Project Name:** `tigerharness`
   - **Owner:** `DingyuZhou`  *(GitHub URL slug — no spaces, exact case)*
   - **Repository name:** `TigerHarness`
   - **Workflow name:** `release.yml`
   - **Environment name:** *(leave blank)*
3. Click **Add**.

If you forget this step, the workflow's "Publish to PyPI" step fails
with a 4xx from PyPI. The build step before it still succeeds, so the
wheel is built correctly — re-run the workflow after registering.

### Per-release recipe

```bash
# 1. Bump the version in pyproject.toml (and any other versioned files).
$EDITOR pyproject.toml          # e.g. 0.1.4 -> 0.1.5

# 2. Commit the bump on a branch, open + merge a PR, then on main:
git checkout main && git pull --ff-only

# 3. Tag the merge commit. The tag name must match `v*`.
git tag -a v0.1.5 -m "v0.1.5 -- short summary"
git push origin v0.1.5

# 4. Watch the workflow:
# https://github.com/DingyuZhou/TigerHarness/actions/workflows/release.yml

# 5. Once green, confirm on PyPI:
curl -fsS https://pypi.org/pypi/tigerharness/0.1.5/json >/dev/null && echo OK
```

The workflow takes ~1-2 minutes. After it lands, downstream consumers
can bump with `uv lock --upgrade-package tigerharness` (or
`uv add -U tigerharness` if they want the version constraint widened).

### Versioning policy

We follow SemVer compatible-release semantics (`~=0.1.2` allows
`>=0.1.2, <0.2.0`):

- **Patch** (`0.1.x`) — bug fixes, doc improvements, additive options.
- **Minor** (`0.x.0`) — new sub-packages, new CLI verbs, backwards-
  compatible feature work that downstream pins (`~=0.x.0`) won't catch.
- **Major** (`x.0.0`) — breaking changes to the CLI surface, generated
  layouts, or persona/memory config schemas.

### Re-running a failed release

The workflow triggers on tag push, so the cleanest re-trigger is via
the GitHub UI: open the failed run and click **"Re-run failed jobs"**.
Avoid deleting + re-pushing the tag — anyone who fetched the original
will see history churn.
