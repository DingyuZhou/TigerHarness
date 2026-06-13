"""Tests for tigerharness.init team-based scaffolding."""
from __future__ import annotations

import builtins
import json
import runpy
from pathlib import Path
from unittest.mock import patch

import pytest

from tigerharness.init import (
    _scaffold_repos_yaml,
    _LEGACY_AUTOCOMPACT_ENV_KEY,
    _LEGACY_AUTOCOMPACT_SEEDED_PCT,
    _append_lane_to_slack_bridge_index,
    _append_persona_to_yaml,
    _auto_init_tiger_memory,
    _command_prefix,
    _remove_compact_env_in_file,
    _format_path,
    _inject_allowed_user_ids,
    _maybe_register_slack_bridge_lane,
    _prompt_choice,
    _prompt_optional_text,
    _prompt_text,
    _prompt_yes_no,
    _render_memory_config,
    _scaffold_claude_dir,
    _validate_name,
    _write_if_missing,
    add_persona,
    create_team,
    detect_claude_project_path,
    discover_teams,
    expected_claude_project_path,
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
        # Single internal spaces are valid: space-separated words,
        # each starting with an alphanumeric.
        "Chuan Ying", "Chuan Ying Wu", "Tiger Team", "9 to 5",
    ])
    def test_accepts_valid(self, name: str):
        assert _validate_name(name, kind="persona") == name

    def test_strips_whitespace(self):
        assert _validate_name("  chief  ", kind="persona") == "chief"

    def test_strips_whitespace_around_spaced_name(self):
        assert _validate_name(" Chuan Ying ", kind="persona") == "Chuan Ying"

    @pytest.mark.parametrize("name", [
        "", "   ", "chief/scout", "..", ".hidden", "-leading-dash",
        "with/slash", "with\\back", "weird:char",
        "../escape", "name.with.dot",
        # Space edge classes: only single INTERNAL ASCII spaces are
        # valid. (Leading/trailing spaces are stripped before
        # validation, so they are exercised in the accept tests.)
        "Chuan  Ying",      # consecutive spaces
        "Chuan\tYing",      # tab
        "Chuan\u00a0Ying",  # unicode (non-breaking) space
        "Chuan -Ying",      # word starting with non-alphanumeric
        # ASCII-only lock: non-ASCII letters stay rejected so the
        # error text keeps describing the real rule.
        "\u5bab\u57ce",        # 宫城
        "\u5bab\u57ce \u826f\u7530",  # 宫城 良田
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
    def test_writes_expected_path_when_dir_doesnt_exist_yet(
        self, tmp_path: Path
    ):
        """No claude transcripts dir yet -> still writes the REAL
        expected path (not a placeholder), so the user doesn't have
        to come back and edit it once the bridge dispatches."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        proj = tmp_path / "tigers"
        proj.mkdir()
        cfg = _render_memory_config(
            persona="chief", team="tigers",
            project_root=proj, home=fake_home,
        )
        encoded = str(proj.resolve()).replace("/", "-")
        # The REAL expected path appears, not the old placeholder.
        assert encoded in cfg
        assert "-home-user-myproject" not in cfg  # never use the placeholder
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

    def test_multi_team_adds_persona_filter_and_slack_source(self, tmp_path: Path):
        """When multi_team=True, the rendered config includes the
        persona filter on the claude_code source and a slack_thread
        source pointing at the bridge's per-team threads.json."""
        fake_home = tmp_path / "home"
        proj = tmp_path / "shohoku"
        proj.mkdir()
        cfg = _render_memory_config(
            persona="ayako", team="shohoku",
            project_root=proj, multi_team=True, home=fake_home,
        )
        assert "persona: ayako" in cfg
        assert "kind: slack_thread" in cfg
        assert "~/.local/state/slack-bridge/shohoku/threads.json" in cfg

    def test_single_tenant_has_no_persona_filter(self, tmp_path: Path):
        """multi_team=False keeps the legacy single-source layout."""
        fake_home = tmp_path / "home"
        proj = tmp_path / "shohoku"
        proj.mkdir()
        cfg = _render_memory_config(
            persona="ayako", team="shohoku",
            project_root=proj, multi_team=False, home=fake_home,
        )
        assert "persona: ayako" not in cfg
        assert "kind: slack_thread" not in cfg
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
        # charter + knowledge: the team's entry point + curated base.
        assert (team / "charter" / "README.md").exists()
        assert (team / "knowledge" / "README.md").exists()
        charter_text = (team / "charter" / "README.md").read_text()
        assert "Team charter -- tigers" in charter_text
        assert "First-read checklist" in charter_text
        knowledge_text = (team / "knowledge" / "README.md").read_text()
        assert "Team knowledge -- tigers" in knowledge_text
        # AGENTS.md + CLAUDE.md: the auto-loaded vendor-neutral entry point
        assert (team / "AGENTS.md").exists()
        assert (team / "CLAUDE.md").exists()
        # .claude/ directory scaffolded
        assert (team / ".claude" / "settings.json").exists()
        settings_text = (team / ".claude" / "settings.json").read_text()
        assert "TIGERHARNESS_PERSONAS_CONFIG" in settings_text
        # Skills copied from package
        assert (team / ".claude" / "skills" / "slack-notify" / "SKILL.md").exists()
        # gitignore excludes secrets and the runner's working journal,
        # but NOT archive/journal (those are git-tracked memory summaries).
        gi_text = (team / ".gitignore").read_text()
        assert "configs/.env" in gi_text
        assert "memories/*/archive/" not in gi_text
        assert "memories/*/journal/" not in gi_text
        # personas.yaml header references team name
        assert "Team: tigers" in (team / "configs" / "personas.yaml").read_text()
        # tiger-memory defaults created
        assert (team / "configs" / "tiger-memory.defaults.yaml").exists()
        defaults_text = (team / "configs" / "tiger-memory.defaults.yaml").read_text()
        assert "summarizer:" in defaults_text
        assert "claude-sonnet-4-6" in defaults_text
        # Base paths (9: gitignore, personas.yaml, mem defaults, .env,
        # skills README, charter README, knowledge README, AGENTS.md,
        # CLAUDE.md) + settings.json + 2 skills = 12.
        assert len(created) >= 12

    def test_entry_point_files_wired(self, tmp_path: Path):
        """AGENTS.md is the vendor-neutral entry point; CLAUDE.md imports
        it so Claude Code loads the same source. AGENTS.md must stay
        persona-name-agnostic (point at default_persona, not a hardcoded
        name) and keep the conditional rule that stops it overwriting a
        launcher-assigned persona."""
        team = tmp_path / "shohoku"
        create_team(team, include_slack=False)

        agents = (team / "AGENTS.md").read_text()
        # Team name interpolated, not a generic doc.
        assert "shohoku -- agent session bootstrap" in agents
        # Default resolved from personas.yaml, never a hardcoded name.
        assert "default_persona" in agents
        assert "configs/personas.yaml" in agents
        # Conditional rule: an already-assigned persona is not overwritten.
        assert "If your system prompt already names a specific" in agents
        # Routes to the charter (the operating manual) rather than duplicating it.
        assert "charter/README.md" in agents
        # The voice-only / no --driver caveat is preserved.
        assert "--driver" in agents
        # Regression: the generic template scaffolds arbitrary teams, so it
        # must not leak the Shohoku/Slam Dunk roster as hardcoded examples.
        for leaked in ("Rukawa", "Anzai", "Sakuragi", "Ayako"):
            assert leaked not in agents, f"template leaks persona name: {leaked}"

        claude = (team / "CLAUDE.md").read_text()
        # Thin pointer that imports the single source of truth.
        assert "@AGENTS.md" in claude

    def test_skip_slack(self, tmp_path: Path):
        team = tmp_path / "tigers"
        create_team(team, include_slack=False)
        assert not (team / "configs" / ".env").exists()
        assert (team / "configs" / "personas.yaml").exists()
        # charter + knowledge: scaffolded regardless of slack opt-in
        assert (team / "charter" / "README.md").exists()
        assert (team / "knowledge" / "README.md").exists()

    def test_charter_and_knowledge_seeded_for_team_name(self, tmp_path: Path):
        """Charter and knowledge READMEs interpolate the team name in
        their headers -- so the team isn't reading a generic doc."""
        team = tmp_path / "shohoku"
        create_team(team, include_slack=False)
        assert (
            "Team charter -- shohoku"
            in (team / "charter" / "README.md").read_text()
        )
        assert (
            "Team knowledge -- shohoku"
            in (team / "knowledge" / "README.md").read_text()
        )

    def test_charter_links_to_knowledge_index(self, tmp_path: Path):
        """The charter's 'Using team knowledge' section must point at
        ../knowledge/ so personas know where the reference base lives.
        Without this, the team's entry-point doc is incoherent."""
        team = tmp_path / "tigers"
        create_team(team, include_slack=False)
        charter = (team / "charter" / "README.md").read_text()
        assert "../knowledge/" in charter
        assert "INDEX.md" in charter

    def test_knowledge_readme_excludes_governance_and_journal(
        self, tmp_path: Path,
    ):
        """The knowledge README's 'doesn't belong here' list must
        explicitly mention `../charter/` (governance lives there, not
        here) and `../task_journal/` (runtime artifact). Without these
        pointers, the knowledge folder becomes a dumping ground."""
        team = tmp_path / "tigers"
        create_team(team, include_slack=False)
        kb = (team / "knowledge" / "README.md").read_text()
        assert "../charter/" in kb

    def test_first_read_order_project_before_briefing(
        self, tmp_path: Path,
    ):
        """The charter's first-read checklist puts the project's own
        README before the persona's tiger-memory briefing. A new
        persona needs to orient on the project first; briefings are
        most useful once that context is established."""
        team = tmp_path / "tigers"
        create_team(team, include_slack=False)
        charter = (team / "charter" / "README.md").read_text()
        # Anchor on a substring unique to each step so the test pins
        # the ORDER, not just the existence of each line.
        project_idx = charter.find("project repo's top-level `README.md`")
        briefing_idx = charter.find(
            "briefing at `../memories/<persona>/briefing/README.md`"
        )
        assert project_idx > 0, "project README step missing from charter"
        assert briefing_idx > 0, "briefing step missing from charter"
        assert project_idx < briefing_idx, (
            "first-read checklist has briefing before project README "
            "-- new personas should orient on the project first"
        )

    def test_idempotent(self, tmp_path: Path):
        team = tmp_path / "tigers"
        create_team(team, include_slack=True)
        # Second run creates nothing
        created = create_team(team, include_slack=True)
        assert created == []


class TestScaffoldClaudeDir:
    """_scaffold_claude_dir creates .claude/settings.json + skills."""

    def test_settings_json_written(self, tmp_path: Path):
        team = tmp_path / "myteam"
        team.mkdir()
        created = _scaffold_claude_dir(team)
        settings = team / ".claude" / "settings.json"
        assert settings.exists()
        assert settings in created
        text = settings.read_text()
        assert "TIGERHARNESS_PERSONAS_CONFIG" in text
        # Team-root-relative on purpose (T6 portability): the same
        # checked-in settings file must work on every machine.
        assert '"configs/personas.yaml"' in text
        assert "myteam" not in text

    def test_add_persona_backfills_repos_yaml(self, tmp_path: Path):
        # Sakuragi (b2 defense): an existing pre-T6 team that re-runs
        # init to add a persona must adopt repos.yaml -- create_team
        # is not the only door into an aging team.
        team = tmp_path / "teams" / "oldteam"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas_dir: ../personas\npersonas: []\n"
        )
        add_persona(team, "newbie", include_memory=False)
        assert (team / "configs" / "repos.yaml").exists()

    def test_repos_yaml_detection_hit(self, tmp_path: Path):
        # Layout: tmp/projects/{tigerharness, teams/myteam}
        proj = tmp_path / "projects" / "tigerharness"
        proj.mkdir(parents=True)
        # The REAL spelling -- the fixture must match reality or the
        # test verifies the code against itself (b2-haruko lesson).
        (proj / "pyproject.toml").write_text(
            '[project]\nname = "TigerHarness"\n'
        )
        team = tmp_path / "projects" / "teams" / "myteam"
        team.mkdir(parents=True)
        path = _scaffold_repos_yaml(team)
        assert path is not None and path.exists()
        text = path.read_text()
        assert "team_root: ." in text
        assert "project: ../../tigerharness" in text

    def test_repos_yaml_detection_hit_lowercase_name(self, tmp_path: Path):
        proj = tmp_path / "projects" / "tigerharness"
        proj.mkdir(parents=True)
        (proj / "pyproject.toml").write_text(
            '[project]\nname = "tigerharness"\n'
        )
        team = tmp_path / "projects" / "teams" / "myteam"
        team.mkdir(parents=True)
        path = _scaffold_repos_yaml(team)
        assert path is not None
        assert "project: ../../tigerharness" in path.read_text()

    def test_detection_walks_past_odd_children(self, tmp_path: Path):
        # Non-dir sibling, dir without pyproject, unreadable pyproject
        # (a directory), then the real hit -- all in one walk.
        projects = tmp_path / "projects"
        (projects / "teams" / "myteam").mkdir(parents=True)
        (projects / "loose-file.txt").write_text("not a dir")
        (projects / "no-pyproject").mkdir()
        weird = projects / "weird"
        (weird / "pyproject.toml").mkdir(parents=True)  # read -> OSError
        proj = projects / "zz-tigerharness"
        proj.mkdir()
        (proj / "pyproject.toml").write_text(
            '[project]\nname = "TigerHarness"\n')
        team = projects / "teams" / "myteam"
        path = _scaffold_repos_yaml(team)
        assert path is not None
        assert "project: ../../zz-tigerharness" in path.read_text()

    def test_detection_tolerates_unreadable_pyproject_and_other_names(
        self, tmp_path: Path, monkeypatch,
    ):
        from tigerharness.init import _detect_project_dir
        projects = tmp_path / "projects"
        team = projects / "teams" / "myteam"
        team.mkdir(parents=True)
        other = projects / "aa-other"
        other.mkdir()
        (other / "pyproject.toml").write_text(
            '[project]\nname = "somethingelse"\n')
        secret = projects / "bb-secret"
        secret.mkdir()
        (secret / "pyproject.toml").write_text("locked")
        real_read = Path.read_text

        def flaky_read(self, *a, **k):
            if "bb-secret" in str(self):
                raise OSError("permission denied")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", flaky_read)
        proj = projects / "zz-tigerharness"
        proj.mkdir()
        (proj / "pyproject.toml").write_text(
            '[project]\nname = "TigerHarness"\n')
        assert _detect_project_dir(team) == proj

    def test_detection_tolerates_unstatable_sibling(
        self, tmp_path: Path, monkeypatch,
    ):
        """A level-mate whose entries can't even be stat()ed (e.g.
        /tmp's systemd-private-* dirs, mode 700 root-owned) must be
        skipped, not crash init: pathlib's is_file() re-raises EACCES
        -- only the ENOENT class is swallowed. Regression: found by
        the fresh-init smoke run against a real /tmp."""
        from tigerharness.init import _detect_project_dir
        projects = tmp_path / "projects"
        team = projects / "teams" / "myteam"
        team.mkdir(parents=True)
        locked = projects / "aa-locked"
        locked.mkdir()
        (locked / "pyproject.toml").write_text("secret")
        real_is_file = Path.is_file

        def denied_is_file(self):
            if "aa-locked" in str(self):
                raise PermissionError(13, "Permission denied", str(self))
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", denied_is_file)
        proj = projects / "zz-tigerharness"
        proj.mkdir()
        (proj / "pyproject.toml").write_text(
            '[project]\nname = "TigerHarness"\n')
        assert _detect_project_dir(team) == proj

    def test_detection_tolerates_unlistable_parent(
        self, tmp_path: Path, monkeypatch,
    ):
        from tigerharness.init import _detect_project_dir
        team = tmp_path / "projects" / "teams" / "myteam"
        team.mkdir(parents=True)
        real_iterdir = Path.iterdir

        def flaky_iterdir(self):
            if self.name == "teams":
                raise OSError("denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", flaky_iterdir)
        # teams/ unlistable -> level skipped; nothing matches above.
        assert _detect_project_dir(team) is None

    def test_detection_stops_at_filesystem_root(self):
        from tigerharness.init import _detect_project_dir
        from pathlib import Path as _P
        # parent == current short-circuits the walk; read-only probe.
        assert _detect_project_dir(_P("/")) is None

    def test_repos_yaml_detection_miss_writes_placeholder(
        self, tmp_path: Path, capsys,
    ):
        team = tmp_path / "teams" / "myteam"
        team.mkdir(parents=True)
        path = _scaffold_repos_yaml(team)
        assert path is not None
        text = path.read_text()
        assert "# project:" in text and "set me" in text
        assert "could not auto-detect" in capsys.readouterr().err

    def test_repos_yaml_never_clobbers_existing(self, tmp_path: Path):
        team = tmp_path / "myteam"
        (team / "configs").mkdir(parents=True)
        existing = team / "configs" / "repos.yaml"
        existing.write_text("team_root: .\nproject: /custom/spot\n")
        assert _scaffold_repos_yaml(team) is None
        assert existing.read_text() == "team_root: .\nproject: /custom/spot\n"

    def test_skills_copied(self, tmp_path: Path):
        team = tmp_path / "myteam"
        team.mkdir()
        created = _scaffold_claude_dir(team)
        assert (team / ".claude" / "skills" / "slack-notify" / "SKILL.md").exists()
        assert (
            team / ".claude" / "skills" / "tigerharness-basics" / "SKILL.md"
        ).exists()
        # At least settings.json + 2 skills
        assert len(created) >= 3

    def test_idempotent(self, tmp_path: Path):
        team = tmp_path / "myteam"
        team.mkdir()
        _scaffold_claude_dir(team)
        created = _scaffold_claude_dir(team)
        assert created == []

    def test_no_bundled_skills_dir_is_fine(self, tmp_path: Path):
        """When _bundled_skills doesn't exist, only settings.json is created."""
        team = tmp_path / "myteam"
        team.mkdir()
        import tigerharness.init as init_mod
        real_file = Path(init_mod.__file__).resolve()
        real_skills = real_file.parent / "_bundled_skills"
        backup = real_skills.rename(real_skills.with_suffix(".gone"))
        try:
            created = _scaffold_claude_dir(team)
            # settings.json still created, but no skills
            assert (team / ".claude" / "settings.json").exists()
            assert not (team / ".claude" / "skills").exists()
            assert len(created) == 1
        finally:
            backup.rename(real_skills)

    def test_skill_dir_without_skill_md_skipped(self, tmp_path: Path):
        """A subdirectory in _bundled_skills with no SKILL.md is skipped."""
        team = tmp_path / "myteam"
        team.mkdir()
        # Patch the package skills dir to include a bogus entry
        fake_skills = tmp_path / "fake_skills"
        fake_skills.mkdir()
        (fake_skills / "real-skill").mkdir()
        (fake_skills / "real-skill" / "SKILL.md").write_text("# real\n")
        (fake_skills / "empty-dir").mkdir()  # no SKILL.md
        with patch("tigerharness.init.Path.__file__", create=True):
            # Simpler: patch the resolved path directly
            import tigerharness.init as init_mod
            original = init_mod.Path
            with patch.object(init_mod, "Path", wraps=original) as mock_path:
                # Can't easily patch __file__; just call _scaffold_claude_dir
                # with a custom bundled skills dir. Refactor: use the real
                # function but point it at our fake dir.
                pass
        # Direct approach: temporarily replace the skills dir
        import tigerharness.init as init_mod
        real_file = Path(init_mod.__file__).resolve()
        real_skills = real_file.parent / "_bundled_skills"
        # Rename real -> backup, put fake in its place
        backup = real_skills.rename(real_skills.with_suffix(".bak"))
        try:
            fake_skills.rename(real_skills)
            created = _scaffold_claude_dir(team)
            # Should have settings.json + real-skill, NOT empty-dir
            assert (team / ".claude" / "skills" / "real-skill" / "SKILL.md").exists()
            assert not (team / ".claude" / "skills" / "empty-dir").exists()
        finally:
            # Restore
            real_skills.rename(fake_skills)
            backup.rename(real_skills)


# ---------------------------------------------------------------------------
# Journal write-guard PreToolUse hook wiring
# ---------------------------------------------------------------------------



class TestJournalGuardHelpers:
    """Unit coverage for the hook-merge helpers."""

class TestRemoveCompactEnvInFile:
    """Layer A is retired (Operator ruling 2026-06-11): the remover
    takes back the key WE seeded, and only that."""

    def test_removes_old_seeded_default(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"env": {
                "KEEP": "1",
                _LEGACY_AUTOCOMPACT_ENV_KEY: _LEGACY_AUTOCOMPACT_SEEDED_PCT,
            }}, indent=2) + "\n",
            encoding="utf-8",
        )
        assert _remove_compact_env_in_file(path) is True
        env = json.loads(path.read_text())["env"]
        assert _LEGACY_AUTOCOMPACT_ENV_KEY not in env
        assert env["KEEP"] == "1"  # everything else preserved

    def test_leaves_operator_chosen_value_and_logs(
        self, tmp_path: Path, caplog,
    ):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"env": {_LEGACY_AUTOCOMPACT_ENV_KEY: "70"}},
                       indent=2) + "\n",
            encoding="utf-8",
        )
        import logging
        with caplog.at_level(logging.INFO):
            assert _remove_compact_env_in_file(path) is False
        env = json.loads(path.read_text())["env"]
        assert env[_LEGACY_AUTOCOMPACT_ENV_KEY] == "70"  # untouched
        assert "explicit choice" in caplog.text

    def test_absent_key_is_noop(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"env": {"FOO": "bar"}}) + "\n",
                        encoding="utf-8")
        assert _remove_compact_env_in_file(path) is False

    def test_missing_file_returns_false(self, tmp_path: Path):
        missing = tmp_path / "nope" / "settings.json"
        assert _remove_compact_env_in_file(missing) is False

    def test_malformed_file_untouched(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text("{not json", encoding="utf-8")
        assert _remove_compact_env_in_file(path) is False
        assert path.read_text() == "{not json"

    def test_non_dict_settings_or_env(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps([1, 2]) + "\n", encoding="utf-8")
        assert _remove_compact_env_in_file(path) is False
        path.write_text(json.dumps({"env": "nope"}) + "\n",
                        encoding="utf-8")
        assert _remove_compact_env_in_file(path) is False

class TestScaffoldGuardHook:
    """_scaffold_claude_dir creates settings.json and tidies an existing
    one (removes the retired compact key; nothing is injected)."""

    def test_existing_settings_merged_additively(self, tmp_path: Path):
        """An existing settings.json gains the compact env additively --
        pre-existing keys survive, nothing else is injected."""
        team = tmp_path / "tigers"
        (team / ".claude").mkdir(parents=True)
        settings_path = team / ".claude" / "settings.json"
        settings_path.write_text(json.dumps({"env": {
            "KEEP": "1",
            _LEGACY_AUTOCOMPACT_ENV_KEY: _LEGACY_AUTOCOMPACT_SEEDED_PCT,
        }}) + "\n")
        _scaffold_claude_dir(team)
        merged = json.loads(settings_path.read_text())
        assert merged["env"]["KEEP"] == "1"
        # Layer A retired: the old seeded key is actively removed.
        assert _LEGACY_AUTOCOMPACT_ENV_KEY not in merged["env"]
        assert "hooks" not in merged

    def test_malformed_existing_settings_not_in_created(self, tmp_path: Path):
        team = tmp_path / "myteam"
        (team / ".claude").mkdir(parents=True)
        settings_path = team / ".claude" / "settings.json"
        settings_path.write_text("{ broken", encoding="utf-8")
        created = _scaffold_claude_dir(team)
        assert settings_path not in created
        assert settings_path.read_text() == "{ broken"


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
        # The first persona added becomes the team's default.
        assert "default_persona: chief" in text

    def test_does_not_inject_default_persona_into_existing_file(
        self, tmp_path: Path,
    ):
        """An existing personas.yaml that has no default_persona: line
        is left alone -- silently mid-file editing could surprise an
        operator who's been managing the yaml by hand. default_persona
        is only seeded when this function creates a fresh file."""
        yp = tmp_path / "personas.yaml"
        yp.write_text("personas:\n  - name: scout\n")
        _append_persona_to_yaml(yp, "chief", "desc", "tigers")
        text = yp.read_text()
        # No default_persona injected.
        assert "default_persona:" not in text
        # Both personas still in the file.
        assert "- name: scout" in text
        assert "- name: chief" in text

    def test_does_not_clobber_existing_default_persona(
        self, tmp_path: Path,
    ):
        """If the yaml already has a default_persona: line, leave it
        alone -- the operator's choice wins over any change."""
        yp = tmp_path / "personas.yaml"
        yp.write_text(
            "default_persona: scout\n"
            "personas:\n  - name: scout\n"
        )
        _append_persona_to_yaml(yp, "chief", "desc", "tigers")
        text = yp.read_text()
        # Scout still the default; only one default_persona line.
        assert "default_persona: scout" in text
        assert text.count("default_persona:") == 1

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

    def test_prompt_wires_charter_and_knowledge(self, tmp_path: Path):
        """The generated persona prompt must wire in the team's
        charter + knowledge as the first thing a persona reads. This
        is the contract that makes those folders load-bearing rather
        than decorative -- if a future edit drops the references,
        every new persona starts disconnected from team governance."""
        team = tmp_path / "tigers"
        create_team(team, include_slack=False)
        add_persona(team, "chief", include_memory=False)
        prompt = (team / "personas" / "chief" / "prompt.md").read_text()
        # Section header + both pointers + briefing pointer.
        assert "Before you start work" in prompt
        assert "../charter/README.md" in prompt
        # INDEX.md is the primary entry; README is the fallback.
        idx_pos = prompt.find("../knowledge/INDEX.md")
        readme_pos = prompt.find("../knowledge/README.md")
        assert idx_pos > 0, "persona prompt missing knowledge INDEX pointer"
        assert readme_pos > 0, "persona prompt missing knowledge README fallback"
        assert idx_pos < readme_pos, (
            "knowledge INDEX should be listed before README -- INDEX "
            "is the primary entry, README is a fallback for new teams"
        )
        assert "../memories/chief/briefing/README.md" in prompt

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
        # prompt + yaml + backfilled repos.yaml = 3 paths
        assert len(created) == 3
        assert (team / "configs" / "repos.yaml").exists()

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

    def test_main_skips_fragment_when_explicitly_opting_out(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        """Legacy single-tenant mode via --no-multi-team. No fragment,
        no top-level index, no multi-bridge mention in Next steps."""
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "ayako",
            "--team", "shohoku",
            "--yes", "--no-multi-team",
        ])
        assert rc == 0
        fragment = tmp_path / "shohoku" / "configs" / "slack-bridge.yaml"
        assert not fragment.exists()
        assert not (tmp_path / "slack-bridge.yaml").exists()
        out = capsys.readouterr().out
        assert "slack-bridge.yaml" not in out

    def test_main_creates_index_by_default_under_yes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        """The new default: --yes opts into multi-team mode. Both the
        index file and the per-team fragment get auto-generated."""
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "ayako",
            "--team", "shohoku",
            "--yes",
        ])
        assert rc == 0
        # Top-level index exists with the team registered.
        idx = tmp_path / "slack-bridge.yaml"
        assert idx.exists()
        assert "  - shohoku\n" in idx.read_text()
        # Per-team fragment exists.
        fragment = tmp_path / "shohoku" / "configs" / "slack-bridge.yaml"
        assert fragment.exists()
        # Next-steps mentions the fragment.
        out = capsys.readouterr().out
        assert "slack-bridge.yaml" in out


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
            include_multi_team=False,  # scripted: opt out, don't prompt
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
        # Seed team with .env (include_multi_team=False to avoid prompt)
        init(
            persona="chief", team="tigers",
            include_memory=False, include_slack=True,
            include_multi_team=False,
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
            include_multi_team=False,
            search_root=tmp_path,
        )
        # Add scout WITH slack now -- .env should appear
        team_dir, _, _ = init(
            persona="scout",
            team="tigers",
            include_memory=False,
            include_slack=True,
            include_multi_team=False,
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
        """Interactive prompts in order: persona name -> team name ->
        slack y/n -> multi-team y/n (gated on slack) -> memory y/n."""
        answers = iter([
            "chief",     # persona name
            "tigers",    # team name (new)
            "y",         # slack? yes
            "y",         # multi-team? yes (only asked because slack=yes)
            "y",         # memory? yes
        ])
        monkeypatch.setattr(builtins, "input", lambda _: next(answers))
        # Mock the tiger-memory init subprocess so this test doesn't try
        # to invoke the real CLI (which would need the venv).
        with patch("tigerharness.init.subprocess.run"):
            team_dir, persona, created = init(search_root=tmp_path)
        assert persona == "chief"
        assert team_dir.name == "tigers"
        assert (team_dir / "personas" / "chief" / "prompt.md").exists()
        # Multi-team was enabled -> index exists.
        assert (tmp_path / "slack-bridge.yaml").exists()

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
        # Seed (--no-multi-team keeps this test focused on team selection,
        # not multi-bridge scaffolding)
        main([
            "--dir", str(tmp_path),
            "--persona", "scout",
            "--team", "tigers",
            "--yes", "--no-memory", "--no-slack", "--no-multi-team",
        ])
        # Now without --team
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--yes", "--no-memory", "--no-slack", "--no-multi-team",
        ])
        assert rc == 0
        assert (tmp_path / "tigers" / "personas" / "chief" / "prompt.md").exists()
        # No second team was spuriously created (check directories only).
        team_dirs = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
        assert team_dirs == ["tigers"]

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
        # Charter customization gets a nudge on first scaffold -- the
        # seeded charter ships with TODO markers users often forget.
        assert "tigers/charter/README.md" in out
        assert "Mission" in out
        assert "tigers/configs/.env" in out
        # Memory config gets the auto-init treatment now -- "review",
        # not "set sources.project_path" or "Initialize memory".
        assert "tigers/memories/chief/tiger-memory.config.yaml" in out
        # `tigerharness tiger-memory init` is no longer in the next-steps
        # because we auto-run it in PR8's UX pass.
        assert "tiger-memory --config" not in out
        assert "TIGERHARNESS_PERSONAS_CONFIG=tigers/configs/personas.yaml" in out
        assert "journal new --kind task --persona chief" in out

    def test_next_steps_charter_nudge_only_on_first_scaffold(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        """The charter-customization step appears the first time a team
        is scaffolded (the TODOs are fresh), but NOT on re-runs (the
        team may have already filled in the charter). Tying the nudge
        to ``charter in created`` prevents nagging existing teams."""
        # First run: charter step should appear.
        main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team", "tigers",
            "--yes",
        ])
        first_out = capsys.readouterr().out
        assert "tigers/charter/README.md" in first_out

        # Second run: add another persona to the same team. Charter
        # already exists, so the nudge must NOT fire again.
        main([
            "--dir", str(tmp_path),
            "--persona", "scout",
            "--team", "tigers",
            "--yes",
        ])
        second_out = capsys.readouterr().out
        assert "tigers/charter/README.md" not in second_out
        # Confirm the run actually did something else (sanity check
        # the re-run wasn't a no-op that would trivially pass).
        assert "tigers/personas/scout/prompt.md" in second_out

    def test_next_steps_uses_uv_run_prefix_when_off_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """In a `uv add` install, `tigerharness` is in .venv/bin (off PATH).
        The "To run tasks" snippet must prefix the runnable command with
        `uv run` so copy-paste from the user's shell actually works.

        (The `tiger-memory init` line is no longer printed -- auto-run
        in PR8's UX pass -- so we only check the journal snippet.)"""
        from tigerharness import init as init_mod
        monkeypatch.setattr(init_mod.shutil, "which", lambda name: None)
        main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team", "tigers",
            "--yes",
        ])
        out = capsys.readouterr().out
        assert "uv run tigerharness journal new --kind task --persona chief" in out
        # Every `tigerharness <subcommand>` invocation must be prefixed.
        assert (
            out.count("tigerharness journal new")
            == out.count("uv run tigerharness journal new")
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
        # `tigerharness tiger-memory init` line is no longer printed
        # (auto-run in PR8); the journal scaffold is the Next Steps hint.
        assert "tigerharness journal new --kind task --persona chief" in out
        assert "uv run tigerharness" not in out

    def test_next_steps_numbering_is_sequential_no_gaps(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        """Skipping --no-slack must not leave a gap in the numbered list.
        Uses --no-multi-team so this test stays focused on slack/memory
        toggle interaction with the numbering (the multi-team default
        would add another step that's tested separately).

        Memory auto-init in PR8 removed the separate 'Initialize memory'
        step; the review hint is now a single optional step."""
        main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team", "tigers",
            "--no-slack", "--no-multi-team",
            "--yes",
        ])
        out = capsys.readouterr().out
        # With slack + multi-team skipped, memory still on, and the
        # team freshly scaffolded (so the charter customization nudge
        # fires), we expect:
        #   1. Edit prompt
        #   2. Customize charter
        #   3. (Optional) review memory config
        # No "4." and no missing slot.
        assert "  1. Edit" in out
        assert "  2. Customize" in out
        assert "  3. (Optional)" in out
        assert "  4." not in out
        # And no .env step (it was skipped)
        assert "Fill in" not in out

    def test_next_steps_minimal_when_no_memory_no_slack(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        """Memory + Slack off, on a fresh team scaffold -- two steps:
        edit the persona prompt, customize the seeded charter."""
        main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team", "tigers",
            "--no-slack", "--no-memory", "--no-multi-team",
            "--yes",
        ])
        out = capsys.readouterr().out
        assert "  1. Edit" in out
        assert "  2. Customize" in out
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


class TestRefreshSkills:
    """`tigerharness init --refresh-skills` brings an existing team's
    bundled skills current without touching personas: installs missing
    skills, refreshes any skill byte-identical to a previously-shipped
    version, leaves hand-edited skills alone, and tidies
    `.claude/settings.json` (removes the retired mid-task compact
    override if still at the old seeded default). Idempotent."""

    def test_installs_missing_bundled_skills(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        # First scaffold a team via --yes (this copies whatever skills
        # were bundled at that moment).
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief", "--team", "tigers", "--yes",
        ])
        assert rc == 0
        team_dir = tmp_path / "tigers"
        # Remove a bundled skill from the team to simulate "team was
        # scaffolded BEFORE this skill was added to the bundle."
        target = team_dir / ".claude" / "skills" / "journal-new" / "SKILL.md"
        if target.exists():
            target.unlink()
            target.parent.rmdir()
        capsys.readouterr()
        # Refresh should reinstall it.
        rc = main(["--dir", str(tmp_path), "--refresh-skills"])
        assert rc == 0
        assert target.is_file()
        out = capsys.readouterr().out
        assert "Installed" in out
        assert "journal-new" in out

    def test_refresh_installs_tigerharness_basics(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        """A team scaffolded before the tigerharness-basics skill existed
        picks it up via --refresh-skills (the brief's acceptance path)."""
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief", "--team", "tigers", "--yes",
        ])
        assert rc == 0
        team_dir = tmp_path / "tigers"
        target = (
            team_dir / ".claude" / "skills" / "tigerharness-basics"
            / "SKILL.md"
        )
        assert target.is_file()  # fresh scaffold already installs it
        target.unlink()
        target.parent.rmdir()
        capsys.readouterr()
        rc = main(["--dir", str(tmp_path), "--refresh-skills"])
        assert rc == 0
        assert target.is_file()
        out = capsys.readouterr().out
        assert "Installed" in out
        assert "tigerharness-basics" in out

    def test_idempotent_when_nothing_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief", "--team", "tigers", "--yes",
        ])
        assert rc == 0
        capsys.readouterr()
        rc = main(["--dir", str(tmp_path), "--refresh-skills"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Nothing to do" in out

    def test_does_not_clobber_hand_edited_skill(self, tmp_path: Path):
        """If the team has a hand-edited SKILL.md, refresh leaves it
        alone -- the operator's local edit always wins."""
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief", "--team", "tigers", "--yes",
        ])
        assert rc == 0
        target = (
            tmp_path / "tigers" / ".claude" / "skills"
            / "journal-new" / "SKILL.md"
        )
        target.write_text("# hand-edited; do not overwrite\n")
        rc = main(["--dir", str(tmp_path), "--refresh-skills"])
        assert rc == 0
        assert "do not overwrite" in target.read_text()

    def test_refresh_updates_unmodified_prior_and_keeps_handedited(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A skill byte-identical to a *prior shipped* version is refreshed
        to the current bundled version (the team never customized it);
        a hand-edited skill is left alone."""
        import hashlib
        from pathlib import Path as _Path
        import tigerharness.init as _init

        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief", "--team", "tigers", "--yes",
        ])
        assert rc == 0
        skills = tmp_path / "tigers" / ".claude" / "skills"
        dj = skills / "drive-journal" / "SKILL.md"
        jn = skills / "journal-new" / "SKILL.md"
        # drive-journal: overwrite with a synthetic *prior shipped* version.
        prior = "# drive-journal -- an earlier shipped version\n"
        dj.write_text(prior)
        # journal-new: a genuine hand-edit (matches no shipped version).
        jn.write_text("# hand-edited journal-new\nkeep me\n")
        monkeypatch.setattr(
            "tigerharness.init._PRIOR_SKILL_HASHES",
            {"drive-journal": {hashlib.sha256(prior.encode()).hexdigest()}},
        )
        capsys.readouterr()
        rc = main(["--dir", str(tmp_path), "--refresh-skills"])
        assert rc == 0
        # drive-journal refreshed to the exact current bundled content.
        bundled = (
            _Path(_init.__file__).parent / "_bundled_skills"
            / "drive-journal" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert dj.read_text(encoding="utf-8") == bundled
        # journal-new hand-edit preserved.
        assert "keep me" in jn.read_text()
        out = capsys.readouterr().out
        assert "Refreshed" in out
        assert "Left" in out and "hand-edited" in out

    def test_refresh_removes_legacy_seeded_compact_key(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        """A team still carrying the old seeded Layer-A default ("50")
        sheds it on --refresh-skills; an operator-chosen value stays."""
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief", "--team", "tigers", "--yes",
        ])
        assert rc == 0
        settings_path = tmp_path / "tigers" / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        # Simulate a pre-redesign team: the seeded key is present.
        settings["env"][_LEGACY_AUTOCOMPACT_ENV_KEY] = (
            _LEGACY_AUTOCOMPACT_SEEDED_PCT
        )
        settings_path.write_text(
            json.dumps(settings, indent=2) + "\n", encoding="utf-8"
        )
        capsys.readouterr()
        rc = main(["--dir", str(tmp_path), "--refresh-skills"])
        assert rc == 0
        refreshed = json.loads(settings_path.read_text())
        assert _LEGACY_AUTOCOMPACT_ENV_KEY not in refreshed["env"]
        out = capsys.readouterr().out
        assert "Updated" in out
        assert "retired mid-task compact override" in out
        # Operator-chosen value: untouched on a second pass.
        refreshed["env"][_LEGACY_AUTOCOMPACT_ENV_KEY] = "70"
        settings_path.write_text(
            json.dumps(refreshed, indent=2) + "\n", encoding="utf-8"
        )
        capsys.readouterr()
        rc = main(["--dir", str(tmp_path), "--refresh-skills"])
        assert rc == 0
        final = json.loads(settings_path.read_text())
        assert final["env"][_LEGACY_AUTOCOMPACT_ENV_KEY] == "70"

    def test_current_bundled_hash_not_in_prior_manifest(self):
        """Maintenance footgun guard: the CURRENTLY shipped SKILL.md must
        never appear in _PRIOR_SKILL_HASHES. The manifest records *prior*
        versions (so an existing team's unmodified copy refreshes); the
        documented rule is "append the OLD content's hash" on every edit.
        Appending the NEW hash instead is the classic slip -- it both stops
        propagation (old copies look hand-edited) and makes the set
        self-referential. This catches that at CI time."""
        import tigerharness.init as _init
        skills_root = Path(_init.__file__).parent / "_bundled_skills"
        for name, hashes in _init._PRIOR_SKILL_HASHES.items():
            skill_md = skills_root / name / "SKILL.md"
            assert skill_md.is_file(), f"manifest names missing skill {name!r}"
            current = _init._sha256_text(skill_md.read_text(encoding="utf-8"))
            assert current not in hashes, (
                f"{name}: the current bundled SKILL.md hash is listed in "
                f"_PRIOR_SKILL_HASHES -- you likely appended the NEW hash "
                f"instead of the OLD one."
            )

    def test_refresh_with_no_settings_file_is_fine(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        """A team dir with no .claude/settings.json (only skills) refreshes
        without crashing -- the settings top-up is simply skipped."""
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief", "--team", "tigers", "--yes",
        ])
        assert rc == 0
        (tmp_path / "tigers" / ".claude" / "settings.json").unlink()
        capsys.readouterr()
        rc = main(["--dir", str(tmp_path), "--refresh-skills"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Nothing to do" in out

    def test_explicit_team_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        """When >1 team exists, --team disambiguates."""
        for t in ("tigers", "shohoku"):
            rc = main([
                "--dir", str(tmp_path),
                "--persona", "chief", "--team", t, "--yes",
            ])
            assert rc == 0
        # Wipe a skill from one team so refresh has something to do.
        target = (
            tmp_path / "shohoku" / ".claude" / "skills"
            / "journal-new" / "SKILL.md"
        )
        target.unlink()
        target.parent.rmdir()
        capsys.readouterr()
        # --refresh-skills with no team picker -> error because
        # multiple teams exist.
        rc = main(["--dir", str(tmp_path), "--refresh-skills"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "multiple teams" in err
        # Disambiguate.
        rc = main([
            "--dir", str(tmp_path), "--refresh-skills",
            "--team", "shohoku",
        ])
        assert rc == 0
        assert target.is_file()

    def test_explicit_team_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief", "--team", "tigers", "--yes",
        ])
        assert rc == 0
        team_dir = tmp_path / "tigers"
        target = team_dir / ".claude" / "skills" / "journal-new" / "SKILL.md"
        target.unlink()
        target.parent.rmdir()
        capsys.readouterr()
        rc = main([
            "--refresh-skills", "--team-dir", str(team_dir),
        ])
        assert rc == 0
        assert target.is_file()

    def test_team_dir_not_a_team_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        notateam = tmp_path / "notateam"
        notateam.mkdir()
        rc = main([
            "--refresh-skills", "--team-dir", str(notateam),
        ])
        assert rc == 1
        assert "not a team" in capsys.readouterr().err

    def test_unknown_team_name_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief", "--team", "tigers", "--yes",
        ])
        assert rc == 0
        capsys.readouterr()
        rc = main([
            "--dir", str(tmp_path), "--refresh-skills",
            "--team", "nonexistent",
        ])
        assert rc == 1
        assert "no team named 'nonexistent'" in capsys.readouterr().err

    def test_no_teams_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        rc = main(["--dir", str(tmp_path), "--refresh-skills"])
        assert rc == 1
        assert "no teams found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Bundled drive-journal skill content + mirror invariant
# ---------------------------------------------------------------------------

class TestBundledDriveJournalSkill:
    """The bundled drive-journal skill is design copy (no behavior), but
    two of its contracts are load-bearing and easy to drop on a rewrite,
    so pin them."""

    def _bundled(self, name: str) -> Path:
        import tigerharness.init as _init
        return (
            Path(_init.__file__).parent / "_bundled_skills" / name / "SKILL.md"
        )

    def test_skill_teaches_per_persona_memory_gates(self):
        """The skill is read top-to-bottom and `claim`s in step 2 BEFORE it
        loads OPERATING.md in step 3 -- so the per-persona-memory gates have
        to live in the skill itself, or a Slack-driven drive claims without
        --driver and silently skips per-persona memory. (This regressed once
        when the skill was rewritten as a lazy-load checklist; pin it.)"""
        text = self._bundled("drive-journal").read_text(encoding="utf-8")
        assert "--driver" in text          # attribution at claim/release
        assert "--output" in text          # the note is the ticket (done gate)
        assert "journal step-done" in text  # graph-walk gate
        # --driver must appear at the claim step, not only at release --
        # claim happens before OPERATING.md is read.
        claim_idx = text.index("journal claim")
        assert "--driver" in text[claim_idx:claim_idx + 600]

    def test_step_done_scoped_to_graph_walk_not_compile(self):
        """`step-done` is the graph-walk gate; the compile sub-protocol uses
        `land-compile`. The skill must not imply step-done drives a compile
        round (it would mislead a driver in compile_pending=true)."""
        text = self._bundled("drive-journal").read_text(encoding="utf-8")
        assert "graph walk" in text.lower()
        assert "land-compile" in text


# Canonical invocation anchors for the tigerharness-basics skill. ONE
# home for these strings (the SKILL.md is authored against this list):
# each anchor is the exact backticked form the doc must use, so a CLI
# rename or a doc rewrite that drops a sub-command fails here instead
# of shipping silently. Plain `in` checks on short aliases ("j", "tm")
# would pass any text -- hence full backticked forms only.
_BASICS_SKILL_ANCHORS = [
    "`tigerharness init`",
    "`tigerharness dismiss`",
    "`tigerharness journal` (alias: `j`)",
    "`tigerharness tiger-memory` (alias: `tm`)",
    "`tigerharness slack-bridge` (alias: `sb`)",
    "`--refresh-skills`",
    "## Recruiting a new persona",
    "## Creating a workflow task",
]


class TestBundledBasicsSkill:
    """The tigerharness-basics skill teaches the CLI surface and team
    layout; pin its load-bearing claims so drift fails loudly."""

    def _text(self) -> str:
        import tigerharness.init as _init
        p = (
            Path(_init.__file__).parent / "_bundled_skills"
            / "tigerharness-basics" / "SKILL.md"
        )
        return p.read_text(encoding="utf-8")

    def test_anchored_invocation_forms(self):
        """Every dispatched sub-command (and the two walkthroughs the
        brief demands) appears in its canonical anchored form."""
        text = self._text()
        for anchor in _BASICS_SKILL_ANCHORS:
            assert anchor in text, f"missing anchor: {anchor!r}"

    def test_describes_skill_only_driving(self):
        """The skill must repeat the rail rule: journal driving is
        skill-only, never CLI-driven."""
        text = self._text()
        assert "drive-journal" in text
        assert "journal-new" in text
        assert "skill-only" in text

    def test_playbook_described_as_bare_name(self):
        """--playbook takes a BARE name resolving to
        <team-root>/workflow/<name>.md; the CLI rejects path-like
        values (journal/cli.py + scaffold.py's name regex). The skill
        must not re-teach the path variant (b2 defense finding)."""
        text = self._text()
        assert "name-or-path" not in text
        assert "workflow/<name>.md" in text

    def test_validate_personas_taught_correctly(self):
        """validate-personas requires a positional team and checks the
        COMPILE-role personas, not the whole roster -- taught wrong, a
        fresh team concludes its successful recruit failed (b2
        user-perspective finding: the bare invocation exits 2, and the
        'roster check' reading exits 1 on any fresh team)."""
        text = self._text()
        assert "validate-personas <Team>" in text
        assert "every roster entry" not in text
        # The workflow walkthrough must carry its precondition: the
        # compile roles (default Anzai/Akagi/Ayako) have to exist.
        assert "Anzai/Akagi/Ayako" in text
        assert "compile_personas" in text


# ---------------------------------------------------------------------------
# Integration: generated artifacts actually work
# ---------------------------------------------------------------------------

class TestGeneratedConfigLoads:
    """End-to-end check that what `init` writes is consumable -- the
    personas.yaml parses and its fields resolve to real files. (The
    legacy loader retired with ADR 0003; the yaml shape
    itself is the surviving contract.)"""

    def test_personas_yaml_loads_and_resolves_prompt(self, tmp_path: Path):
        import yaml as _yaml
        team_dir, _, _ = init(
            persona="chief", team="tigers",
            include_memory=False, include_slack=False,
            search_root=tmp_path,
        )
        data = _yaml.safe_load(
            (team_dir / "configs" / "personas.yaml").read_text()
        )
        personas = data["personas"]
        assert len(personas) == 1
        chief = personas[0]
        assert chief["name"] == "chief"
        # cwd: .. resolves to the team root (not configs/)
        assert (team_dir / "configs" / chief["cwd"]).resolve() == team_dir
        # prompt_file resolves under personas_dir
        prompt = (
            team_dir / "configs" / data["personas_dir"]
            / (chief["prompt_file"] + ".md")
        ).resolve()
        assert prompt.exists()
        assert "You are chief, part of team tigers" in prompt.read_text()

    def test_two_personas_both_load(self, tmp_path: Path):
        import yaml as _yaml
        team_dir, _, _ = init(
            persona="chief", team="tigers",
            include_memory=False, include_slack=False,
            search_root=tmp_path,
        )
        init(
            persona="scout", team="tigers",
            include_memory=False, include_slack=False,
            search_root=tmp_path,
        )
        data = _yaml.safe_load(
            (team_dir / "configs" / "personas.yaml").read_text()
        )
        names = [p["name"] for p in data["personas"]]
        assert names == ["chief", "scout"]
        for p in data["personas"]:
            f = (
                team_dir / "configs" / data["personas_dir"]
                / (p["prompt_file"] + ".md")
            ).resolve()
            assert f.exists()

class TestExpectedClaudeProjectPath:
    """``expected_claude_project_path`` always returns a path, even when
    the encoded dir doesn't exist yet (Claude Code hasn't run there)."""

    def test_returns_encoded_path_for_nonexistent_dir(self, tmp_path: Path):
        fake_home = tmp_path / "home"
        proj = tmp_path / "fresh-team"
        proj.mkdir()
        result = expected_claude_project_path(proj, home=fake_home)
        encoded = str(proj.resolve()).replace("/", "-")
        assert result == fake_home / ".claude" / "projects" / encoded
        assert not result.exists()


class TestPromptOptionalText:
    def test_empty_input_returns_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(builtins, "input", lambda _: "")
        assert _prompt_optional_text("anything") == ""

    def test_returns_stripped_input(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(builtins, "input", lambda _: "  hello  ")
        assert _prompt_optional_text("q") == "hello"


class TestInjectAllowedUserIds:
    """Rewrites the placeholder in a freshly-generated slack-bridge
    fragment with a real YAML list of user IDs."""

    _PLACEHOLDER = (
        "allowed_user_ids: []  "
        "# TODO: add at least one user ID before starting the bridge"
    )

    def test_writes_yaml_list_replacing_placeholder(self, tmp_path: Path):
        frag = tmp_path / "slack-bridge.yaml"
        frag.write_text(f"persona: ayako\n{self._PLACEHOLDER}\nstate_dir: /x\n")
        _inject_allowed_user_ids(frag, "U0ABC, U0DEF, U0GHI")
        content = frag.read_text()
        assert "allowed_user_ids:\n  - U0ABC\n  - U0DEF\n  - U0GHI" in content
        # Placeholder line gone.
        assert self._PLACEHOLDER not in content

    def test_empty_csv_is_no_op(self, tmp_path: Path):
        frag = tmp_path / "slack-bridge.yaml"
        original = f"persona: x\n{self._PLACEHOLDER}\n"
        frag.write_text(original)
        _inject_allowed_user_ids(frag, "   ,  ,")  # all blank
        assert frag.read_text() == original

    def test_no_placeholder_means_no_op(self, tmp_path: Path):
        """If the fragment already has a populated allowlist, don't
        touch it -- avoids stomping on user edits."""
        frag = tmp_path / "slack-bridge.yaml"
        original = "allowed_user_ids:\n  - U0EXISTING\n"
        frag.write_text(original)
        _inject_allowed_user_ids(frag, "U0NEW")
        assert frag.read_text() == original

    def test_strips_whitespace_around_each_id(self, tmp_path: Path):
        frag = tmp_path / "slack-bridge.yaml"
        frag.write_text(f"persona: x\n{self._PLACEHOLDER}\n")
        _inject_allowed_user_ids(frag, "  U0A  ,U0B  ,  U0C")
        content = frag.read_text()
        assert "  - U0A\n" in content
        assert "  - U0B\n" in content
        assert "  - U0C\n" in content


class TestAutoInitTigerMemory:
    """``_auto_init_tiger_memory`` spawns a subprocess to run
    ``tiger-memory init`` for a freshly-generated config. Failures are
    non-fatal (warn + continue) so a missing CLI doesn't break the
    main init flow."""

    def test_runs_subprocess_with_correct_args(self, tmp_path: Path):
        from unittest.mock import patch as _patch, MagicMock
        cfg = tmp_path / "mem.yaml"
        cfg.write_text("agent: {name: x, role: x}\n")
        with _patch(
            "tigerharness.init.subprocess.run",
            return_value=MagicMock(returncode=0),
        ) as run_mock:
            _auto_init_tiger_memory(cfg)
        run_mock.assert_called_once()
        cmd = run_mock.call_args[0][0]
        assert "tigerharness" in cmd
        assert "tiger-memory" in cmd
        assert "init" in cmd
        assert str(cfg) in cmd

    def test_subprocess_failure_logs_warning_doesnt_raise(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        from unittest.mock import patch as _patch
        import subprocess
        cfg = tmp_path / "mem.yaml"
        cfg.write_text("x")
        with _patch(
            "tigerharness.init.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, cmd=["x"]),
        ):
            _auto_init_tiger_memory(cfg)  # should not raise
        err = capsys.readouterr().err
        assert "warning:" in err
        assert "auto-init" in err

    def test_timeout_is_caught(self, tmp_path: Path, capsys: pytest.CaptureFixture):
        from unittest.mock import patch as _patch
        import subprocess
        cfg = tmp_path / "mem.yaml"
        cfg.write_text("x")
        with _patch(
            "tigerharness.init.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=30),
        ):
            _auto_init_tiger_memory(cfg)
        assert "warning:" in capsys.readouterr().err

    def test_oserror_is_caught(self, tmp_path: Path, capsys: pytest.CaptureFixture):
        """e.g. command not found"""
        from unittest.mock import patch as _patch
        cfg = tmp_path / "mem.yaml"
        cfg.write_text("x")
        with _patch(
            "tigerharness.init.subprocess.run",
            side_effect=OSError("No such file"),
        ):
            _auto_init_tiger_memory(cfg)
        assert "warning:" in capsys.readouterr().err


class TestMainPromptsForUserIds:
    """Interactive flow: when init creates a slack-bridge fragment, main
    prompts for user IDs. ``--yes`` skips the prompt; empty input
    leaves the placeholder; populated input rewrites the file."""

    def test_yes_skips_prompt(self, tmp_path: Path):
        """--yes mode never prompts -- fragment stays with empty list."""
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "ayako",
            "--team", "shohoku",
            "--yes",
        ])
        assert rc == 0
        frag = tmp_path / "shohoku" / "configs" / "slack-bridge.yaml"
        assert "allowed_user_ids: []" in frag.read_text()

    def test_interactive_prompt_populates_fragment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Without --yes, the prompt fires and populates the fragment.
        Pre-create the index so multi-team auto-resolves to True when
        slack=yes (no separate multi-team prompt fires)."""
        (tmp_path / "slack-bridge.yaml").write_text("")
        responses = iter([
            "y",                     # slack .env? yes -> fragment will be made
            "n",                     # memory? no
            "U0ABC,U0DEF",           # allowed_user_ids prompt
        ])
        monkeypatch.setattr(builtins, "input", lambda _: next(responses))
        # Bypass tiger-memory init subprocess; we said no memory anyway.
        with patch("tigerharness.init.subprocess.run"):
            rc = main([
                "--dir", str(tmp_path),
                "--persona", "ayako",
                "--team", "shohoku",
            ])
        assert rc == 0
        frag = tmp_path / "shohoku" / "configs" / "slack-bridge.yaml"
        content = frag.read_text()
        assert "  - U0ABC\n" in content
        assert "  - U0DEF\n" in content
        assert "allowed_user_ids: []" not in content

    def test_interactive_prompt_empty_leaves_placeholder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        (tmp_path / "slack-bridge.yaml").write_text("")
        # Same order as above: slack -> memory -> user-IDs.
        responses = iter(["y", "n", ""])
        monkeypatch.setattr(builtins, "input", lambda _: next(responses))
        with patch("tigerharness.init.subprocess.run"):
            rc = main([
                "--dir", str(tmp_path),
                "--persona", "ayako",
                "--team", "shohoku",
            ])
        assert rc == 0
        frag = tmp_path / "shohoku" / "configs" / "slack-bridge.yaml"
        assert "allowed_user_ids: []" in frag.read_text()


class TestMultiTeamEnvTemplate:
    """In multi-team mode, the generated .env has tokens only -- the
    allowlist lives in the yaml fragment (single source of truth)."""

    def test_multi_team_env_omits_allowed_user_ids(self, tmp_path: Path):
        (tmp_path / "slack-bridge.yaml").write_text("")
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "ayako",
            "--team", "shohoku",
            "--yes",
        ])
        assert rc == 0
        env = (tmp_path / "shohoku" / "configs" / ".env").read_text()
        assert "ALLOWED_SLACK_USER_IDS" not in env
        assert "SLACK_APP_TOKEN" in env
        assert "SLACK_BOT_TOKEN" in env

    def test_legacy_env_keeps_allowed_user_ids(self, tmp_path: Path):
        """--no-multi-team uses the legacy template, which still bundles
        the allowlist for backwards compat with single-tenant users."""
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "ayako",
            "--team", "shohoku",
            "--yes", "--no-multi-team",
        ])
        assert rc == 0
        env = (tmp_path / "shohoku" / "configs" / ".env").read_text()
        assert "ALLOWED_SLACK_USER_IDS" in env

    def test_multi_team_explicit_flag_works_without_yes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """`--multi-team` works without `--yes`; covers the elif branch
        in main()."""
        responses = iter(["n", "n", ""])
        monkeypatch.setattr(builtins, "input", lambda _: next(responses))
        with patch("tigerharness.init.subprocess.run"):
            rc = main([
                "--dir", str(tmp_path),
                "--persona", "ayako",
                "--team", "shohoku",
                "--multi-team",
            ])
        assert rc == 0
        assert (tmp_path / "slack-bridge.yaml").exists()

    def test_yes_with_no_slack_skips_multi_team(self, tmp_path: Path):
        """`--yes --no-slack`: --yes accepts defaults BUT only when
        slack is on. With slack opted out, multi-team is gated off
        (no index, no fragment)."""
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "ayako",
            "--team", "shohoku",
            "--yes", "--no-slack",
        ])
        assert rc == 0
        assert not (tmp_path / "slack-bridge.yaml").exists()
        assert not (tmp_path / "shohoku" / "configs" / "slack-bridge.yaml").exists()

    def test_yes_with_explicit_multi_team_overrides_no_slack(self, tmp_path: Path):
        """`--yes --no-slack --multi-team`: the explicit --multi-team
        flag overrides the slack-gating. Advanced users can pre-create
        the index before having Slack apps ready."""
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "ayako",
            "--team", "shohoku",
            "--yes", "--no-slack", "--multi-team",
        ])
        assert rc == 0
        # Index gets created because --multi-team is explicit.
        assert (tmp_path / "slack-bridge.yaml").exists()


class TestInitMultiTeamGating:
    """``init()`` only prompts for multi-team when Slack is on. With
    Slack opted out, multi-team is deterministically False -- no prompt."""

    def test_no_slack_means_no_prompt_for_multi_team(self, tmp_path: Path):
        """Direct init() call, scripted, include_slack=False ->
        include_multi_team auto-resolves to False without prompting."""
        team_dir, persona, created = init(
            persona="chief", team="tigers",
            include_memory=False,
            include_slack=False,
            # include_multi_team intentionally omitted (None)
            search_root=tmp_path,
        )
        # No index because slack-gated decision -> False
        assert not (tmp_path / "slack-bridge.yaml").exists()
        # No per-team fragment either.
        assert not (team_dir / "configs" / "slack-bridge.yaml").exists()

    def test_slack_on_index_exists_means_no_prompt(self, tmp_path: Path):
        """If the index already exists, multi-team is implicit -- no
        prompt even with include_multi_team=None and include_slack=True."""
        (tmp_path / "slack-bridge.yaml").write_text("")
        with patch("tigerharness.init.subprocess.run"):
            team_dir, _, _ = init(
                persona="chief", team="tigers",
                include_memory=False,
                include_slack=True,
                # include_multi_team intentionally omitted
                search_root=tmp_path,
            )
        # Index still there + fragment got auto-registered.
        assert (tmp_path / "slack-bridge.yaml").exists()
        assert (team_dir / "configs" / "slack-bridge.yaml").exists()

    def test_explicit_multi_team_overrides_no_slack(self, tmp_path: Path):
        """Caller passes include_multi_team=True with include_slack=False.
        The explicit override wins -- the index gets created even
        without slack."""
        init(
            persona="chief", team="tigers",
            include_memory=False,
            include_slack=False,
            include_multi_team=True,
            search_root=tmp_path,
        )
        assert (tmp_path / "slack-bridge.yaml").exists()

    def test_no_multi_team_with_existing_index_does_not_register_lane(
        self, tmp_path: Path,
    ):
        """Bug fix: if an index exists from a prior run, passing
        --no-multi-team for THIS team must NOT register it as a lane.
        Otherwise we get a half-state: single-tenant .env + memory
        config, but the team appears in the multi-bridge index."""
        # Pre-create the index (simulating an earlier multi-team setup).
        (tmp_path / "slack-bridge.yaml").write_text(
            "lanes:\n  - existing-team\n"
        )
        # Now add a team WITHOUT multi-team scaffolding.
        with patch("tigerharness.init.subprocess.run"):
            team_dir, _, _ = init(
                persona="solo", team="loner",
                include_memory=True,
                include_slack=True,
                include_multi_team=False,
                search_root=tmp_path,
            )
        # No per-team fragment.
        assert not (team_dir / "configs" / "slack-bridge.yaml").exists()
        # Team NOT appended to the index.
        idx = (tmp_path / "slack-bridge.yaml").read_text()
        assert "  - loner\n" not in idx
        # The pre-existing entry survives untouched.
        assert "  - existing-team\n" in idx

    def test_explicit_multi_team_without_slack_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        """--multi-team + --no-slack is unusual but allowed. We print
        a warning to stderr so a typo doesn't leave the user confused."""
        init(
            persona="chief", team="tigers",
            include_memory=False,
            include_slack=False,
            include_multi_team=True,
            search_root=tmp_path,
        )
        err = capsys.readouterr().err
        assert "warning:" in err
        assert "Slack" in err

    def test_index_exists_writes_memory_config_with_persona_filter(
        self, tmp_path: Path,
    ):
        """Test gap from review: when the index pre-exists AND
        include_slack=True AND include_multi_team=None (resolves to
        True via auto-detect), the memory config gets the persona
        filter and the slack_thread source. This is the path most
        real users will hit when adding a 2nd persona to an
        existing multi-team setup."""
        # Pre-create the index.
        (tmp_path / "slack-bridge.yaml").write_text("")
        with patch("tigerharness.init.subprocess.run"):
            team_dir, _, _ = init(
                persona="ayako", team="shohoku",
                include_memory=True,
                include_slack=True,
                # include_multi_team omitted -> auto-resolved by init
                search_root=tmp_path,
            )
        mem = (team_dir / "memories" / "ayako" / "tiger-memory.config.yaml").read_text()
        assert "persona: ayako" in mem
        assert "kind: slack_thread" in mem
        assert "threads_json:" in mem


    def test_interrupted_user_ids_prompt_leaves_placeholder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """If the user Ctrl-C's the user-IDs prompt, treat it as 'skip
        for now' rather than aborting the whole init."""
        # Pre-create the index so we go straight to multi-team mode.
        (tmp_path / "slack-bridge.yaml").write_text("")
        responses = [
            "y",     # slack .env prompt -> yes (so fragment gets made)
            "n",     # memory prompt -> skip
        ]
        idx = iter(responses)

        def _input(_prompt):
            try:
                return next(idx)
            except StopIteration:
                # We've exhausted scripted responses -- the next call
                # is the user-IDs prompt. Simulate Ctrl-C.
                raise KeyboardInterrupt

        monkeypatch.setattr(builtins, "input", _input)
        with patch("tigerharness.init.subprocess.run"):
            rc = main([
                "--dir", str(tmp_path),
                "--persona", "ayako",
                "--team", "shohoku",
            ])
        # init succeeded; fragment still has the placeholder.
        assert rc == 0
        frag = tmp_path / "shohoku" / "configs" / "slack-bridge.yaml"
        assert "allowed_user_ids: []" in frag.read_text()


# ---------------------------------------------------------------------------
# Space-containing names (init side)
# ---------------------------------------------------------------------------

class TestSpacedNames:
    """End-to-end coverage for space-containing persona/team names.

    The grammar accepts space-separated words ([A-Za-z0-9][A-Za-z0-9_-]*
    joined by single spaces); these tests pin the user-visible surfaces:
    the interactive prompt (the originally-reported failure), folder
    scaffolding, the personas.yaml round-trip through a real YAML
    loader, shell-snippet quoting, and --team-dir basename derivation.
    """

    def test_interactive_prompt_accepts_spaced_persona(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        # The reported failure surface: the persona arrives via the
        # interactive prompt, not a flag.
        from tigerharness import init as init_mod
        monkeypatch.setattr(
            init_mod, "_prompt_text", lambda *a, **k: "Chuan Ying"
        )
        rc = main([
            "--dir", str(tmp_path),
            "--team", "tigers",
            "--no-memory", "--no-slack", "--no-multi-team",
        ])
        assert rc == 0
        assert (
            tmp_path / "tigers" / "personas" / "Chuan Ying" / "prompt.md"
        ).exists()
        import yaml as _yaml
        cfg = _yaml.safe_load(
            (tmp_path / "tigers" / "configs" / "personas.yaml").read_text()
        )
        # (default_persona seeding is a separate, pre-existing flow --
        # create_team scaffolds the personas.yaml header before the
        # entry is appended -- so it is not asserted here.)
        entry = cfg["personas"][0]
        assert entry["name"] == "Chuan Ying"
        assert entry["prompt_file"] == "Chuan Ying/prompt"
        # Copy-paste shell snippets must quote the spaced name.
        out = capsys.readouterr().out
        assert "--persona 'Chuan Ying'" in out

    def test_spaced_team_scaffolds_and_quotes_paths(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "ayako",
            "--team", "Tiger Team",
            "--no-memory", "--no-slack", "--no-multi-team", "--yes",
        ])
        assert rc == 0
        assert (
            tmp_path / "Tiger Team" / "personas" / "ayako" / "prompt.md"
        ).exists()
        out = capsys.readouterr().out
        # The export line embeds a path containing the spaced team
        # name; it must arrive shell-quoted.
        assert "export TIGERHARNESS_PERSONAS_CONFIG='Tiger Team/" in out

    def test_team_dir_basename_with_space_accepted(self, tmp_path: Path):
        # --team-dir derives the team name from an existing folder's
        # basename (init.py team derivation); a single internal space
        # must now pass validation.
        team_dir = tmp_path / "Tiger Team"
        team_dir.mkdir()
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "ayako",
            "--team-dir", str(team_dir),
            "--no-memory", "--no-slack", "--no-multi-team", "--yes",
        ])
        assert rc == 0
        assert (team_dir / "personas" / "ayako" / "prompt.md").exists()

    def test_team_dir_basename_double_space_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        team_dir = tmp_path / "Bad  Team"
        team_dir.mkdir()
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "ayako",
            "--team-dir", str(team_dir),
            "--no-memory", "--no-slack", "--no-multi-team", "--yes",
        ])
        assert rc != 0
        assert "invalid team name" in capsys.readouterr().err

    def test_golden_unspaced_output_unchanged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        """Regression lock: for space-free names the printed snippets
        are byte-identical to the pre-spaces behavior -- shlex.quote
        leaves shell-safe strings unquoted, so legacy users see no
        change. Golden lines captured from the pre-change output.
        """
        rc = main([
            "--dir", str(tmp_path),
            "--persona", "chief",
            "--team", "tigers",
            "--no-memory", "--no-slack", "--no-multi-team", "--yes",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert (
            "export TIGERHARNESS_PERSONAS_CONFIG="
            "tigers/configs/personas.yaml\n"
        ) in out
        assert "--persona chief --prd <brief.md>" in out
