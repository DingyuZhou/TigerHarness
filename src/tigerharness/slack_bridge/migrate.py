"""Rewrite a slack-bridge ``threads.json`` from the pre-routing schema
to the post-routing schema.

Pre-routing schema (pre-PR4)::

    {
        "1234.5678": "session-abc-def",
        "1234.5679": "session-xyz"
    }

Post-routing schema (PR4+)::

    {
        "1234.5678": {"session_id": "session-abc-def", "persona": "ayako"},
        "1234.5679": {"session_id": "session-xyz",     "persona": "ayako"}
    }

When you migrate a team from single-persona to multi-persona mode,
pre-existing entries have no persona attribution. The bridge falls
back to ``default_persona`` for them at dispatch time, but
tiger-memory's per-persona filter excludes them under strict mode
(``include_unattributed: false``). So the persona's memory loses
history.

This tool rewrites those entries with an explicit persona name so
they're included in that persona's memory after the migration. Run it
once, per team, against the bridge's ``threads.json`` for that team.

Usage::

    python -m tigerharness.slack_bridge.migrate \\
        --state-dir ~/.local/state/slack-bridge/shohoku/ \\
        --to ayako

    # See what would change without writing:
    python -m tigerharness.slack_bridge.migrate \\
        --state-dir ~/.local/state/slack-bridge/shohoku/ \\
        --to ayako --dry-run

The tool is idempotent: re-running on a fully-migrated file is a no-op.
Entries that already have the new shape are left untouched (so you can
mix-and-match -- only the pre-routing strings get rewritten).
"""
from __future__ import annotations

import logging

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("tigerharness.slack_bridge.migrate")


@dataclass(frozen=True)
class MigrationResult:
    rewritten: int       # entries converted from str -> dict
    already_new: int     # entries already in dict shape, left alone
    invalid_skipped: int # entries that were neither str nor valid dict


def migrate(
    state_dir: Path,
    target_persona: str,
    *,
    dry_run: bool = False,
) -> MigrationResult:
    """Read ``<state_dir>/threads.json``, rewrite pre-routing entries to
    the new shape, write atomically. Returns counts for reporting.

    Raises ``ValueError`` for unrecoverable problems (missing file,
    not a JSON object, empty persona name). Returns a no-op result
    if there's nothing to migrate.
    """
    if not target_persona or not target_persona.strip():
        raise ValueError("--to <persona> cannot be empty")
    persona = target_persona.strip()

    threads_json = (state_dir / "threads.json").expanduser()
    if not threads_json.exists():
        raise ValueError(f"threads.json not found at {threads_json}")
    try:
        data = json.loads(threads_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{threads_json} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(
            f"{threads_json} top-level must be a JSON object, "
            f"got {type(data).__name__}"
        )

    rewritten = 0
    already_new = 0
    invalid_skipped = 0
    new_data: dict[str, object] = {}
    for tts, val in data.items():
        if isinstance(val, str) and val:
            new_data[tts] = {"session_id": val, "persona": persona}
            rewritten += 1
        elif isinstance(val, dict) and isinstance(val.get("session_id"), str):
            new_data[tts] = val
            already_new += 1
        else:
            # Malformed entry -- drop or keep? Keep, since we can't know
            # what the user intends, but report it.
            new_data[tts] = val
            invalid_skipped += 1

    if rewritten > 0 and not dry_run:
        _atomic_write_json(threads_json, new_data)

    return MigrationResult(
        rewritten=rewritten,
        already_new=already_new,
        invalid_skipped=invalid_skipped,
    )


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically via tmpfile + os.replace -- same pattern
    the bridge uses so a crash mid-write can't corrupt the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        prefix=".threads.",
        suffix=".tmp",
    )
    try:
        with fd as tf:
            json.dump(data, tf, indent=2, sort_keys=True)
            tf.write("\n")
        os.replace(fd.name, path)
    except Exception:
        try:
            os.unlink(fd.name)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tigerharness.slack_bridge.migrate",
        description=(
            "Rewrite pre-routing threads.json entries to the new "
            "{session_id, persona} schema."
        ),
    )
    parser.add_argument(
        "--state-dir", required=True, type=Path,
        help="Directory containing threads.json (e.g. "
             "~/.local/state/slack-bridge/shohoku/)",
    )
    parser.add_argument(
        "--to", required=True, dest="target_persona",
        help="Persona name to attribute pre-routing entries to "
             "(must exist in the team's personas.yaml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change without writing the file.",
    )
    args = parser.parse_args(argv)

    try:
        result = migrate(
            args.state_dir, args.target_persona, dry_run=args.dry_run,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    verb = "would rewrite" if args.dry_run else "rewrote"
    print(f"{verb} {result.rewritten} pre-routing entries -> persona={args.target_persona!r}")
    print(f"left alone: {result.already_new} entries already in new schema")
    if result.invalid_skipped:
        print(
            f"warning: {result.invalid_skipped} malformed entries left "
            f"unchanged -- inspect manually",
            file=sys.stderr,
        )
    if result.rewritten == 0 and not args.dry_run:
        print("(no changes needed; threads.json unchanged)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
