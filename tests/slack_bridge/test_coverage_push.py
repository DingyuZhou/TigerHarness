"""Coverage-push tests for various slack_bridge modules.

Covers:
- downloader.py:146 (_human_size large TB value), 161->163 (no ext fallback)
- persistence.py:101, 102->97, 105 (pre-routing schema, invalid session_id)
- multi.py:190, 197, 204, 209, 232 (error paths in _get_personas)
- migrate.py:136-141 (atomic write error cleanup)
- notify.py:133->132 (no allowed_user_id found)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestHumanSize:
    """downloader.py: _human_size edge cases."""

    def test_bytes_range(self):
        from tigerharness.slack_bridge.downloader import _human_size
        assert _human_size(512) == "512 B"

    def test_kb_range(self):
        from tigerharness.slack_bridge.downloader import _human_size
        assert "KB" in _human_size(2048)

    def test_mb_range(self):
        from tigerharness.slack_bridge.downloader import _human_size
        assert "MB" in _human_size(2 * 1024 * 1024)

    def test_gb_range(self):
        from tigerharness.slack_bridge.downloader import _human_size
        assert "GB" in _human_size(5 * 1024**3)

    def test_tb_range(self):
        """Cover line 146: hits TB unit and returns."""
        from tigerharness.slack_bridge.downloader import _human_size
        result = _human_size(2 * 1024**4)
        assert "TB" in result

    def test_very_large_tb(self):
        """Cover the f >= 1024 TB case — still returns TB."""
        from tigerharness.slack_bridge.downloader import _human_size
        result = _human_size(2000 * 1024**4)
        assert "TB" in result


class TestPickExt:
    """downloader.py: _pick_ext edge cases."""

    def test_no_filetype_no_name(self):
        """Line 161->163: no filetype, no dot in name → empty string."""
        from tigerharness.slack_bridge.downloader import _pick_ext
        result = _pick_ext({"filetype": "", "name": "noext"})
        assert result == ""

    def test_no_filetype_name_with_dot(self):
        from tigerharness.slack_bridge.downloader import _pick_ext
        result = _pick_ext({"filetype": "", "name": "file.txt"})
        assert result == ".txt"

    def test_filetype_present(self):
        from tigerharness.slack_bridge.downloader import _pick_ext
        result = _pick_ext({"filetype": "pdf"})
        assert result == ".pdf"

    def test_no_filetype_no_name_key(self):
        """No name key at all → empty."""
        from tigerharness.slack_bridge.downloader import _pick_ext
        result = _pick_ext({})
        assert result == ""

    def test_no_filetype_name_with_trailing_dot(self):
        """161->163: name has dot but ext after split is empty."""
        from tigerharness.slack_bridge.downloader import _pick_ext
        result = _pick_ext({"filetype": "", "name": "file."})
        assert result == ""


class TestPersistencePreRoutingSchema:
    """persistence.py: loading pre-routing schema and invalid entries."""

    def test_load_pre_routing_bare_string(self, tmp_path):
        """Line 101: pre-routing schema — bare session_id string."""
        from tigerharness.slack_bridge.persistence import ThreadStore
        p = tmp_path / "threads.json"
        p.write_text(json.dumps({"1.0": "sess-old-format"}))
        store = ThreadStore(p)
        rec = store.get_record("1.0")
        assert rec is not None
        assert rec.session_id == "sess-old-format"
        assert rec.persona is None

    def test_load_invalid_session_id_skipped(self, tmp_path):
        """Line 105: dict entry with empty/non-string session_id → skipped."""
        from tigerharness.slack_bridge.persistence import ThreadStore
        p = tmp_path / "threads.json"
        p.write_text(json.dumps({
            "1.0": {"session_id": "", "persona": "foo"},
            "2.0": {"session_id": 123, "persona": "bar"},
            "3.0": {"persona": "baz"},  # no session_id at all
        }))
        store = ThreadStore(p)
        assert store.get_record("1.0") is None
        assert store.get_record("2.0") is None
        assert store.get_record("3.0") is None

    def test_load_empty_string_value_skipped(self, tmp_path):
        """Line 102->97: empty string value — not valid pre-routing schema."""
        from tigerharness.slack_bridge.persistence import ThreadStore
        p = tmp_path / "threads.json"
        p.write_text(json.dumps({"1.0": ""}))
        store = ThreadStore(p)
        assert store.get_record("1.0") is None


class TestMultiGetPersonasErrors:
    """multi.py: error paths in _get_personas."""

    def test_no_personas_yaml(self, tmp_path):
        """Line 190: personas.yaml not found."""
        from tigerharness.slack_bridge.multi import _read_team_roster
        with pytest.raises(ValueError, match="personas.yaml not found"):
            _read_team_roster(tmp_path, "test-lane")

    def test_personas_yaml_empty_list(self, tmp_path):
        """Line 197: personas list empty."""
        from tigerharness.slack_bridge.multi import _read_team_roster
        cfg = tmp_path / "configs" / "personas.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("personas: []\n")
        with pytest.raises(ValueError, match="no 'personas' list"):
            _read_team_roster(tmp_path, "test-lane")

    def test_personas_yaml_not_list(self, tmp_path):
        """Line 197: personas is not a list."""
        from tigerharness.slack_bridge.multi import _read_team_roster
        cfg = tmp_path / "configs" / "personas.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("personas: not-a-list\n")
        with pytest.raises(ValueError, match="no 'personas' list"):
            _read_team_roster(tmp_path, "test-lane")

    def test_personas_entry_not_dict(self, tmp_path):
        """Line 204: entry is not a mapping."""
        from tigerharness.slack_bridge.multi import _read_team_roster
        cfg = tmp_path / "configs" / "personas.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("personas:\n  - just-a-string\n")
        with pytest.raises(ValueError, match="entries must be mappings"):
            _read_team_roster(tmp_path, "test-lane")

    def test_personas_entry_missing_name(self, tmp_path):
        """Line 209: entry missing 'name' field."""
        from tigerharness.slack_bridge.multi import _read_team_roster
        cfg = tmp_path / "configs" / "personas.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("personas:\n  - role: coach\n")
        with pytest.raises(ValueError, match="missing/empty 'name'"):
            _read_team_roster(tmp_path, "test-lane")

    def test_personas_entry_empty_name(self, tmp_path):
        """Line 209: entry with empty name string."""
        from tigerharness.slack_bridge.multi import _read_team_roster
        cfg = tmp_path / "configs" / "personas.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("personas:\n  - name: ''\n")
        with pytest.raises(ValueError, match="missing/empty 'name'"):
            _read_team_roster(tmp_path, "test-lane")

    def test_prompt_not_found(self, tmp_path):
        """Line 232: persona prompt.md not found."""
        from tigerharness.slack_bridge.multi import _build_persona_slot
        (tmp_path / "personas" / "alpha").mkdir(parents=True)
        with pytest.raises(ValueError, match="prompt not found"):
            _build_persona_slot(tmp_path, "alpha", "testlane", ["alpha"], "testlane")


class TestMigrateAtomicWriteFailure:
    """migrate.py:136-141: atomic write fails → temp file cleaned up."""

    def test_atomic_write_error_cleans_temp(self, tmp_path):
        from tigerharness.slack_bridge.migrate import _atomic_write_json
        path = tmp_path / "threads.json"

        # Make os.replace fail → triggers cleanup
        with patch("tigerharness.slack_bridge.migrate.os.replace",
                   side_effect=OSError("permission denied")):
            with pytest.raises(OSError):
                _atomic_write_json(path, {"key": "value"})

        # Temp file should be cleaned up (unlink succeeded)
        temps = list(tmp_path.glob(".threads.*.tmp"))
        assert len(temps) == 0

    def test_atomic_write_error_unlink_also_fails(self, tmp_path):
        """Lines 139-140: os.replace AND os.unlink both fail."""
        from tigerharness.slack_bridge.migrate import _atomic_write_json
        path = tmp_path / "threads.json"

        # Make both os.replace and os.unlink fail
        with patch("tigerharness.slack_bridge.migrate.os.replace",
                   side_effect=OSError("replace failed")):
            with patch("tigerharness.slack_bridge.migrate.os.unlink",
                       side_effect=OSError("unlink failed")):
                with pytest.raises(OSError, match="replace failed"):
                    _atomic_write_json(path, {"key": "value"})


class TestNotifyGetFirstUserId:
    """notify.py:133->132: _first_allowed_user_from_yaml returns None."""

    def test_returns_none_for_empty_ids(self, tmp_path):
        from tigerharness.slack_bridge.notify import _first_allowed_user_from_yaml
        cfg = tmp_path / "bridge.yaml"
        cfg.write_text("allowed_user_ids: []\n")
        result = _first_allowed_user_from_yaml(cfg)
        assert result is None

    def test_returns_none_for_non_string_ids(self, tmp_path):
        from tigerharness.slack_bridge.notify import _first_allowed_user_from_yaml
        cfg = tmp_path / "bridge.yaml"
        cfg.write_text("allowed_user_ids:\n  - 123\n  - \n")
        result = _first_allowed_user_from_yaml(cfg)
        assert result is None

    def test_returns_first_valid_id(self, tmp_path):
        from tigerharness.slack_bridge.notify import _first_allowed_user_from_yaml
        cfg = tmp_path / "bridge.yaml"
        cfg.write_text("allowed_user_ids:\n  - U12345\n")
        result = _first_allowed_user_from_yaml(cfg)
        assert result == "U12345"

    def test_returns_none_for_missing_file(self, tmp_path):
        from tigerharness.slack_bridge.notify import _first_allowed_user_from_yaml
        result = _first_allowed_user_from_yaml(tmp_path / "nonexistent.yaml")
        assert result is None
