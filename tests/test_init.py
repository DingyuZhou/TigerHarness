"""Tests for tigerharness.init team-based scaffolding."""
from __future__ import annotations

import builtins
import runpy
from pathlib import Path
from unittest.mock import patch

import pytest

from tigerharness.init import (
    _append_lane_to_slack_bridge_index,
    _append_persona_to_yaml,
    _command_prefix,
    _format_path,
    _maybe_register_slack_bridge_lane,
    _prompt_choice,
    _prompt_text,
    _prompt_yes_no,
    _render_memory_config,
    _validate_name,
    _write_if_missing,
    add_persona,
    create_team,
    detect_claude_project_path,
    discover_teams,
    init,
    list_personas_in_team,
    main,
)


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

class TestValidateName:
    @pytest.mark.parametrize("name", [
        "chief", "scout-7", "Q_master", "a", "X1", "team-99",
    ])
    def test_accepts_valid(self, name: str):
        assert _validate_name(name, kind="persona") == name

    def test_strips_whitespace(self):
        assert _validate_name("  chief  ", kind="persona") == "chief"

    @pytest.mark.parametrize("name", [
        "", "   ", "chief/scout", "..", ".hidden", "-leading-dash",
        "with space", "with/slash", "with\\back", "weird:char",
        "../escape", "name.with.dot",
    ])
    def test_rejects_invalid(self, name: str):
        with pytest.raises(ValueError, match="(invalid|cannot be empty)"):
            _validate_name(name, kind="persona")

    def test_kind_appears_in_error(self):
        with pytest.raises(ValueError, match="team"):
            _validate_name("bad/name", kind="team")


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

class TestWriteIfMissing:
    def test_creates_file_and_parent(self, tmp_path: Path):
        f = tmp_path / "sub" / "deep" / "test.txt"
        assert _write_if_missing(f, "hello") is True
        assert f.read_text() == "hello"

    def test_skips_existing(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_text("original")
        assert _write_if_missing(f, "replacement") is False
        assert f.read_text() == "original"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscoverTeams:
    def test_empty_dir(self, tmp_path: Path):
        assert discover_teams(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path: Path):
        assert discover_teams(tmp_path / "missing") == []

    def test_search_root_itself_is_team(self, tmp_path: Path):
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "personas.yaml").write_text("personas: []\n")
        assert discover_teams(tmp_path) == [tmp_path]

    def test_finds_child_teams_sorted(self, tmp_path: Path):
        for name in ("zebras", "alphas", "mids"):
            d = tmp_path / name / "configs"
            d.mkdir(parents=True)
            (d / "personas.yaml").write_text("personas: []\n")
        # Add a non-team subdir as noise
        (tmp_path / "junk").mkdir()
        # And a file at the top level
        (tmp_path / "stray.txt").write_text("x")
        teams = discover_teams(tmp_path)
        assert [t.name for t in teams] == ["alphas", "mids", "zebras"]

    def test_skips_dirs_without_personas_yaml(self, tmp_path: Path):
        (tmp_path / "almost" / "configs").mkdir(parents=True)
        # configs/ exists but no personas.yaml
        assert discover_teams(tmp_path) == []


class TestListPersonasInTeam:
    def test_empty_team(self, tmp_path: Path):
        (tmp_path / "personas").mkdir()
        assert list_personas_in_team(tmp_path) == []

    def test_no_personas_dir(self, tmp_path: Path):
        assert list_personas_in_team(tmp_path) == []

    def test_lists_only_dirs_with_prompt(self, tmp_path: Path):
        personas = tmp_path / "personas"
        personas.mkdir()
        # Valid
        (personas / "chief").mkdir()
        (personas / "chief" / "prompt.md").write_text("you are chief")
        (personas / "scout").mkdir()
        (personas / "scout" / "prompt.md").write_text("you are scout")
        # Invalid: dir without prompt.md
        (personas / "halfbaked").mkdir()
        # Invalid: file, not dir
        (personas / "stray.md").write_text("x")
        assert list_personas_in_team(tmp_path) == ["chief", "scout"]


# ---------------------------------------------------------------------------
# Claude project path detection
# ---------------------------------------------------------------------------

class TestDetectClaudeProjectPath:
    def test_returns_path_when_exists(self, tmp_path: Path):
        # Fake $HOME with a transcripts dir for tmp_path/myproject
        fake_home = tmp_path / "home"
        proj = tmp_path / "myproject"
        proj.mkdir()
        encoded = str(proj.resolve()).replace("/", "-")
        transcripts = fake_home / ".claude" / "projects" / encoded
        transcripts.mkdir(parents=True)
        result = detect_claude_project_path(proj, home=fake_home)
        assert result == transcripts

    def test_returns_none_when_missing(self, tmp_path: Path):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "myproject"
        proj.mkdir()
        assert detect_claude_project_path(proj, home=fake_home) is None

    def test_returns_none_when_home_missing(self, tmp_path: Path):
        proj = tmp_path / "myproject"
        proj.mkdir()
        assert detect_claude_project_path(
            proj, home=tmp_path / "nonexistent-home"
        ) is None


class TestRenderMemoryConfig:
    def test_uses_placeholder_when_undetected(self, tmp_path: Path):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "tigers"
        proj.mkdir()
        cfg = _render_memory_config(
            persona="chief", team="tigers",
            project_root=proj, home=fake_home,
        )
        assert "~/.claude/projects/-home-user-myproject/" in cfg
        assert "Replace with your Claude Code project path" in cfg
        assert "name: chief" in cfg

    def test_fills_in_detected_path(self, tmp_path: Path):
        fake_home = tmp_path / "home"
        proj = tmp_path / "tigers"
        proj.mkdir()
        encoded = str(proj.resolve()).replace("/", "-")
        transcripts = fake_home / ".claude" / "projects" / encoded
        transcripts.mkdir(parents=True)

        cfg = _render_memory_config(
            persona="chief", team="tigers",
            project_root=proj, home=fake_home,
        )
        assert str(transcripts) in cfg
        assert "Auto-detected from the team root" in cfg
        # placeholder text should NOT appear
        assert "-home-user-myproject" not in cfg


# ---------------------------------------------------------------------------
# create_team
# ---------------------------------------------------------------------------

class TestCreateTeam:
    def test_creates_full_scaffold(self, tmp_path: Path):
        team = tmp_path / "tigers"
        created = create_team(team, include_slack=True)
        assert (team / ".gitignore").exists()
        assert (team / "configs" / "personas.yaml").exists()
        assert (team / "configs" / ".env").exists()
        assert (team / "skills" / "README.md").exists()
        # gitignore excludes secrets
        assert "configs/.env" in (team / ".gitignore").read_text()
        # personas.yaml header references team name
        assert "Team: tigers" in (team / "configs" / "personas.yaml").read_text()
        # All four paths returned
        assert len(created) == 4

    def test_skip_slack(self, tmp_path: Path):
        team = tmp_path / "tigers"
        create_team(team, include_slack=False)
        assert not (team / "configs" / ".env").exists()
        assert (team / "configs" / "personas.yaml").exists()

    def test_idempotent(self, tmp_path: Path):
        team = tmp_path / "tigers"
        create_team(team, include_slack=True)
        # Second run creates nothing
        created = create_team(team, include_slack=True)
        assert created == []


# ---------------------------------------------------------------------------
# add_persona / _append_persona_to_yaml
# ---------------------------------------------------------------------------

class TestAppendPersonaToYaml:
    def test_creates_file_when_missing(self, tmp_path: Path):
        yp = tmp_path / "configs" / "personas.yaml"
        assert _append_persona_to_yaml(yp, "chief", "desc", "tigers") is True
        text = yp.read_text()
        assert "Team: tigers" in text
        assert "- name: chief" in text
        assert 'description: "desc"' in text

    def test_appends_to_existing(self, tmp_path: Path):
        yp = tmp_path / "personas.yaml"
        yp.write_text("personas:\n  - name: scout\n")
        assert _append_persona_to_yaml(yp, "chief", "desc", "tigers") is True
        text = yp.read_text()
        assert "- name: scout" in text
        assert "- name: chief" in text

    def test_idempotent_when_present(self, tmp_path: Path):
        yp = tmp_path / "personas.yaml"
        yp.write_text("personas:\n  - name: chief\n")
        assert _append_persona_to_yaml(yp, "chief", "desc", "tigers") is False

    def test_handles_missing_trailing_newline(self, tmp_path: Path):
        yp = tmp_path / "personas.yaml"
        yp.write_text("personas:\n  - name: scout")  # no trailing \n
        _append_persona_to_yaml(yp, "chief", "desc", "tigers")
        text = yp.read_text()
        # No collapsed lines
        assert "scout\n  - name: chief" in text

    def test_idempotency_check_is_indent_anchored(self, tmp_path: Path):
        """Idempotency must not false-positive on substrings inside
        descriptions or comments. The check anchors on the 2-space
        indent that `_PERSONA_ENTRY` emits."""
        yp = tmp_path / "personas.yaml"
        # Description happens to contain the bare substring "- name: chief".
        yp.write_text(
            "personas:\n"
            "  - name: scout\n"
            "    description: \"reports to - name: chief upstream\"\n"
        )
        # Chief is NOT actually registered yet -- the substring is just noise.
        assert _append_persona_to_yaml(yp, "chief", "the boss", "tigers") is True
        text = yp.read_text()
        # Chief now appears as a real entry
        assert text.count("  - name: chief\n") == 1


class TestAddPersona:
    def test_creates_prompt_memory_and_registers(self, tmp_path: Path):
        team = tmp_path / "tigers"
        create_team(team, include_slack=False)
        created = add_persona(team, "chief", include_memory=True)
        assert (team / "personas" / "chief" / "prompt.md").exists()
        assert (team / "memories" / "chief" / "tiger-memory.config.yaml").exists()
        # registry updated
        text = (team / "configs" / "personas.yaml").read_text()
        assert "- name: chief" in text
        # all 3 paths returned (prompt, memory, yaml updated)
        assert len(created) == 3

    def test_skip_memory(self, tmp_path: Path):
        team = tmp_path / "tigers"
        create_team(team, include_slack=False)
        add_persona(team, "scout", include_memory=False)
        assert not (team / "memories" / "scout" / "tiger-memory.config.yaml").exists()
        assert (team / "personas" / "scout" / "prompt.md").exists()

    def test_raises_when_persona_exists(self, tmp_path: Path):
        team = tmp_path / "tigers"
        create_team(team, include_slack=False)
        add_persona(team, "chief", include_memory=False)
        with pytest.raises(ValueError, match="already exists"):
            add_persona(team, "chief", include_memory=False)

    def test_custom_description(self, tmp_path: Path):
        team = tmp_path / "tigers"
        create_team(team, include_slack=False)
        add_persona(
            team, "chief", include_memory=False,
            description="the boss",
        )
        assert 'description: "the boss"' in (team / "configs" / "personas.yaml").read_text()

    def test_default_description(self, tmp_path: Path):
        team = tmp_path / "tigers"
        create_team(team, include_slack=False)
        add_persona(team, "scout", include_memory=False)
        text = (team / "configs" / "personas.yaml").read_text()
        assert 'description: "scout on team tigers"' in text

    def test_works_without_pre_created_team(self, tmp_path: Path):
        """add_persona creates the personas.yaml even when team scaffold is absent."""
        team = tmp_path / "tigers"
        # No create_team call -- add_persona handles fresh dir.
        created = add_persona(team, "chief", include_memory=False)
        assert (team / "personas" / "chief" / "prompt.md").exists()
        assert (team / "configs" / "personas.yaml").exists()
        # prompt + yaml = 2 paths
        assert len(created) == 2

    def test_partial_recovery_memory_and_yaml_present(self, tmp_path: Path):
        """If memory cfg and yaml entry exist but prompt does not, add_persona
        creates only the prompt (idempotent on memory + yaml)."""
        team = tmp_path / "tigers"
        create_team(team, include_slack=False)
        # Pre-create the memory config
        mem = team / "memories" / "chief" / "tiger-memory.config.yaml"
        mem.parent.mkdir(parents=True)
        mem.write_text("# preexisting\n")
        # Pre-add the yaml entry
        yp = team / "configs" / "personas.yaml"
        yp.write_text(yp.read_text() + "  - name: chief\n    cwd: .\n")

        created = add_persona(team, "chief", include_memory=True)
        # Only the prompt was newly created
        assert (team / "personas" / "chief" / "prompt.md") in created
        assert mem not in created
        assert yp not in created
        # Memory file untouched (still contains the preexisting marker)
        assert "# preexisting" in mem.read_text()


# ---------------------------------------------------------------------------
# Multi-lane slack-bridge auto-registration
# ---------------------------------------------------------------------------

class TestAppendLaneToSlackBridgeIndex:
    def test_writes_header_when_file_lacks_lanes_key(self, tmp_path: Path):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("# just a comment\n")
        appended = _append_lane_to_slack_bridge_index(idx, "shohoku")
        assert appended is True
        content = idx.read_text()
        assert "lanes:\n" in content
        assert "  - shohoku\n" in content

    def test_appends_under_existing_header(self, tmp_path: Path):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("lanes:\n  - tigers\n")
        appended = _append_lane_to_slack_bridge_index(idx, "shohoku")
        assert appended is True
        content = idx.read_text()
        assert content.count("  - tigers\n") == 1
        assert content.count("  - shohoku\n") == 1
        # New entry comes after existing ones.
        assert content.index("tigers") < content.index("shohoku")

    def test_no_op_when_entry_already_present(self, tmp_path: Path):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("lanes:\n  - shohoku\n")
        before = idx.read_text()
        appended = _append_lane_to_slack_bridge_index(idx, "shohoku")
        assert appended is False
        assert idx.read_text() == before

    def test_handles_file_without_trailing_newline(self, tmp_path: Path):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("lanes:\n  - tigers")  # no trailing newline
        _append_lane_to_slack_bridge_index(idx, "shohoku")
        content = idx.read_text()
        # Existing entry preserved + new entry on its own line.
        assert "  - tigers\n" in content
        assert "  - shohoku\n" in content

    def test_handles_empty_file(self, tmp_path: Path):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("")  # empty
        appended = _append_lane_to_slack_bridge_index(idx, "shohoku")
        assert appended is True
        assert idx.read_text() == "lanes:\n  - shohoku\n"

    def test_handles_unterminated_header_only_file(self, tmp_path: Path):
        """File has a leading comment but no trailing newline AND no
        `lanes:` header. Helper must inject the newline before writing
        the new header + entry."""
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("# top comment, no newline")  # deliberately ragged
        _append_lane_to_slack_bridge_index(idx, "shohoku")
        content = idx.read_text()
        # Original comment preserved, header injected, entry appended.
        assert content.startswith("# top comment, no newline\n")
        assert "lanes:\n  - shohoku\n" in content


class TestMaybeRegisterSlackBridgeLane:
    def test_returns_empty_when_index_missing(self, tmp_path: Path):
        # No top-level slack-bridge.yaml -> single-tenant mode, helper
        # is a no-op.
        team_dir = tmp_path / "shohoku"
        team_dir.mkdir()
        result = _maybe_register_slack_bridge_lane(
            tmp_path, team_dir, "shohoku", "ayako"
        )
        assert result == []
        # Helper must not have created the fragment either.
        assert not (team_dir / "configs" / "slack-bridge.yaml").exists()

    def test_writes_fragment_and_appends_to_index(self, tmp_path: Path):
        team_dir = tmp_path / "shohoku"
        team_dir.mkdir()
        (team_dir / "configs").mkdir()
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("")  # empty index -> opts the user into multi mode
        result = _maybe_register_slack_bridge_lane(
            tmp_path, team_dir, "shohoku", "ayako"
        )
        fragment_path = team_dir / "configs" / "slack-bridge.yaml"
        assert fragment_path in result
        assert idx in result
        assert fragment_path.exists()
        body = fragment_path.read_text()
        assert "persona: ayako" in body
        assert "shohoku" in body
        assert "allowed_user_ids: []" in body
        assert "  - shohoku\n" in idx.read_text()

    def test_idempotent_when_called_twice(self, tmp_path: Path):
        team_dir = tmp_path / "shohoku"
        team_dir.mkdir()
        (team_dir / "configs").mkdir()
        (tmp_path / "slack-bridge.yaml").write_text("")
        # First call creates both.
        first = _maybe_register_slack_bridge_lane(
            tmp_path, team_dir, "shohoku", "ayako"
        )
        assert len(first) == 2
        # Second call -- fragment exists, index entry exists -- no-op.
        second = _maybe_register_slack_bridge_lane(
            tmp_path, team_dir, "shohoku", "ayako"
        )
        assert second == []


class TestInitWithMultiBridgeIndex:
    """End-to-end: when slack-bridge.yaml exists at the search root, init
    auto-generates the fragment and appends to the index."""

    def test_main_writes_fragment_and_extends_next_steps(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        # User opts in by creating the (empty) index.
        (tmp_path / "slack-bridge.yaml").write_text("")
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "ayako",
            "--team", "shohoku",
            "--yes",
        ])
        assert rc == 0
        fragment = tmp_path / "shohoku" / "configs" / "slack-bridge.yaml"
        assert fragment.exists()
        idx_text = (tmp_path / "slack-bridge.yaml").read_text()
        assert "  - shohoku\n" in idx_text
        out = capsys.readouterr().out
        # The new "Edit slack-bridge.yaml" reminder must appear in Next steps.
        assert "shohoku/configs/slack-bridge.yaml" in out
        assert "allowed_user_ids" in out

    def test_main_skips_fragment_when_index_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        # No top-level index -> single-tenant mode.
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "ayako",
            "--team", "shohoku",
            "--yes",
        ])
        assert rc == 0
        fragment = tmp_path / "shohoku" / "configs" / "slack-bridge.yaml"
        assert not fragment.exists()
        out = capsys.readouterr().out
        # No multi-bridge mention in Next steps when not opted in.
        assert "slack-bridge.yaml" not in out


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

class TestPromptText:
    def test_returns_input(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(builtins, "input", lambda _: "scout")
        assert _prompt_text("name") == "scout"

    def test_returns_default_on_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(builtins, "input", lambda _: "")
        assert _prompt_text("name", default="chief") == "chief"

    def test_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(builtins, "input", lambda _: "  scout  ")
        assert _prompt_text("name") == "scout"

    def test_reprompts_when_blank_and_no_default(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        answers = iter(["", "  ", "chief"])
        monkeypatch.setattr(builtins, "input", lambda _: next(answers))
        assert _prompt_text("name") == "chief"


class TestPromptYesNo:
    @pytest.mark.parametrize("ans,expected", [
        ("y", True), ("Y", True), ("yes", True), ("YES", True),
        ("n", False), ("N", False), ("no", False), ("NO", False),
    ])
    def test_explicit_answers(
        self, monkeypatch: pytest.MonkeyPatch, ans: str, expected: bool
    ):
        monkeypatch.setattr(builtins, "input", lambda _: ans)
        assert _prompt_yes_no("?", default=True) is expected

    def test_default_true_on_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(builtins, "input", lambda _: "")
        assert _prompt_yes_no("?", default=True) is True

    def test_default_false_on_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(builtins, "input", lambda _: "")
        assert _prompt_yes_no("?", default=False) is False

    def test_reprompts_on_garbage(self, monkeypatch: pytest.MonkeyPatch):
        answers = iter(["maybe", "huh", "y"])
        monkeypatch.setattr(builtins, "input", lambda _: next(answers))
        assert _prompt_yes_no("?", default=False) is True


class TestPromptChoice:
    def test_picks_choice(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        monkeypatch.setattr(builtins, "input", lambda _: "2")
        assert _prompt_choice("Pick", ["a", "b", "c"], default_idx=0) == 1

    def test_default_on_empty(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        monkeypatch.setattr(builtins, "input", lambda _: "")
        assert _prompt_choice("Pick", ["a", "b"], default_idx=1) == 1

    def test_reprompts_on_invalid(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        answers = iter(["abc", "99", "0", "1"])
        monkeypatch.setattr(builtins, "input", lambda _: next(answers))
        assert _prompt_choice("Pick", ["a", "b"], default_idx=0) == 0


# ---------------------------------------------------------------------------
# init() orchestration -- non-interactive paths
# ---------------------------------------------------------------------------

class TestInitNonInteractive:
    def test_new_team_and_persona(self, tmp_path: Path):
        team_dir, persona, created = init(
            persona="chief",
            team="tigers",
            include_memory=True,
            include_slack=True,
            search_root=tmp_path,
        )
        assert persona == "chief"
        assert team_dir == (tmp_path / "tigers").resolve()
        assert (team_dir / "personas" / "chief" / "prompt.md").exists()
        assert (team_dir / "configs" / "personas.yaml").exists()
        assert (team_dir / "configs" / ".env").exists()
        assert (team_dir / "memories" / "chief" / "tiger-memory.config.yaml").exists()
        assert created

    def test_no_memory_no_slack(self, tmp_path: Path):
        team_dir, _, _ = init(
            persona="scout",
            team="tigers",
            include_memory=False,
            include_slack=False,
            search_root=tmp_path,
        )
        assert not (team_dir / "configs" / ".env").exists()
        assert not (team_dir / "memories" / "scout" / "tiger-memory.config.yaml").exists()
        assert (team_dir / "personas" / "scout" / "prompt.md").exists()

    def test_adds_to_existing_team(self, tmp_path: Path):
        init(
            persona="chief",
            team="tigers",
            include_memory=False,
            include_slack=False,
            search_root=tmp_path,
        )
        # Second persona joins same team
        team_dir, _, _ = init(
            persona="scout",
            team="tigers",
            include_memory=False,
            include_slack=False,
            search_root=tmp_path,
        )
        text = (team_dir / "configs" / "personas.yaml").read_text()
        assert "- name: chief" in text
        assert "- name: scout" in text

    def test_skill_pointer_in_personas_yaml(self, tmp_path: Path):
        """Generated personas.yaml mentions skills/ as a commented option."""
        team_dir, _, _ = init(
            persona="chief", team="tigers",
            include_memory=False, include_slack=False,
            search_root=tmp_path,
        )
        text = (team_dir / "configs" / "personas.yaml").read_text()
        # Header references the skills directory
        assert "../skills" in text
        # Per-entry comment is also there as a commented-out example
        assert "# extra:" in text
        # Per-entry comment is NOT cluttered with the cwd explanation
        assert "team root (one level up" not in text

    def test_auto_detects_claude_project_path(self, tmp_path: Path):
        """When ~/.claude/projects/<encoded>/ exists, fill it in."""
        fake_home = tmp_path / "home"
        # Pre-create transcripts dir for the team root
        team_root = (tmp_path / "tigers").resolve()
        encoded = str(team_root).replace("/", "-")
        transcripts = fake_home / ".claude" / "projects" / encoded
        transcripts.mkdir(parents=True)

        team_dir, _, _ = init(
            persona="chief", team="tigers",
            include_memory=True, include_slack=False,
            search_root=tmp_path, home=fake_home,
        )
        mem = (team_dir / "memories" / "chief" / "tiger-memory.config.yaml").read_text()
        assert str(transcripts) in mem
        assert "Auto-detected" in mem

    def test_slack_prompt_skipped_when_env_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Re-running init on a team whose .env exists must not re-ask."""
        # Seed team with .env
        init(
            persona="chief", team="tigers",
            include_memory=False, include_slack=True,
            search_root=tmp_path,
        )
        # Now add scout interactively: only memory should be asked.
        # If slack were re-asked, this iter would run dry and raise StopIteration.
        answers = iter(["n"])  # memory: no
        monkeypatch.setattr(builtins, "input", lambda _: next(answers))
        team_dir, _, _ = init(
            persona="scout", team="tigers",
            search_root=tmp_path,
        )
        # .env still exists, scout was added, no errors
        assert (team_dir / "configs" / ".env").exists()
        assert (team_dir / "personas" / "scout" / "prompt.md").exists()

    def test_existing_team_slack_request_creates_env(self, tmp_path: Path):
        init(
            persona="chief",
            team="tigers",
            include_memory=False,
            include_slack=False,
            search_root=tmp_path,
        )
        # Add scout WITH slack now -- .env should appear
        team_dir, _, _ = init(
            persona="scout",
            team="tigers",
            include_memory=False,
            include_slack=True,
            search_root=tmp_path,
        )
        assert (team_dir / "configs" / ".env").exists()

    def test_raises_on_duplicate_persona(self, tmp_path: Path):
        init(
            persona="chief",
            team="tigers",
            include_memory=False,
            include_slack=False,
            search_root=tmp_path,
        )
        with pytest.raises(ValueError, match="already exists"):
            init(
                persona="chief",
                team="tigers",
                include_memory=False,
                include_slack=False,
                search_root=tmp_path,
            )

    def test_custom_team_dir(self, tmp_path: Path):
        custom = tmp_path / "elsewhere" / "mybox"
        team_dir, _, _ = init(
            persona="chief",
            team_dir=custom,
            include_memory=False,
            include_slack=False,
            search_root=tmp_path,
        )
        assert team_dir == custom.resolve()
        assert team_dir.name == "mybox"
        assert (team_dir / "personas" / "chief" / "prompt.md").exists()

    def test_team_dir_with_explicit_team_name(self, tmp_path: Path):
        custom = tmp_path / "elsewhere"
        team_dir, _, _ = init(
            persona="chief",
            team="tigers",
            team_dir=custom,
            include_memory=False,
            include_slack=False,
            search_root=tmp_path,
        )
        # team_dir override wins; team name is just used for templates
        assert team_dir == custom.resolve()


# ---------------------------------------------------------------------------
# init() orchestration -- interactive paths
# ---------------------------------------------------------------------------

class TestInitInteractive:
    def test_full_interactive_no_existing_teams(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        answers = iter([
            "chief",     # persona name
            "tigers",    # team name (new)
            "y",         # slack? yes
            "y",         # memory? yes
        ])
        monkeypatch.setattr(builtins, "input", lambda _: next(answers))
        team_dir, persona, created = init(search_root=tmp_path)
        assert persona == "chief"
        assert team_dir.name == "tigers"
        assert (team_dir / "personas" / "chief" / "prompt.md").exists()

    def test_interactive_picks_existing_team(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Seed an existing team
        init(
            persona="scout",
            team="tigers",
            include_memory=False,
            include_slack=False,
            search_root=tmp_path,
        )
        answers = iter([
            "chief",     # persona name
            "1",         # choose first existing team
            "n",         # slack? no
            "n",         # memory? no
        ])
        monkeypatch.setattr(builtins, "input", lambda _: next(answers))
        team_dir, persona, _ = init(search_root=tmp_path)
        assert persona == "chief"
        assert team_dir.name == "tigers"
        assert "- name: scout" in (team_dir / "configs" / "personas.yaml").read_text()
        assert "- name: chief" in (team_dir / "configs" / "personas.yaml").read_text()

    def test_interactive_create_new_team_with_existing_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Seed an existing team
        init(
            persona="scout",
            team="tigers",
            include_memory=False,
            include_slack=False,
            search_root=tmp_path,
        )
        answers = iter([
            "alpha",     # persona name
            "2",         # choose "Create new team"
            "lions",     # new team name
            "n",         # slack? no
            "n",         # memory? no
        ])
        monkeypatch.setattr(builtins, "input", lambda _: next(answers))
        team_dir, _, _ = init(search_root=tmp_path)
        assert team_dir.name == "lions"
        assert (team_dir / "personas" / "alpha" / "prompt.md").exists()
        # original team untouched
        assert (tmp_path / "tigers" / "personas" / "scout" / "prompt.md").exists()


# ---------------------------------------------------------------------------
# _format_path
# ---------------------------------------------------------------------------

class TestFormatPath:
    def test_relative_under_base(self, tmp_path: Path):
        p = tmp_path / "x" / "y.txt"
        assert _format_path(p, tmp_path) == "x/y.txt"

    def test_outside_base_returns_absolute(self, tmp_path: Path):
        p = Path("/etc/hosts")
        assert _format_path(p, tmp_path) == "/etc/hosts"


class TestCommandPrefix:
    """`_command_prefix()` returns "uv run " when `tigerharness` isn't on PATH.

    Driven entirely by `shutil.which("tigerharness")`. We monkeypatch
    that lookup so the tests are independent of the host environment.
    """

    def test_on_path_returns_empty(self, monkeypatch: pytest.MonkeyPatch):
        from tigerharness import init as init_mod
        monkeypatch.setattr(
            init_mod.shutil, "which",
            lambda name: "/usr/local/bin/tigerharness" if name == "tigerharness" else None,
        )
        assert _command_prefix() == ""

    def test_off_path_returns_uv_run(self, monkeypatch: pytest.MonkeyPatch):
        from tigerharness import init as init_mod
        monkeypatch.setattr(init_mod.shutil, "which", lambda name: None)
        assert _command_prefix() == "uv run "


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------

class TestMain:
    def test_non_interactive_yes(self, tmp_path: Path, capsys: pytest.CaptureFixture):
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team", "tigers",
            "--yes",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Created" in out
        assert (tmp_path / "tigers" / "personas" / "chief" / "prompt.md").exists()
        assert (tmp_path / "tigers" / "configs" / ".env").exists()
        assert (tmp_path / "tigers" / "memories" / "chief" / "tiger-memory.config.yaml").exists()

    def test_no_memory_no_slack_flags(self, tmp_path: Path):
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "scout",
            "--team", "tigers",
            "--no-memory",
            "--no-slack",
            "--yes",
        ])
        assert rc == 0
        assert not (tmp_path / "tigers" / "configs" / ".env").exists()
        assert not (tmp_path / "tigers" / "memories" / "scout").exists()

    def test_yes_with_no_persona_defaults_to_assistant(self, tmp_path: Path):
        rc = main(["--dir", str(tmp_path), "--yes"])
        assert rc == 0
        assert (tmp_path / "tigers" / "personas" / "assistant" / "prompt.md").exists()

    def test_yes_defaults_to_first_existing_team(self, tmp_path: Path):
        """With --yes and no --team, the first existing team is used
        deterministically -- no prompt, no EOFError."""
        # Seed
        main([
            "--dir", str(tmp_path),
            "--persona", "scout",
            "--team", "tigers",
            "--yes", "--no-memory", "--no-slack",
        ])
        # Now without --team
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--yes", "--no-memory", "--no-slack",
        ])
        assert rc == 0
        assert (tmp_path / "tigers" / "personas" / "chief" / "prompt.md").exists()
        # No second team was spuriously created
        assert sorted(p.name for p in tmp_path.iterdir()) == ["tigers"]

    def test_invalid_persona_name_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "bad/name",
            "--team", "tigers",
            "--yes",
        ])
        assert rc == 1
        assert "invalid persona name" in capsys.readouterr().err

    def test_invalid_team_name_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team", "../escape",
            "--yes",
        ])
        assert rc == 1
        assert "invalid team name" in capsys.readouterr().err

    def test_duplicate_persona_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        main([
            "--dir", str(tmp_path),
            "--persona", "chief", "--team", "tigers", "--yes",
        ])
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief", "--team", "tigers", "--yes",
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "already exists" in err

    def test_keyboard_interrupt_returns_130(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        def _interrupt(_):
            raise KeyboardInterrupt
        monkeypatch.setattr(builtins, "input", _interrupt)
        rc = main(["--dir", str(tmp_path)])
        assert rc == 130
        assert "aborted" in capsys.readouterr().err

    def test_eof_returns_130(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        def _eof(_):
            raise EOFError
        monkeypatch.setattr(builtins, "input", _eof)
        rc = main(["--dir", str(tmp_path)])
        assert rc == 130

    def test_team_dir_arg(self, tmp_path: Path):
        custom = tmp_path / "boxes" / "mybox"
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team-dir", str(custom),
            "--yes",
        ])
        assert rc == 0
        assert (custom / "personas" / "chief" / "prompt.md").exists()

    def test_via_top_level_cli(self, tmp_path: Path):
        from tigerharness.cli import main as cli_main
        rc = cli_main([
            "init",
            "--dir", str(tmp_path),
            "--persona", "scout",
            "--team", "tigers",
            "--yes",
        ])
        assert rc == 0
        assert (tmp_path / "tigers" / "personas" / "scout" / "prompt.md").exists()

    def test_next_steps_output_includes_paths(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team", "tigers",
            "--yes",
        ])
        out = capsys.readouterr().out
        assert "Next steps" in out
        assert "tigers/personas/chief/prompt.md" in out
        assert "tigers/configs/.env" in out
        # tiger-memory subcommand uses --config BEFORE the verb
        assert "tiger-memory --config" in out
        assert " init\n" in out  # the 'init' verb is at the end
        assert "TIGERHARNESS_PERSONAS_CONFIG=tigers/configs/personas.yaml" in out
        assert "task-runner assign --to chief" in out

    def test_next_steps_uses_uv_run_prefix_when_off_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """In a `uv add` install, `tigerharness` is in .venv/bin (off PATH).
        The "Next steps" output must prefix the runnable commands with
        `uv run` so copy-paste from the user's shell actually works."""
        from tigerharness import init as init_mod
        monkeypatch.setattr(init_mod.shutil, "which", lambda name: None)
        main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team", "tigers",
            "--yes",
        ])
        out = capsys.readouterr().out
        assert "uv run tigerharness tiger-memory --config" in out
        assert "uv run tigerharness task-runner assign --to chief" in out
        # Every `tigerharness <subcommand>` invocation must be prefixed --
        # if any bare one slipped through, the counts would differ.
        assert (
            out.count("tigerharness tiger-memory")
            == out.count("uv run tigerharness tiger-memory")
        )
        assert (
            out.count("tigerharness task-runner")
            == out.count("uv run tigerharness task-runner")
        )

    def test_next_steps_uses_bare_command_when_on_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """When `tigerharness` is on PATH (pip / pipx / uv tool install),
        no `uv run` prefix is needed -- printed commands stay bare."""
        from tigerharness import init as init_mod
        monkeypatch.setattr(
            init_mod.shutil, "which",
            lambda name: "/usr/local/bin/tigerharness" if name == "tigerharness" else None,
        )
        main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team", "tigers",
            "--yes",
        ])
        out = capsys.readouterr().out
        assert "tigerharness tiger-memory --config" in out
        assert "tigerharness task-runner assign --to chief" in out
        assert "uv run tigerharness" not in out

    def test_next_steps_numbering_is_sequential_no_gaps(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        """Skipping --no-slack must not leave a gap in the numbered list."""
        main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team", "tigers",
            "--no-slack",
            "--yes",
        ])
        out = capsys.readouterr().out
        # With slack skipped, memory still on, we expect:
        #   1. Edit prompt
        #   2. Edit memory config
        #   3. Initialize memory
        # No "4." and no missing "2.".
        assert "  1. Edit" in out
        assert "  2. Edit" in out
        assert "  3. Initialize" in out
        assert "  4." not in out
        # And no .env step (it was skipped)
        assert "Fill in" not in out

    def test_next_steps_minimal_when_no_memory_no_slack(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team", "tigers",
            "--no-slack", "--no-memory",
            "--yes",
        ])
        out = capsys.readouterr().out
        assert "  1. Edit" in out
        assert "  2." not in out
        assert "  3." not in out

    def test_nothing_to_do_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        """If init() returns an empty created list, main prints 'Nothing to do'."""
        from tigerharness import init as init_mod
        team = (tmp_path / "tigers").resolve()

        def _stub(**_kwargs):
            return team, "chief", []

        monkeypatch.setattr(init_mod, "init", _stub)
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief", "--team", "tigers", "--yes",
        ])
        assert rc == 0
        assert "Nothing to do" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Integration: generated artifacts actually work
# ---------------------------------------------------------------------------

class TestGeneratedConfigLoads:
    """End-to-end check that what `init` writes is consumable by
    downstream tigerharness components -- not just syntactically
    plausible markdown/YAML."""

    def test_personas_yaml_loads_and_resolves_prompt(self, tmp_path: Path):
        from tigerharness.task_runner.personas import (
            clear_registry,
            load_personas_config,
        )
        team_dir, _, _ = init(
            persona="chief", team="tigers",
            include_memory=False, include_slack=False,
            search_root=tmp_path,
        )
        clear_registry()
        try:
            personas = load_personas_config(
                team_dir / "configs" / "personas.yaml"
            )
        finally:
            clear_registry()

        assert len(personas) == 1
        chief = personas[0]
        assert chief.name == "chief"
        # cwd: .. resolves to the team root (not configs/)
        assert chief.cwd == team_dir
        # prompt_file resolves into personas/chief/prompt.md
        cfg = chief.build_config()
        assert "You are chief, part of team tigers" in cfg.instructions

    def test_two_personas_both_load(self, tmp_path: Path):
        from tigerharness.task_runner.personas import (
            clear_registry,
            load_personas_config,
        )
        init(
            persona="chief", team="tigers",
            include_memory=False, include_slack=False,
            search_root=tmp_path,
        )
        team_dir, _, _ = init(
            persona="scout", team="tigers",
            include_memory=False, include_slack=False,
            search_root=tmp_path,
        )
        clear_registry()
        try:
            personas = load_personas_config(
                team_dir / "configs" / "personas.yaml"
            )
        finally:
            clear_registry()
        names = sorted(p.name for p in personas)
        assert names == ["chief", "scout"]

    def test_memory_config_loads_and_resolves_store_root(self, tmp_path: Path):
        """The generated tiger-memory config must satisfy its own loader,
        and the auto-suffix logic must NOT double-append the agent slug."""
        from tigerharness.tiger_memory.config import load_config
        team_dir, _, _ = init(
            persona="chief", team="tigers",
            include_memory=True, include_slack=False,
            search_root=tmp_path,
        )
        cfg_path = team_dir / "memories" / "chief" / "tiger-memory.config.yaml"
        cfg = load_config(cfg_path)
        assert cfg.agent.name == "chief"
        # store.root="." with the config inside memories/chief/ should
        # resolve to memories/chief/ (not memories/chief/chief/) because
        # tiger-memory detects the slug match.
        assert cfg.store.root == (team_dir / "memories" / "chief").resolve()


# ---------------------------------------------------------------------------
# __main__ coverage
# ---------------------------------------------------------------------------

class TestDunderMain:
    def test_task_runner_main(self):
        with patch("tigerharness.task_runner.cli.main", return_value=0):
            with pytest.raises(SystemExit) as exc_info:
                runpy.run_module(
                    "tigerharness.task_runner",
                    run_name="__main__",
                    alter_sys=True,
                )
            assert exc_info.value.code == 0

    def test_task_runner_main_error(self):
        with patch("tigerharness.task_runner.cli.main", return_value=2):
            with pytest.raises(SystemExit) as exc_info:
                runpy.run_module(
                    "tigerharness.task_runner",
                    run_name="__main__",
                    alter_sys=True,
                )
            assert exc_info.value.code == 2
