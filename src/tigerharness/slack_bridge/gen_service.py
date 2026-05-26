"""Generate a systemd user unit for the multi-team slack-bridge.

Tigerharness ships an example unit at `examples/slack-bridge-multi.service`
but that template uses ``%h``-style systemd specifiers and placeholder
paths. This module emits a unit customized for *this* machine: the
running user's home, the project's venv path, and an
``EnvironmentFile=`` line pointing at a small env file the user is
expected to write (single line: ``TIGERHARNESS_BRIDGES_CONFIG=...``).

The user redirects the output to ``~/.config/systemd/user/`` and runs::

    systemctl --user daemon-reload
    systemctl --user enable --now slack-bridge-multi.service

Linux-only. macOS / other platforms get a friendly stderr message
explaining the manual setup.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def render_systemd_unit(
    *,
    teams_root: Path,
    bridges_config: Path,
    env_file: Path,
    venv_python: Path,
) -> str:
    """Build the unit file content with absolute paths baked in.

    All four paths are resolved + interpolated into the template so a
    cat-of-the-unit reads cleanly without needing systemd %-specifiers.
    """
    return f"""\
[Unit]
Description=Tigerharness multi-team Slack bridge (Socket Mode)
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
# internal _DRAIN_TIMEOUT_S (90s in current source).
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

# Logs go to `journalctl --user -u slack-bridge-multi`
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
            "customized for this machine. Redirect to "
            "~/.config/systemd/user/slack-bridge-multi.service."
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

    print(render_systemd_unit(
        teams_root=teams_root,
        bridges_config=bridges_config,
        env_file=env_file,
        venv_python=venv_python,
    ))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
