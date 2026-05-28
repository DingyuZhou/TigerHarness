"""End-to-end tests for the workflow-runner.

Each scenario in this package drives a synthetic 3-step playbook
through the public ``tigerharness.workflow_runner.cli`` entry point
against a scripted fake ``claude`` binary, then asserts on the
resulting ``events.jsonl`` + ``status.json``. The goal is to prove
that the trailer parser, session manager, models, locks, events,
CLI, and executor compose correctly on a real (if synthetic) task.

See ``conftest.py`` for the shared driver harness fixtures and
``steps/`` for the canonical 3-step playbook.
"""
