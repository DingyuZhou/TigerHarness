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

Coverage threshold: **98.5%** (enforced in `pyproject.toml`). Current: **98.60%** (1088 tests).

## Project structure

```
src/tigerharness/
    __init__.py              Top-level package
    cli.py                   Unified CLI entry point
    init.py                  Project scaffolding (tigerharness init)
    py.typed                 PEP 561 type stub marker
    task_runner/             Iterative task execution
        cli.py               CLI subcommands (assign, list, cancel, logs)
        runner.py            Core iteration loop
        notifier.py          Slack DM notifications
        personas.py          Config-driven persona registry
        registry.py          Task state persistence
    slack_bridge/            Slack Socket Mode bridge
        bridge.py            Event handler + dispatch
        config.py            Env-var-driven config loader
        downloader.py        File attachment download
        notify.py            Outbound DM/file CLI
        persistence.py       Thread -> session mapping
    tiger_memory/            Persistent memory management
        cli.py               CLI (init, rebuild, search, drill, pin)
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
tests/
    task_runner/             Task runner tests
    slack_bridge/            Slack bridge tests
    tiger_memory/            Tiger memory tests
    test_main_modules.py     __main__.py entrypoint tests
examples/
    tigers/                  Sample team scaffolded by `tigerharness init`
    tiger-memory.config.yaml Standalone memory config reference
    env.example              Standalone Slack bridge env template
docs/
    DESIGN.md                Architecture decisions + migration notes
    task-runner.md           Task runner module README
    slack-bridge.md          Slack bridge module README
    tiger-memory.md          Tiger memory module README
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
python -m tigerharness.task_runner assign --to analyst --prompt "..." --iters 5
```

The team folder structure is documented in the [README](README.md) and
in `examples/tigers/`. Each persona lives at
`<team>/personas/<name>/prompt.md` (edit this) with optional memory
config at `<team>/memories/<name>/tiger-memory.config.yaml`. The
generated `<team>/configs/personas.yaml` is the registry consumed by
`tigerharness.task_runner.personas.load_personas_config`.

If you need to register a persona programmatically (e.g. for tests),
use `register_persona()` from `tigerharness.task_runner.personas`.

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
# Task runner
python -m tigerharness.task_runner --help

# Slack bridge (daemon)
python -m tigerharness.slack_bridge

# Tiger memory
tiger-memory --help
# or: python -m tigerharness.tiger_memory.cli --help
```

## Building

```bash
uv build
# Creates dist/tigerharness-0.1.0-py3-none-any.whl
```
