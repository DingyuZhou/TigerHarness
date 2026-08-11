# ADR 0009 — Remove the single-tenant Slack bridge fallback

- Status: accepted
- Date: 2026-08-11
- Deciders: Operator (direction), Shohoku (execution; journal task
  `20260811-184001-docs-sync-follow-ups`)

## Context

The bridge originally ran one team from process env / `.env`
(`config.load()` in `slack_bridge/config.py`, `_run_single` in
`slack_bridge/__main__.py`). The multi-lane index
(`TIGERHARNESS_BRIDGES_CONFIG` → `slack-bridge.yaml`, loaded by
`multi.load_multi()`) superseded it: N teams, one process, one
Socket-Mode connection per lane, per-lane logs. A single team is just
a one-lane index, so the env fallback bought nothing except a second
startup path to document, test, and keep from drifting. The fallback
was deprecated with a startup warning on 2026-06-13 (`46c5c5c`,
non-breaking) and every known deployment migrated during the
portable-team-configs work (2026-08-03..10).

## Decision

Remove the single-tenant path outright; the lanes index is the only
way to run the bridge.

- `__main__.py`: `_run_single` and `_warn_single_tenant_deprecated`
  are gone. `main()` now **fails fast** when
  `TIGERHARNESS_BRIDGES_CONFIG` is unset, with an error naming this
  ADR and the migration steps — no silent fallback.
- `config.py`: the env-reading `load()` is gone. `BridgeConfig` and
  `bridge.build_bridge` (the single-persona factory) **stay** — they
  are the composition seam used by embedders and tests; only the
  process entrypoint stopped calling them.
- The log-family-V secret-redaction seam moved with the token
  loading: `config.redact_token()` (prefix/suffix only, never a full
  secret) is now emitted per lane in `multi._build_lane`.

**Canonical allowlist env name.** The per-lane `.env` reads
`SLACK_ALLOWED_USER_IDS`. The legacy spelling
`ALLOWED_SLACK_USER_IDS` survives in exactly one place: the notify
CLI's read-side fallback. Templates, docs, and `init` scaffolding
emit only the canonical name.

**Dead env vars dropped from templates/docs** (no reader after the
removal): `TIGERHARNESS_AGENT_CWD`, `TIGERHARNESS_PERSONAS_DIR`,
`TIGERHARNESS_AGENT_PROMPT`, `TIGERHARNESS_SLACK_BRIDGE_DIR`.

## Migration note — stranded single-tenant deployments

A deployment that still launches the bridge without the index var now
exits at startup with the pointer instead of serving Slack. To
migrate: write a one-lane `slack-bridge.yaml`, set
`TIGERHARNESS_BRIDGES_CONFIG` to it, and regenerate the systemd unit
with `tigerharness slack-bridge gen-service` (never hand-edit the
unit). Full walkthrough: `docs/slack-bridge.md`
("Migrating off single-tenant").

**Stale-checkout warning (ADR 0003 precedent).** A service unit or
wrapper script pinned to a pre-removal checkout/venv will keep
running the old env-fallback silently — the fail-fast only protects
deployments actually running this version. Editable installs should
also clear stray `src/tigerharness/slack_bridge/__pycache__/` after
pulling: orphaned bytecode can leave the deleted `config.load` /
`_run_single` importable and mask the removal from tests.

## Consequences

- One startup story: index → lanes → bridges. Docs and tests cover a
  single path.
- `examples/env.example` documents the per-lane `.env` shape (tokens
  + `SLACK_ALLOWED_USER_IDS` + optional `TIGER_MEMORY_CLI`), not a
  process-env deployment.
- The 100% coverage gate holds through the removal; the deprecation
  tests were replaced by fail-fast coverage of the new `main()`.
- ADRs 0001/0002 and older design docs keep their historical
  references; this ADR is the tombstone for the single-tenant path.
