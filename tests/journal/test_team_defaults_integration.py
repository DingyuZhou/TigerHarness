"""Integration test for the persona-alias + default-persona +
default-captain features against a realistic, real-shape fixture
team (modelled on Shohoku). The fixture lives at
``tests/journal/fixtures/shohoku_like/`` and mirrors the production
shape -- multiple personas with aliases, a top-level
``default_persona``, a ``workflow.yaml`` mapping ``ayako`` to an
alias, and a playbook with a multi-section HTML-comment YAML block.

The unit tests in ``test_workflow_scaffold.py`` exercise each helper
in isolation with synthetic yaml strings. This test exercises the
HELPERS AGAINST A REAL FILE TREE so a future edit to either the
helpers OR the fixture (e.g. matching a change made to the real
team's yaml shape) trips the assertion before it reaches production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tigerharness.journal.scaffold import (
    canonicalize_persona,
    extract_playbook_meta,
    read_team_alias_map,
    read_team_roster,
    resolve_compile_personas,
    resolve_default_persona,
    resolve_playbook_default_captain,
    validate_personas,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "shohoku_like"


@pytest.fixture
def team_root() -> Path:
    """The on-disk fixture team root."""
    return _FIXTURE


class TestRealishTeamConfig:
    def test_roster_reads_canonical_names(self, team_root: Path):
        roster = read_team_roster(team_root)
        assert {"Ayako", "Anzai", "Akagi", "Mitsui", "Kogure"} <= roster

    def test_alias_map_includes_multilingual_aliases(self, team_root: Path):
        m = read_team_alias_map(team_root)
        # Canonical self-maps.
        assert m["ayako"] == "Ayako"
        assert m["kogure"] == "Kogure"
        # Multi-script aliases.
        assert m["caizi"] == "Ayako"
        assert m["mumu"] == "Kogure"
        # Separator normalisation: "Anxi Jiaolian" -> "anxi-jiaolian".
        assert m["anxi-jiaolian"] == "Anzai"

    def test_default_persona_resolves_to_canonical(self, team_root: Path):
        assert resolve_default_persona(team_root) == "Ayako"

    def test_canonicalize_through_alias(self, team_root: Path):
        # Mumu -> Kogure (the case the Operator runs in production).
        assert canonicalize_persona(team_root, "Mumu") == "Kogure"
        # Case + separator collapse.
        assert canonicalize_persona(team_root, "  mumu  ") == "Kogure"
        assert canonicalize_persona(team_root, "Kogure-senpai") == "Kogure"

    def test_compile_personas_resolves_workflow_yaml_alias(
        self, team_root: Path,
    ):
        """workflow.yaml has `ayako: Mumu`. The resolver is the single
        canonicalization home: it returns canonical Kogure, so every
        downstream consumer (compile-context prints, land-compile
        worklog stamping, validate_personas) sees the name the memory
        store is keyed by -- the wrong-name compile-stamp bug's pin."""
        mapping = resolve_compile_personas(team_root)
        assert mapping == {
            "drafter": "Anzai", "akagi": "Akagi", "ayako": "Kogure",
        }

    def test_validate_personas_resolves_compile_mapping_via_aliases(
        self, team_root: Path,
    ):
        """End-to-end: the compile mapping uses an alias (Mumu);
        validate_personas canonicalises (Mumu -> Kogure) and finds
        the prompt.md on disk. No false 'missing' report."""
        mapping = resolve_compile_personas(team_root)
        missing = validate_personas(team_root, set(mapping.values()))
        assert missing == []

    def test_playbook_meta_extracts_default_captain_and_drops_runner_config(
        self, team_root: Path,
    ):
        """The fixture playbook's HTML-comment block carries BOTH a
        journal-side key (default_captain) and a runner-side block
        (workflow_config). The whitelist keeps the captain and drops
        workflow_config -- pins the journal/runner boundary."""
        text = (team_root / "workflow" / "default.md").read_text(
            encoding="utf-8",
        )
        meta = extract_playbook_meta(text)
        assert meta == {"default_captain": "Mitsui"}

    def test_playbook_default_captain_canonicalises(self, team_root: Path):
        text = (team_root / "workflow" / "default.md").read_text(
            encoding="utf-8",
        )
        assert resolve_playbook_default_captain(text, team_root) == "Mitsui"

    def test_compile_persona_collision_with_canonical_name_safe(
        self, tmp_path: Path,
    ):
        """A regression test using a fixture-equivalent setup but with
        an alias colliding with another persona's canonical name.
        Canonical names ALWAYS win over alias entries regardless of
        file order -- the lookup of the canonical name stays correct."""
        # Build a tiny team with Akagi (canonical) and Mitsui declaring
        # an alias "Akagi" that would (without the fix) shadow Akagi's
        # canonical lookup.
        team = tmp_path / "T"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Mitsui\n"
            "    aliases: [Akagi]\n"  # alias collides with another's canonical
            "  - name: Akagi\n"
        )
        assert canonicalize_persona(team, "Akagi") == "Akagi"
