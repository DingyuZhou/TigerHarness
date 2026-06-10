"""configure_cli_logging: one parser, one default, one test surface."""

from __future__ import annotations

import logging

import pytest

from tigerharness._logging import ENV_VAR, configure_cli_logging


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)


def test_default_is_warning(monkeypatch):
    assert configure_cli_logging() == logging.WARNING


def test_env_overrides_case_insensitively(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "debug")
    assert configure_cli_logging() == logging.DEBUG


def test_unrecognized_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "verbose")
    assert configure_cli_logging() == logging.WARNING


def test_per_cli_default(monkeypatch):
    assert configure_cli_logging(default="INFO") == logging.INFO


def test_env_beats_per_cli_default(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "ERROR")
    assert configure_cli_logging(default="INFO") == logging.ERROR
