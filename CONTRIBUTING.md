# Contributing to tigerharness

## Development setup

```bash
git clone https://github.com/DingyuZhou/TigerHarness.git
cd tigerharness
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

Coverage threshold: **100%** line + branch (enforced in `pyproject.toml`'s `[tool.coverage.report] fail_under = 100`). Current: **100.00%** (1700+ tests).

## Project structure

```
src/tigerharness/
    __init__.py              Top-level package
    cli.py                   Unified CLI entry point
    init.py                  Project scaffolding (tigerharness init)
    dismiss.py               Symmetric teardown (tigerharness dismiss)
    py.typed                 PEP 561 type stub marker
    agent_sdk/               Backend-agnostic agent SDK (swappable runtimes)
        types.py             AgentConfig + AgentBackend Protocol (the interface)
        factory.py           get_backend / register_backend / list_backends
        retry.py             run_with_retry (exponential backoff)
        errors.py            Exception hierarchy
    slack_bridge/            Slack Socket Mode bridge
        bridge.py            Event handler + dispatch
        config.py            Env-var-driven config loader
        downloader.py        File attachment download
        notify.py            Outbound DM/file CLI
        persistence.py       Thread -> session mapping
    tiger_memory/            Persistent memory management
        cli.py               CLI (init, bootstrap, rebuild, pin, resummarize, drill, tree, raw, search, state)
        config.py            YAML config loader
        lifecycle.py         Bootstrap / rebuild / resummarize engine
        briefing.py          Layered briefing rebuild
        drill.py             Read commands (drill, tree, raw, search)
        store.py             On-disk store + atomic write + locking
        must_memorize.py     Scored memo table with decay
        rag.py               Embedding-based semantic search
        embedders.py         Pluggable embedding backends
        frontmatter.py       YAML frontmatter parser/writer
        sources/             Source adapters (claude_code, docs)
        summarizers/         Summarizer backends (anthropic, mock)
        templates/           Briefing README template
    journal/                 File-based subscription backend (kind=task + kind=workflow)
        cli.py               new / list / status / sweep / claim / release / step-done
        compile_cli.py       Compile subcommands (compile-context ... validate-personas)
        models.py            status.json schema + state machine
        scaffold.py          Task / workflow scaffolding
        sweep.py             Lazy sweep (archive + fresh/stale classify)
        operating_template.py  OPERATING.md contract shipped into each journal
        wfcore/              Workflow compile core (models, drafter, critique
                             prompts, Tier-1 validators, trailer parser)
tests/
    agent_sdk/               Agent SDK tests
    slack_bridge/            Slack bridge tests
    tiger_memory/            Tiger memory tests
    journal/                 Journal backend tests (incl. wfcore/)
    test_main_modules.py     __main__.py entrypoint tests
examples/
    tigers/                  Sample team scaffolded by `tigerharness init`
    tiger-memory.config.yaml Standalone memory config reference
    env.example              Standalone Slack bridge env template
docs/
    agent_sdk.md             Agent SDK reference
    slack-bridge.md          Slack bridge module README
    tiger-memory.md          Tiger memory module README
    journal.md               Journal / subscription-backend operator quickstart
    subscription-backend.md  Subscription backend concept + status.json schema
    journal-workflow-mode.md kind=workflow compile + graph-walk deep dive
    adr/                     Architecture Decision Records (0003: legacy
                             runner removal + write-guard migration)
skills/                      Claude Code SKILL.md definitions
```

## Adding a new module

1. Create `src/tigerharness/<module>/` with `__init__.py`.
2. Add tests in `tests/<module>/`.
3. If it has CLI commands, add a `cli.py` and wire into `src/tigerharness/cli.py`.
4. If it has optional dependencies, add an extra in `pyproject.toml`.
5. Write a module README in `docs/<module>.md`.
6. Run `uv run pytest --cov=tigerharness --cov-report=term-missing` and verify coverage.

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
