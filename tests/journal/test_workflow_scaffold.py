"""Tests for the Phase 1.5 workflow-mode scaffolder (``new_workflow_task``)
plus its persona-validation helpers and team-root resolver."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tigerharness.journal.models import CompilePhase, State, Status
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.scaffold import (
    COMPILE_PERSONAS,
    JournalScaffoldError,
    MissingPersonaError,
    _required_workflow_personas,
    extract_persona_refs_from_playbook,
    new_workflow_task,
    read_team_roster,
    resolve_team_root,
    validate_personas,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_team(
    root: Path,
    *,
    personas: list[str] | None = None,
    name: str = "Shohoku",
) -> Path:
    """Build a fake team directory with the given persona registry and
    prompt.md files. Returns the team root path."""
    team = root / "teams" / name
    (team / "configs").mkdir(parents=True)
    if personas is None:
        personas = list(COMPILE_PERSONAS) + ["Mitsui"]
    lines = ["personas:\n"]
    for p in personas:
        lines.append(f"  - name: {p}\n")
    (team / "configs" / "personas.yaml").write_text("".join(lines))
    for p in personas:
        pdir = team / "personas" / p
        pdir.mkdir(parents=True)
        (pdir / "prompt.md").write_text(f"You are {p}.\n")
    return team


# ---------------------------------------------------------------------------
# resolve_team_root
# ---------------------------------------------------------------------------

class TestResolveTeamRoot:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIGERHARNESS_TEAMS_DIR", str(tmp_path))
        assert resolve_team_root("Shohoku") == tmp_path / "Shohoku"

    def test_cwd_is_team_root(self, monkeypatch, tmp_path):
        """If cwd has configs/personas.yaml, cwd IS the team root."""
        monkeypatch.delenv("TIGERHARNESS_TEAMS_DIR", raising=False)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "personas.yaml").write_text("personas: []\n")
        monkeypatch.chdir(tmp_path)
        assert resolve_team_root("Shohoku") == tmp_path

    def test_falls_back_to_cwd_teams(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TIGERHARNESS_TEAMS_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        assert resolve_team_root("Shohoku") == tmp_path / "teams" / "Shohoku"

    def test_blank_env_falls_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIGERHARNESS_TEAMS_DIR", "  ")
        monkeypatch.chdir(tmp_path)
        assert resolve_team_root("Shohoku") == tmp_path / "teams" / "Shohoku"


# ---------------------------------------------------------------------------
# extract_persona_refs_from_playbook
# ---------------------------------------------------------------------------

class TestExtractPersonaRefs:
    def test_picks_up_capitalised_words(self):
        text = "Anzai plans. Mitsui implements."
        refs = extract_persona_refs_from_playbook(text)
        assert "Anzai" in refs
        assert "Mitsui" in refs

    def test_ignores_short_words(self):
        """``[A-Z][a-zA-Z]{2,}`` requires at least 3 chars; 'A' / 'AI'
        are too short."""
        text = "A short AI thing."
        assert extract_persona_refs_from_playbook(text) == {"AI"} or \
            "A" not in extract_persona_refs_from_playbook(text)

    def test_ignores_lowercase(self):
        assert extract_persona_refs_from_playbook("mitsui") == set()


# ---------------------------------------------------------------------------
# read_team_roster
# ---------------------------------------------------------------------------

class TestReadTeamRoster:
    def test_reads_yaml(self, tmp_path):
        team = _make_team(tmp_path, personas=["Anzai", "Akagi", "Ayako"])
        assert read_team_roster(team) == {"Anzai", "Akagi", "Ayako"}

    def test_missing_yaml_returns_empty(self, tmp_path):
        # team root with no configs/personas.yaml
        team = tmp_path / "teams" / "Bad"
        team.mkdir(parents=True)
        assert read_team_roster(team) == set()

    def test_malformed_yaml_returns_empty(self, tmp_path):
        team = tmp_path / "teams" / "Bad"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "not_a_dict: just_a_string\n"
        )
        # That parses to {"not_a_dict": "just_a_string"} which has no
        # personas key.
        assert read_team_roster(team) == set()

    def test_top_level_not_dict_returns_empty(self, tmp_path):
        team = tmp_path / "teams" / "Bad"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "- just\n- a\n- list\n"
        )
        assert read_team_roster(team) == set()

    def test_entries_without_name_are_skipped(self, tmp_path):
        team = tmp_path / "teams" / "Bad"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n  - description: just a description\n"
            "  - name: Akagi\n"
        )
        assert read_team_roster(team) == {"Akagi"}

    def test_non_dict_entries_are_skipped(self, tmp_path):
        team = tmp_path / "teams" / "Bad"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n  - just-a-string\n  - name: Akagi\n"
        )
        assert read_team_roster(team) == {"Akagi"}

    def test_non_string_name_skipped(self, tmp_path):
        team = tmp_path / "teams" / "Bad"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n  - name: 42\n  - name: Akagi\n"
        )
        assert read_team_roster(team) == {"Akagi"}

    def test_blank_name_skipped(self, tmp_path):
        team = tmp_path / "teams" / "Bad"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n  - name: \"   \"\n  - name: Akagi\n"
        )
        assert read_team_roster(team) == {"Akagi"}


# ---------------------------------------------------------------------------
# _required_workflow_personas
# ---------------------------------------------------------------------------

class TestRequiredWorkflowPersonas:
    def test_includes_compile_personas_always(self, tmp_path):
        team = _make_team(tmp_path, personas=["Anzai", "Akagi", "Ayako"])
        req = _required_workflow_personas("nothing capitalised here", team)
        assert req == set(COMPILE_PERSONAS)

    def test_union_with_playbook_refs_that_are_in_roster(self, tmp_path):
        team = _make_team(
            tmp_path,
            personas=["Anzai", "Akagi", "Ayako", "Mitsui"],
        )
        req = _required_workflow_personas("Mitsui implements.", team)
        assert req == set(COMPILE_PERSONAS) | {"Mitsui"}

    def test_drops_playbook_refs_not_in_roster(self, tmp_path):
        """A capitalised word that isn't a registered persona is
        English prose, not a typo."""
        team = _make_team(tmp_path, personas=list(COMPILE_PERSONAS))
        req = _required_workflow_personas(
            "Default behavior; QAs are critical.", team,
        )
        # 'Default' and 'QAs' are NOT in the team roster; dropped.
        assert req == set(COMPILE_PERSONAS)


# ---------------------------------------------------------------------------
# validate_personas
# ---------------------------------------------------------------------------

class TestValidatePersonas:
    def test_all_present(self, tmp_path):
        team = _make_team(tmp_path)
        assert validate_personas(team, set(COMPILE_PERSONAS)) == []

    def test_one_missing(self, tmp_path):
        team = _make_team(tmp_path)
        (team / "personas" / "Akagi" / "prompt.md").unlink()
        assert validate_personas(team, set(COMPILE_PERSONAS)) == ["Akagi"]

    def test_empty_prompt_is_missing(self, tmp_path):
        team = _make_team(tmp_path)
        (team / "personas" / "Ayako" / "prompt.md").write_text("")
        assert validate_personas(team, set(COMPILE_PERSONAS)) == ["Ayako"]

    def test_oserror_treated_as_missing(self, monkeypatch, tmp_path):
        team = _make_team(tmp_path)
        original_is_file = Path.is_file

        def _boom(self):
            if "Anzai" in str(self):
                raise OSError("simulated")
            return original_is_file(self)
        monkeypatch.setattr(Path, "is_file", _boom)
        missing = validate_personas(team, set(COMPILE_PERSONAS))
        assert "Anzai" in missing


# ---------------------------------------------------------------------------
# new_workflow_task end-to-end
# ---------------------------------------------------------------------------

class TestNewWorkflowTask:
    def _paths(self, tmp_path: Path) -> JournalPaths:
        return JournalPaths(root=tmp_path / "journal")

    def test_happy_path(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="# Cache eviction\nAdd LRU.\n",
            playbook_text="# Default\nAnzai plans. Mitsui implements.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
            captain="Akagi",
        )
        assert result.status.kind == "workflow"
        assert result.status.state is State.PENDING
        assert result.status.compile_pending is True
        assert result.status.compile_phase is CompilePhase.PENDING
        assert result.status.persona == "Akagi"
        # Title comes from the brief's first H1.
        assert result.status.title == "Cache eviction"
        # Files on disk.
        assert (result.task_dir / "task_brief.md").is_file()
        assert (result.task_dir / "playbook_snapshot.md").is_file()
        assert (result.task_dir / "progress.md").is_file()
        assert (result.task_dir / "artifacts").is_dir()
        assert (result.task_dir / "status.json").is_file()
        # task.md is NOT written for workflow mode (the brief lives in
        # task_brief.md instead).
        assert not (result.task_dir / "task.md").exists()
        # OPERATING.md installed at journal root.
        assert paths.operating_md.is_file()

    def test_captain_optional(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="# T\nb\n",
            playbook_text="Anzai plans.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
            captain=None,
        )
        assert result.status.persona is None

    def test_title_arg_wins_over_brief_h1(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="# From brief\nb\n",
            playbook_text="Anzai.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
            title="Explicit title",
        )
        assert result.status.title == "Explicit title"

    def test_falls_back_to_workflow_when_no_h1(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="just body\n",
            playbook_text="Anzai.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
        )
        assert result.status.title == "workflow"

    def test_slug_overrider(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="# Long title that gets slugified\nb\n",
            playbook_text="Anzai.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
            slug="short",
        )
        assert "-short-" in result.task_id

    def test_empty_brief_rejected(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        with pytest.raises(JournalScaffoldError) as exc:
            new_workflow_task(
                brief_text="   ",
                playbook_text="Anzai.\n",
                playbook_name="default",
                team_root=team,
                paths=paths,
            )
        assert "brief" in str(exc.value).lower()

    def test_empty_playbook_rejected(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        with pytest.raises(JournalScaffoldError) as exc:
            new_workflow_task(
                brief_text="# T\nb\n",
                playbook_text="   ",
                playbook_name="default",
                team_root=team,
                paths=paths,
            )
        assert "playbook" in str(exc.value).lower()

    @pytest.mark.parametrize("bad", [
        "../escape",
        "/abs",
        "foo/bar",
        "",
        ".hidden",
        "with space",
    ])
    def test_bad_playbook_name_rejected(self, tmp_path, bad):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        with pytest.raises(JournalScaffoldError):
            new_workflow_task(
                brief_text="# T\nb\n",
                playbook_text="Anzai.\n",
                playbook_name=bad,
                team_root=team,
                paths=paths,
            )

    def test_missing_compile_persona_raises(self, tmp_path):
        team = _make_team(tmp_path, personas=["Anzai", "Ayako"])  # Akagi missing
        paths = self._paths(tmp_path)
        with pytest.raises(MissingPersonaError) as exc:
            new_workflow_task(
                brief_text="# T\nb\n",
                playbook_text="Anzai.\n",
                playbook_name="default",
                team_root=team,
                paths=paths,
            )
        assert "Akagi" in exc.value.missing
        # No journal artifact should have been written.
        assert paths.list_active_ids() == []

    def test_missing_playbook_persona_raises(self, tmp_path):
        """A persona that's in the roster but doesn't have prompt.md on
        disk is caught."""
        team = _make_team(
            tmp_path,
            personas=list(COMPILE_PERSONAS) + ["Mitsui"],
        )
        # Mitsui is in personas.yaml AND in the playbook, but the
        # prompt.md is empty.
        (team / "personas" / "Mitsui" / "prompt.md").write_text("")
        paths = self._paths(tmp_path)
        with pytest.raises(MissingPersonaError) as exc:
            new_workflow_task(
                brief_text="# T\nb\n",
                playbook_text="Anzai plans. Mitsui implements.\n",
                playbook_name="default",
                team_root=team,
                paths=paths,
            )
        assert "Mitsui" in exc.value.missing

    def test_write_order_status_json_last(self, tmp_path):
        """The Phase 1 write-order invariant: status.json must be the
        LAST file written so a SIGKILL between writes never leaves a
        half-built task visible. We assert by checking that on a
        successful return, all other artifacts exist on disk too."""
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="# T\nb\n",
            playbook_text="Anzai.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
        )
        # task_brief, playbook_snapshot, progress must all exist before
        # status.json -- because status.json was written last.
        # We can't observe ordering directly, but we can confirm all
        # required predecessors exist on disk when status.json does.
        for fname in ("task_brief.md", "playbook_snapshot.md",
                      "progress.md"):
            assert (result.task_dir / fname).is_file()
        assert paths.status_json(result.task_id).is_file()

    def test_status_json_round_trips_after_scaffold(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="# T\nb\n",
            playbook_text="Anzai.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
            captain="Akagi",
        )
        on_disk = Status.from_json(
            paths.status_json(result.task_id).read_text()
        )
        assert on_disk == result.status

    def test_id_mint_failure_re_raises_as_scaffold_error(
        self, tmp_path, monkeypatch,
    ):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        from tigerharness.journal import ids as id_mod
        monkeypatch.setattr(
            id_mod.secrets, "token_hex", lambda n: "deadbeef",
        )
        # Pre-seed both attempted ids in done/ so the mint fails.
        import datetime as dt
        date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        seed = paths.done / f"{date}-collision-deadbeef"
        seed.mkdir(parents=True)
        (seed / "status.json").write_text("{}")
        with pytest.raises(JournalScaffoldError):
            new_workflow_task(
                brief_text="# Collision\nb\n",
                playbook_text="Anzai.\n",
                playbook_name="default",
                team_root=team,
                paths=paths,
                slug="collision",
            )

    def test_zero_max_sessions_rejected_via_model(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        with pytest.raises(JournalScaffoldError) as exc:
            new_workflow_task(
                brief_text="# T\nb\n",
                playbook_text="Anzai.\n",
                playbook_name="default",
                team_root=team,
                paths=paths,
                max_sessions=0,
            )
        # The underlying JournalModelError is re-raised as
        # JournalScaffoldError.
        assert "max_sessions" in str(exc.value)
