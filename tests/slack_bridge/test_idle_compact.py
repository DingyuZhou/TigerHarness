"""ADR 0004 idle-compaction tests: the disabled default FIRST (the
contract), config parsing fail-soft, fraction math, the idle
predicate, the trigger truth table, one-per-idle-period, and the
never-raises promise."""

from __future__ import annotations

from pathlib import Path

import pytest

from tigerharness.journal.models import Status
from tigerharness.journal.paths import JournalPaths
from tigerharness.slack_bridge.idle_compact import (
    IdleCompactConfig,
    context_fraction,
    journal_is_idle,
    maybe_compact,
    should_compact,
)

HOT_USAGE = {
    "input_tokens": 10,
    "cache_creation_input_tokens": 30_000,
    "cache_read_input_tokens": 50_000,
}  # 80_010 / 200_000 = ~0.40


def _journal(tmp_path: Path, *, with_pending: bool = False) -> Path:
    root = tmp_path / "journal"
    (root / "active").mkdir(parents=True)
    if with_pending:
        tdir = root / "active" / "20260611-x-aaaa"
        tdir.mkdir()
        (tdir / "status.json").write_text(
            Status.new(id="20260611-x-aaaa", title="t", persona="P")
            .to_json()
        )
    return root


def _cfg(root: Path, **kw) -> IdleCompactConfig:
    return IdleCompactConfig(
        enabled=True, journal_root=root,
        threshold_fraction=kw.get("threshold", 0.30),
        context_window_tokens=kw.get("window", 200_000),
    )


class TestDisabledDefaultFirst:
    """The conservative default IS the contract."""

    def test_empty_env_means_disabled(self):
        cfg = IdleCompactConfig.from_env({})
        assert cfg.enabled is False
        # And the predicate is False no matter what else is true.
        assert should_compact(
            cfg, HOT_USAGE, already_compacted=False,
        ) is False

    def test_default_dataclass_is_disabled(self):
        assert IdleCompactConfig().enabled is False


class TestConfigParsing:
    def test_enabled_without_root_disables_with_warning(self, caplog):
        cfg = IdleCompactConfig.from_env(
            {"TIGERHARNESS_IDLE_COMPACT": "1"})
        assert cfg.enabled is False
        assert "JOURNAL is unset" in caplog.text

    def test_enabled_with_bad_root_disables(self, tmp_path, caplog):
        cfg = IdleCompactConfig.from_env({
            "TIGERHARNESS_IDLE_COMPACT": "true",
            "TIGERHARNESS_IDLE_COMPACT_JOURNAL": str(tmp_path / "nope"),
        })
        assert cfg.enabled is False
        assert "no active/ dir" in caplog.text

    def test_bad_threshold_disables_never_crashes(self, tmp_path, caplog):
        root = _journal(tmp_path)
        cfg = IdleCompactConfig.from_env({
            "TIGERHARNESS_IDLE_COMPACT": "yes",
            "TIGERHARNESS_IDLE_COMPACT_JOURNAL": str(root),
            "TIGERHARNESS_IDLE_COMPACT_THRESHOLD": "1.5",
        })
        assert cfg.enabled is False
        assert "invalid" in caplog.text
        cfg = IdleCompactConfig.from_env({
            "TIGERHARNESS_IDLE_COMPACT": "yes",
            "TIGERHARNESS_IDLE_COMPACT_JOURNAL": str(root),
            "TIGERHARNESS_IDLE_COMPACT_WINDOW": "zero",
        })
        assert cfg.enabled is False

    def test_valid_env_enables(self, tmp_path):
        root = _journal(tmp_path)
        cfg = IdleCompactConfig.from_env({
            "TIGERHARNESS_IDLE_COMPACT": "on",
            "TIGERHARNESS_IDLE_COMPACT_JOURNAL": str(root),
            "TIGERHARNESS_IDLE_COMPACT_THRESHOLD": "0.5",
            "TIGERHARNESS_IDLE_COMPACT_WINDOW": "100000",
        })
        assert cfg.enabled is True
        assert cfg.journal_root == root
        assert cfg.threshold_fraction == 0.5
        assert cfg.context_window_tokens == 100_000


class TestContextFraction:
    def test_real_payload(self):
        assert context_fraction(HOT_USAGE, 200_000) == pytest.approx(
            80_010 / 200_000)

    def test_missing_usage_reads_zero(self):
        assert context_fraction(None, 200_000) == 0.0
        assert context_fraction("garbage", 200_000) == 0.0  # type: ignore

    def test_malformed_values_skipped(self):
        usage = {"input_tokens": "many",
                 "cache_read_input_tokens": -5,
                 "cache_creation_input_tokens": 1000}
        assert context_fraction(usage, 200_000) == 1000 / 200_000

    def test_zero_window_reads_zero(self):
        assert context_fraction(HOT_USAGE, 0) == 0.0


class TestJournalIsIdle:
    def test_empty_journal_is_idle(self, tmp_path):
        assert journal_is_idle(_journal(tmp_path)) is True

    def test_pending_task_means_busy(self, tmp_path):
        assert journal_is_idle(
            _journal(tmp_path, with_pending=True)) is False

    def test_sweep_failure_means_busy(self, tmp_path, monkeypatch):
        # The sweep tolerates odd roots, so force the failure path
        # directly: when in doubt, don't compact.
        import importlib
        import sys

        importlib.import_module("tigerharness.journal.sweep")
        # The journal package re-exports `sweep` (the function), so
        # attribute access shadows the submodule -- fetch it from
        # sys.modules instead.
        sweep_mod = sys.modules["tigerharness.journal.sweep"]

        def boom(*a, **k):
            raise RuntimeError("sweep exploded")

        monkeypatch.setattr(sweep_mod, "sweep", boom)
        assert journal_is_idle(_journal(tmp_path)) is False


class TestShouldCompact:
    def test_fires_when_hot_and_idle(self, tmp_path):
        cfg = _cfg(_journal(tmp_path))
        assert should_compact(
            cfg, HOT_USAGE, already_compacted=False) is True

    def test_latch_blocks_second_fire(self, tmp_path):
        cfg = _cfg(_journal(tmp_path))
        assert should_compact(
            cfg, HOT_USAGE, already_compacted=True) is False

    def test_cold_usage_never_fires(self, tmp_path):
        cfg = _cfg(_journal(tmp_path))
        assert should_compact(
            cfg, {"input_tokens": 10}, already_compacted=False) is False

    def test_busy_queue_never_fires(self, tmp_path):
        cfg = _cfg(_journal(tmp_path, with_pending=True))
        assert should_compact(
            cfg, HOT_USAGE, already_compacted=False) is False


class TestMaybeCompact:
    async def test_fires_once_per_idle_period(self, tmp_path):
        cfg = _cfg(_journal(tmp_path))
        sent: list[str] = []

        async def send(prompt: str) -> None:
            sent.append(prompt)

        first = await maybe_compact(
            send, cfg, HOT_USAGE, already_compacted=False)
        assert first is True and sent == ["/compact"]
        # The latch (carried by the caller) blocks the second.
        second = await maybe_compact(
            send, cfg, HOT_USAGE, already_compacted=first)
        assert second is False and sent == ["/compact"]

    async def test_send_failure_is_fail_soft(self, tmp_path, caplog):
        cfg = _cfg(_journal(tmp_path))

        async def broken(prompt: str) -> None:
            raise RuntimeError("future CLI dropped /compact")

        result = await maybe_compact(
            broken, cfg, HOT_USAGE, already_compacted=False)
        assert result is False  # never raises, never latches
        assert "fail-soft" in caplog.text

    async def test_disabled_config_sends_nothing(self):
        sent: list[str] = []

        async def send(prompt: str) -> None:
            sent.append(prompt)

        result = await maybe_compact(
            send, IdleCompactConfig(), HOT_USAGE,
            already_compacted=False)
        assert result is False and sent == []
