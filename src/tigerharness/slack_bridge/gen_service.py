"""Generate a systemd user unit for the multi-team slack-bridge.

Tigerharness ships an example unit at `examples/slack-bridge-multi.service`
but that template uses ``%h``-style systemd specifiers and placeholder
paths. This module emits a unit customized for *this* machine: the
running user's home, the project's venv path, and an
``EnvironmentFile=`` line pointing at a small env file the user is
expected to write (single line: ``TIGERHARNESS_BRIDGES_CONFIG=...``).

The user redirects the output to ``~/.config/systemd/user/`` under the
**per-root unit name** the command prints on stderr (e.g.
``slack-bridge-teams-1a2b3c.service``), then runs::

    systemctl --user daemon-reload
    systemctl --user enable --now <printed-unit-name>

The unit name is derived from the teams root so every root owns its
own bridge instance: two roots can never collide on one global unit,
and ``tigerharness dismiss`` can match a unit back to its root (it
scans ``slack-bridge-*.service`` and checks which root each unit's
config resolves into -- the name is a convenience, the content is the
truth). The scan glob is deliberately broad so it still finds units
named under the older ``slack-bridge-multi-<root>-<hash>`` scheme and
the legacy global ``slack-bridge-multi.service``.

Linux-only. macOS / other platforms get a friendly stderr message
explaining the manual setup.
"""
from __future__ import annotations

import hashlib
import logging
import re

import argparse
import sys
from pathlib import Path

log = logging.getLogger("tigerharness.slack_bridge.gen_service")


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def derive_unit_name(teams_root: Path) -> str:
    """Per-root systemd unit name for the multi-team bridge.

    ``<basename>-<hash6>`` keeps the name human-readable (the basename
    says which root at a glance) while the 6-hex digest of the FULL
    resolved path keeps two roots that share a basename (~/a/teams vs
    ~/b/teams) from colliding on one unit. Deterministic, so re-running
    gen-service for the same root always names the same unit.
    """
    resolved = teams_root.resolve()
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", resolved.name).strip("-") or "root"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:6]
    return f"slack-bridge-{base}-{digest}.service"


def render_systemd_unit(
    *,
    teams_root: Path,
    bridges_config: Path,
    env_file: Path,
    venv_python: Path,
    unit_name: str = "slack-bridge-multi.service",
) -> str:
    """Build the unit file content with absolute paths baked in.

    All four paths are resolved + interpolated into the template so a
    cat-of-the-unit reads cleanly without needing systemd %-specifiers.
    *unit_name* only customizes the journalctl hint comment -- systemd
    derives the real unit identity from the installed filename.
    """
    journal_hint = unit_name.removesuffix(".service")
    return f"""\
[Unit]
Description=Tigerharness multi-team Slack bridge for {teams_root} (Socket Mode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={teams_root}

# If the venv is missing or corrupt, systemd will fail at ExecStart
# with status=203/EXEC and name the missing binary in the journal.
# (Earlier versions of this template carried an `ExecStartPre=test`
# pre-check, but `test`'s absolute path varies by distro -- the
# NixOS path leaked into the template and broke on every other
# distro. Dropping the duplicate check is portable + the systemd
# error is already clear.)
ExecStart={venv_python} -m tigerharness.slack_bridge

Restart=on-failure
RestartSec=5

# Drain budget across all lanes (concurrent). Must exceed the bridge's
# internal _DRAIN_TIMEOUT_S (90s in current source). The ordering is
# enforced by tests/slack_bridge/test_drain_budget_invariant.py, which
# parses this value out of the rendered template -- the template is all
# it can see. If you are reading this inside an installed unit, it is a
# snapshot: a change to the source reaches you only on a `gen-service`
# re-run, and no test can tell that you have not done one.
TimeoutStopSec=120

# Only SIGTERM the parent so claude_p children finish posting their
# reply before dying. Without `mixed`, replies get cut off mid-stream
# on restart.
KillMode=mixed

# This file should contain ONLY:
#     TIGERHARNESS_BRIDGES_CONFIG={bridges_config}
# Per-lane tokens stay in each team's own configs/.env, referenced
# from the YAML index.
EnvironmentFile={env_file}

# Logs go to `journalctl --user -u {journal_hint}`
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tigerharness slack-bridge gen-service",
        description=(
            "Emit a systemd user-unit for the multi-team slack-bridge, "
            "customized for this machine. Redirect to the per-root unit "
            "path printed on stderr (each teams root gets its own unit "
            "name, so multiple roots never share one bridge instance)."
        ),
    )
    parser.add_argument(
        "--teams-root", default=".", type=Path,
        help="Directory containing the top-level slack-bridge.yaml index "
             "(default: current directory).",
    )
    parser.add_argument(
        "--bridges-config", default=None, type=Path,
        help="Path to the slack-bridge.yaml index "
             "(default: <teams-root>/slack-bridge.yaml).",
    )
    parser.add_argument(
        "--env-file", default=None, type=Path,
        help="Path to the multi-bridge env file containing only "
             "TIGERHARNESS_BRIDGES_CONFIG=... "
             "(default: <teams-root>/multi-bridge.env).",
    )
    parser.add_argument(
        "--venv-python", default=None, type=Path,
        help="Python binary the unit will exec "
             "(default: <teams-root>/.venv/bin/python).",
    )
    args = parser.parse_args(argv)

    if not _is_linux():
        print(
            f"warning: gen-service only emits a systemd unit (Linux). "
            f"You're on {sys.platform!r}. Set up your own runtime "
            f"target -- the command to run is: "
            f"`<venv-python> -m tigerharness.slack_bridge` "
            f"with TIGERHARNESS_BRIDGES_CONFIG set.",
            file=sys.stderr,
        )
        return 1

    teams_root = args.teams_root.resolve()
    bridges_config = (
        args.bridges_config.resolve()
        if args.bridges_config else (teams_root / "slack-bridge.yaml")
    )
    env_file = (
        args.env_file.resolve()
        if args.env_file else (teams_root / "multi-bridge.env")
    )
    venv_python = (
        args.venv_python.resolve()
        if args.venv_python else (teams_root / ".venv" / "bin" / "python")
    )

    unit_name = derive_unit_name(teams_root)
    print(render_systemd_unit(
        teams_root=teams_root,
        bridges_config=bridges_config,
        env_file=env_file,
        venv_python=venv_python,
        unit_name=unit_name,
    ))
    # Instructions go to stderr so `gen-service > unit-file` redirects
    # stay clean. The per-root name is the whole point -- spell it out.
    print(
        f"# Save as: ~/.config/systemd/user/{unit_name}\n"
        f"# Then:    systemctl --user daemon-reload\n"
        f"#          systemctl --user enable --now {unit_name}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
