"""Final coverage push — targets remaining reachable lines.

Targets:
- store.py:267-269 (lock stat FileNotFoundError → retry)
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


# ---------- store.py:267-269 — lock stat FileNotFoundError → retry ----------


class TestLockStatRace:
    def test_lock_stat_fnfe_triggers_retry(self, tmp_path):
        """Cover store.py:267-269 — stat() races with release → retry."""
        from tigerharness.tiger_memory.store import Store

        store = Store(tmp_path)
        lock_path = tmp_path / "test.lock"

        call_count = 0

        original_stat = Path.stat

        def fake_stat(self, *args, **kwargs):
            nonlocal call_count
            if self == lock_path and call_count == 0:
                call_count += 1
                raise FileNotFoundError("raced with release")
            return original_stat(self, *args, **kwargs)

        # First call: lock file exists → stat raises FNFE → retry
        # Second call: lock file doesn't exist → acquire succeeds
        lock_path.write_text("99999999")  # fake PID

        with patch.object(Path, "stat", fake_stat):
            # The retry should work because after FNFE, it tries to
            # acquire again and the lock file is still there but
            # now stat works
            lock_path.write_text("99999999")
            store._try_acquire_lock(lock_path, timeout_minutes=1)
            # Should have retried
            assert call_count == 1
