"""tigerharness — a generic Claude Code agent harness.

Sub-packages:
    tigerharness.agent_sdk     — backend-agnostic agent SDK
    tigerharness.journal       — file-based subscription backend
    tigerharness.slack_bridge  — Slack Socket Mode bridge to Claude
    tigerharness.tiger_memory  — persistent memory: archive, journal, briefing
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tigerharness")
except PackageNotFoundError:  # pragma: no cover  (only hits during in-tree dev without an install)
    __version__ = "0.0.0+unknown"
