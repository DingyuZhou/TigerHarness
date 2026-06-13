"""CI guard so the bundled-skill refresh manifest never rots again.

Background: `tigerharness init --refresh-skills` only auto-updates an
on-disk skill whose hash is registered in ``init._PRIOR_SKILL_HASHES``.
When a bundled SKILL.md is edited and its OLD hash is NOT appended there,
existing teams silently stay on the stale skill (the 2026-06-13
journal-new-on-Shohoku incident). This guard makes that omission a CI
failure: ``init._CURRENT_SKILL_HASHES`` is a committed manifest of each
skill's current hash, and the test below fails if a bundled SKILL.md no
longer matches it — telling the author to (i) move the old manifest hash
into ``_PRIOR_SKILL_HASHES`` and (ii) roll the manifest. No git
dependency, so it is safe under shallow CI clones.
"""
from __future__ import annotations

from pathlib import Path

import tigerharness.init as _init
from tigerharness.init import (
    _CURRENT_SKILL_HASHES,
    _PRIOR_SKILL_HASHES,
    bundled_skill_hashes,
)


class TestManifestTracksCurrentBundle:
    def test_manifest_matches_current_bundle(self):
        """The core guard: every bundled SKILL.md hashes to its manifest
        entry. If this fails, a skill was edited without rolling the
        manifest."""
        current = bundled_skill_hashes()
        for name, cur_hash in current.items():
            assert name in _CURRENT_SKILL_HASHES, (
                f"bundled skill {name!r} has no _CURRENT_SKILL_HASHES entry. "
                f"Add it (and move any prior hash into _PRIOR_SKILL_HASHES)."
            )
            assert cur_hash == _CURRENT_SKILL_HASHES[name], (
                f"bundled skill {name!r} changed: its SKILL.md now hashes to "
                f"{cur_hash} but the manifest says {_CURRENT_SKILL_HASHES[name]}. "
                f"Fix in init.py: (i) append the OLD hash "
                f"{_CURRENT_SKILL_HASHES[name]!r} into _PRIOR_SKILL_HASHES[{name!r}] "
                f"so existing teams auto-refresh, and (ii) set "
                f"_CURRENT_SKILL_HASHES[{name!r}] = {cur_hash!r}."
            )

    def test_manifest_covers_exactly_the_bundled_skills(self):
        """No skill is missing from the manifest and the manifest names no
        skill that no longer exists (so adding/removing a bundled skill
        forces a manifest update too)."""
        assert set(bundled_skill_hashes()) == set(_CURRENT_SKILL_HASHES)

    def test_manifest_current_is_never_listed_as_a_prior(self):
        """A skill's CURRENT hash must not appear in its own prior set --
        that would be the 'appended the NEW hash instead of the OLD one'
        slip, which stops propagation."""
        for name, cur_hash in _CURRENT_SKILL_HASHES.items():
            assert cur_hash not in _PRIOR_SKILL_HASHES.get(name, set()), (
                f"{name}: current manifest hash is also in _PRIOR_SKILL_HASHES "
                f"-- you appended the NEW hash instead of the OLD one."
            )


class TestNamedPriorFixturesRegistered:
    """The two concrete gaps the task fixes (used as acceptance fixtures)."""

    def test_journal_new_pre_defer_hash_registered(self):
        # The exact pre-defer copy refresh skipped on Shohoku.
        assert (
            "df0105db8bbfa6adfdf1ad27494712ae480e1c23fa1abc10b6d6e44404c52816"
            in _PRIOR_SKILL_HASHES["journal-new"]
        )

    def test_tigerharness_basics_has_a_prior_key(self):
        # There was no key for this skill before; it must exist now and
        # cover the pre-unit-name-change ship.
        assert "tigerharness-basics" in _PRIOR_SKILL_HASHES
        assert (
            "203ba77acfbc23ba3608b856a65a6c06d2731b8cd01da5f3169b4fcc90252ea9"
            in _PRIOR_SKILL_HASHES["tigerharness-basics"]
        )


class TestGuardDetectsUnrecordedChange:
    """Prove the guard FAILS on a simulated unrecorded skill change and
    PASSES on the current tree -- the brief's acceptance for the guard."""

    def _fake_skill(self, root: Path, name: str, body: str) -> None:
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")

    def test_unrecorded_change_is_flagged(self, tmp_path: Path):
        # A simulated bundle + its committed manifest, in sync.
        self._fake_skill(tmp_path, "demo", "v1\n")
        manifest = bundled_skill_hashes(tmp_path)
        assert bundled_skill_hashes(tmp_path) == manifest  # passes when in sync

        # Now edit the skill WITHOUT updating the manifest: the guard's
        # comparison must report the mismatch (i.e., the real guard would
        # fail CI).
        (tmp_path / "demo" / "SKILL.md").write_text("v2\n", encoding="utf-8")
        after = bundled_skill_hashes(tmp_path)
        assert after != manifest
        assert after["demo"] != manifest["demo"]

    def test_current_tree_passes_the_guard(self):
        # Sanity: against the real bundle, current == manifest (no drift).
        assert bundled_skill_hashes() == _CURRENT_SKILL_HASHES


class TestBundledSkillHashesHelper:
    def test_missing_dir_returns_empty(self, tmp_path: Path):
        assert bundled_skill_hashes(tmp_path / "nope") == {}

    def test_skill_dir_without_skill_md_is_skipped(self, tmp_path: Path):
        (tmp_path / "has").mkdir()
        (tmp_path / "has" / "SKILL.md").write_text("x\n", encoding="utf-8")
        (tmp_path / "empty").mkdir()  # no SKILL.md -> skipped
        out = bundled_skill_hashes(tmp_path)
        assert set(out) == {"has"}

    def test_default_dir_is_the_package_bundle(self):
        # No arg -> reads the package _bundled_skills (same as init uses).
        assert set(bundled_skill_hashes()) == {
            d.name
            for d in (_init._bundled_skills_dir()).iterdir()
            if (d / "SKILL.md").is_file()
        }
