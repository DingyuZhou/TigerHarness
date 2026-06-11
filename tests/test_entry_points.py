"""Test that entry point modules are importable and have expected attributes."""

from __future__ import annotations


def test_slack_bridge_main_module_exists():
    import importlib
    spec = importlib.util.find_spec("tigerharness.slack_bridge.__main__")
    assert spec is not None


def test_tigerharness_version():
    """`__version__` is sourced from package metadata; matches the wheel.

    Verifies the import shape (a non-empty string in PEP 440 form) rather
    than pinning a literal so the test doesn't have to be bumped on every
    release.
    """
    from tigerharness import __version__
    assert isinstance(__version__, str) and __version__
    assert __version__[0].isdigit()
