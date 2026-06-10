"""The logging-coverage audit, enforced by the suite forever.

Every module under ``src/tigerharness`` must either:

* (A) attach its named logger — ``logging.getLogger(__name__)`` or
  the literal dotted form
  ``logging.getLogger("tigerharness.<package>.<module>")`` — or
* (B) appear in :data:`NO_LOGGER_BY_AUDIT` below, the reviewed
  allowlist of pure-data / pure-function modules where a logger
  with zero call sites would be noise, not coverage.

Adding decision logic to a (B) module? Move it to (A): give it the
logger and delete its allowlist row — this test will hold you to it.
The same classification appears, with reasons, in the team log map
(teams/Shohoku/knowledge/tigerharness-log-map.md).
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "tigerharness"

#: (B) — reviewed: no decision points; errors surface as exceptions
#: handled (and logged) by their callers.
NO_LOGGER_BY_AUDIT = {
    "tigerharness",                       # package __init__ (metadata only)
    "tigerharness._logging",              # the bootstrap itself
    "tigerharness.agent_sdk",             # re-exports
    "tigerharness.agent_sdk.errors",      # exception hierarchy
    "tigerharness.agent_sdk.types",       # dataclass/Protocol surface
    "tigerharness.agent_sdk.backends",    # re-exports
    "tigerharness.agent_sdk.backends._base",  # shared pure helpers
    "tigerharness.agent_sdk.examples",    # demo scripts (coverage-omitted)
    "tigerharness.agent_sdk.examples.basic",
    "tigerharness.agent_sdk.examples.builtin_tools",
    "tigerharness.agent_sdk.examples.multi_turn",
    "tigerharness.agent_sdk.examples.streaming",
    "tigerharness.agent_sdk.backends.openai_sdk",  # explicit stub (ADR 0003 era)
    "tigerharness.cli",                   # thin dispatch; subcommands log
    "tigerharness.journal",               # re-exports
    "tigerharness.journal.ids",           # pure id helpers
    "tigerharness.journal.models",        # dataclasses + (de)serialization
    "tigerharness.journal.operating_template",  # template text constant
    "tigerharness.journal.paths",         # pure path resolution
    "tigerharness.journal.wfcore",        # re-exports
    "tigerharness.journal.wfcore.critique",   # pure prompt builders
    "tigerharness.journal.wfcore.drafter",    # pure prompt/parse (raises)
    "tigerharness.journal.wfcore.ids",        # pure validators
    "tigerharness.journal.wfcore.models",     # dataclasses
    "tigerharness.journal.wfcore.pipeline",   # pure assembly
    "tigerharness.journal.wfcore.trailer",    # pure parser
    "tigerharness.journal.wfcore.validators", # pure validators (results
                                              # logged by compile_cli)
    "tigerharness.slack_bridge",          # re-exports
    "tigerharness.tiger_memory",          # re-exports
    "tigerharness.tiger_memory.frontmatter",  # pure parser/writer
    "tigerharness.tiger_memory.metrics",      # pure counters
    "tigerharness.tiger_memory.sources",      # re-exports
    "tigerharness.tiger_memory.sources.base",  # ABC surface
    "tigerharness.tiger_memory.summarizers.mock",  # deterministic test double
    "tigerharness.tiger_memory.summarizers",  # re-exports
    "tigerharness.tiger_memory.summarizers.base",  # ABC surface
}


def _dotted(py: Path) -> str:
    rel = py.relative_to(SRC.parent)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _modules() -> list[Path]:
    out = []
    for py in SRC.rglob("*.py"):
        if "__pycache__" in py.parts or "_bundled_skills" in py.parts:
            continue
        if py.name == "__main__.py":
            # __main__ shims delegate to mains that configure logging.
            continue
        out.append(py)
    return out


def test_every_module_logs_or_is_allowlisted() -> None:
    missing, stale = [], set(NO_LOGGER_BY_AUDIT)
    for py in _modules():
        mod = _dotted(py)
        text = py.read_text(encoding="utf-8")
        has = (
            f'logging.getLogger("{mod}")' in text
            or "logging.getLogger(__name__)" in text
        )
        if mod in NO_LOGGER_BY_AUDIT:
            stale.discard(mod)
            assert not has or mod == "tigerharness._logging", (
                f"{mod} is allowlisted (B) but attaches a logger -- "
                "move it to (A): delete its allowlist row"
            )
        elif not has:
            missing.append(mod)
    assert not missing, (
        "modules without their named logger and not allowlisted: "
        f"{sorted(missing)}"
    )
    assert not stale, f"allowlist rows with no matching module: {sorted(stale)}"


def test_basicconfig_only_at_entry_points() -> None:
    allowed = {
        "tigerharness._logging",          # the helper itself
        "tigerharness.slack_bridge.__main__",  # daemon's richer setup
    }
    offenders = []
    for py in SRC.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        mod = _dotted(py)
        if "basicConfig" in py.read_text(encoding="utf-8"):
            if mod not in allowed:
                offenders.append(mod)
    assert not offenders, f"basicConfig outside entry points: {offenders}"
