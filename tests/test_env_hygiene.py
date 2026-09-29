"""The env-hygiene guard: no env var may be present in ``src/`` by oversight.

``tests/conftest.py`` protects the suite from ambient environment with a
**hand-maintained** list. PR #109 fixed the one variable that had gone
missing from it (``TIGERHARNESS_JOURNAL_SLACK_DRIVES``, after weeks of a red
``TestClaimRailGuard`` on one host and green CI); nothing stopped the next
one. These tests close the class: they read the package source for every
env-var-shaped literal and require each to be a **recorded decision** --
scrubbed, or exempt with a reason.

Scrubbing everything is explicitly *not* the goal. An exempt entry is a
complete answer, and for a name no code path reads from ``os.environ``
it is the *better* answer, because an inert scrub entry advertises a
protection that does not exist. What the guard forbids is silence.

The invariant runs in both directions, which is the part a forward-only
sweep gets wrong: ``SCRUBBED_ENV_VARS`` already carried a name the source
no longer mentions, and a one-directional check would neither bless it nor
notice the second one accumulating behind it.

See :mod:`tests.env_hygiene` for why the scan matches literal *shape*
rather than read mechanism -- the short version is that matching
mechanisms means maintaining a second hand-maintained list, of receiver
names, that rots in exactly the same silence as the first.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tigerharness.autodrive import cli as autodrive_cli
from tigerharness.autodrive.settings import AUTOSTART_ENV, Settings

from tests import conftest
from tests.conftest import (
    EXEMPT_ENV_VARS,
    NOT_ENV_VAR_LITERALS,
    SCRUBBED_ENV_VARS,
    SCRUBBED_NOT_IN_SOURCE,
    RealDaemonSpawnBlocked,
)
from tests.env_hygiene import (
    PACKAGE_ROOT,
    computed_env_reads,
    env_name_literals,
    environ_write_sites,
)

#: Names that must appear in any correct scan, one per read mechanism listed
#: in :mod:`tests.env_hygiene`. Without this the forward guard passes
#: vacuously the moment the scan returns nothing -- a moved package root, a
#: tightened regex, a swallowed parse error. A guard that cannot fail is
#: worse than no guard, because it reads as evidence.
SCAN_SENTINELS = frozenset({
    "TIGERHARNESS_JOURNAL_STUCK_TIMEOUT",   # direct os.environ.get
    "TIGERHARNESS_JOURNAL_SLACK_DRIVES",    # Settings.flag indirection
    "TIGERHARNESS_SLACK_WATCHDOG",          # from_env(env=None) indirection
    "TIGERHARNESS_AUTODRIVE_AUTOSTART",     # module-level name constant
    "ALLOWED_SLACK_USER_IDS",               # tuple of candidate spellings
})


def _scrub_families() -> dict[str, tuple[str, ...]]:
    """Every ``*_ENV_VARS`` tuple in ``conftest``, discovered rather than
    listed. A hard-coded roster here would be a third hand-maintained list
    with the same failure mode as the first two: a family added to
    ``conftest`` and wired into ``SCRUBBED_ENV_VARS`` but forgotten here
    would silently stop being checked for duplicates. ``SCRUBBED_ENV_VARS``
    itself is the aggregate, not a family; the exemption registers are
    dicts and filtered out by type.
    """
    return {
        name: value
        for name, value in vars(conftest).items()
        if name.endswith("_ENV_VARS")
        and name != "SCRUBBED_ENV_VARS"
        and isinstance(value, tuple)
    }


SCRUB_FAMILIES = _scrub_families()


class TestScanIsSound:
    """The scan must be trustworthy before anything asserted on it means
    something."""

    def test_package_root_exists(self):
        """A wrong root makes every other test here pass on an empty set."""
        assert PACKAGE_ROOT.is_dir(), (
            f"env-hygiene scan root {PACKAGE_ROOT} is not a directory; the "
            f"scan would silently find nothing and every guard below would "
            f"pass vacuously"
        )

    def test_scan_finds_every_read_mechanism(self):
        """One sentinel per indirection the package actually uses."""
        found = set(env_name_literals())
        missing = sorted(SCAN_SENTINELS - found)
        assert not missing, (
            f"the env-var scan no longer finds {missing}, which the package "
            f"demonstrably reads. The scan is broken, not the source: fix "
            f"tests/env_hygiene.py before trusting any result below"
        )

    def test_all_exports_are_not_reported_as_env_vars(self, tmp_path: Path):
        """``__all__`` entries are identifiers, excluded structurally."""
        mod = tmp_path / "m.py"
        mod.write_text(
            '__all__ = ["SOME_EXPORTED_CONSTANT"]\n'
            'x = os.environ.get("SOME_REAL_ENV_VAR")\n',
            encoding="utf-8",
        )
        found = env_name_literals(tmp_path)
        assert "SOME_EXPORTED_CONSTANT" not in found
        assert "SOME_REAL_ENV_VAR" in found


class TestTheScanCannotQuietlyGoBlind:
    """Two limits Anzai flagged when correcting the PRD's figures. Both are
    real; neither is left as a comment, because a limitation a guard
    depends on and does not assert is the same defect in a new place."""

    def test_no_env_read_uses_a_computed_name(self):
        """The scan's load-bearing assumption -- that a variable is named as
        a literal somewhere -- enforced rather than trusted.

        Passing the name through a variable is fine; *assembling* it is not.
        ``os.environ.get(f"TIGERHARNESS_{suffix}")`` is unenumerable by any
        static scan, so the guard above would keep reporting a completeness
        it had quietly lost."""
        computed = computed_env_reads()
        assert not computed, (
            f"os.environ read(s) with a computed key at {computed}. The "
            f"env-hygiene guard enumerates env vars by finding their names "
            f"written as literals in src/; a name built at runtime is "
            f"invisible to it and to every future reader. Name it as a "
            f"module-level constant and index that instead."
        )

    # The check above passes by finding nothing, which is exactly how a
    # broken detector looks. Against real ``src/`` it has only ever
    # returned ``[]``, and ``--cov=src/tigerharness`` does not measure
    # ``tests/``, so the coverage gate cannot notice a detection branch
    # that never runs. These exercise each form on a synthetic module.
    @pytest.mark.parametrize("read", [
        pytest.param('os.environ.get(f"TIGERHARNESS_{x}")', id="fstring"),
        pytest.param('os.environ.get("TIGERHARNESS_" + x)', id="concat"),
        pytest.param('os.environ.get("TIGERHARNESS_{}".format(x))', id="format"),
        pytest.param('os.environ[f"TIGERHARNESS_{x}"]', id="subscript"),
        pytest.param('os.getenv(f"TIGERHARNESS_{x}")', id="getenv"),
        pytest.param('os.environ.pop(f"TIGERHARNESS_{x}", None)', id="pop"),
        pytest.param('os.environ.get(A if x else B)', id="ifexp"),
        pytest.param('environ.get(f"TIGERHARNESS_{x}")', id="bare-environ"),
        pytest.param('getenv(f"TIGERHARNESS_{x}")', id="bare-getenv"),
    ])
    def test_computed_forms_are_detected(self, tmp_path: Path, read: str):
        (tmp_path / "m.py").write_text(f"v = {read}\n", encoding="utf-8")
        assert computed_env_reads(tmp_path) == ["m.py:1"], (
            f"a computed env-var name written as {read} is not detected"
        )

    @pytest.mark.parametrize("read", [
        pytest.param('os.environ.get("TIGERHARNESS_LITERAL")', id="literal"),
        pytest.param('os.environ.get(ENV_VAR)', id="module-constant"),
        pytest.param('os.environ[ENV_VAR]', id="constant-subscript"),
        pytest.param('some_dict.get(f"computed_{x}")', id="not-environ"),
        pytest.param('cfg.env.get(f"computed_{x}")', id="injected-dict"),
    ])
    def test_acceptable_forms_are_not_flagged(self, tmp_path: Path, read: str):
        """A name passed through a variable is fine -- the literal still
        exists somewhere for the shape scan to find. And a computed key on
        a mapping that is *not* ``os.environ`` is an ordinary dict lookup;
        flagging those would bury the signal in thousands of false hits,
        which is why the check is scoped to the ambient channel itself."""
        (tmp_path / "m.py").write_text(f"v = {read}\n", encoding="utf-8")
        assert computed_env_reads(tmp_path) == []

    def test_suite_cwd_is_not_a_team_root(self):
        """The second contamination channel, which scrubbing cannot reach.

        ``Settings.get`` reads ``os.environ`` **first, then the team's
        ``configs/.env``**. The autouse fixture closes the first door only.
        A suite whose cwd is a real team root reads that team's live knobs
        through the second one with a pristine environment -- and
        ``journal/paths.py:default_journal_root`` would resolve the team's
        own ``journal/`` as well. Neither is something a scrub list can fix,
        so assert the precondition that makes the scrub sufficient."""
        cwd = Path.cwd()
        assert not (cwd / "configs" / "personas.yaml").is_file(), (
            f"pytest is running from inside a team root ({cwd}). Settings "
            f"reads that team's configs/.env after os.environ, so its live "
            f"knobs reach the suite through a channel the autouse env scrub "
            f"cannot close. Run the suite from the package repo root."
        )


class TestEveryEnvNameIsAccountedFor:
    """The forward direction: source -> a recorded decision."""

    def test_no_unaccounted_env_name_in_source(self):
        accounted = (
            set(SCRUBBED_ENV_VARS)
            | set(EXEMPT_ENV_VARS)
            | set(NOT_ENV_VAR_LITERALS)
        )
        found = env_name_literals()
        unaccounted = {n: l for n, l in found.items() if n not in accounted}
        assert not unaccounted, (
            "env var(s) read by src/ but accounted for nowhere in "
            "tests/conftest.py:\n"
            + "\n".join(
                f"  {name} -- read at {', '.join(locs)}"
                for name, locs in sorted(unaccounted.items())
            )
            + "\n\nEvery env var the package names must be a recorded "
            "decision. Either add it to a SCRUBBED_ENV_VARS family (the "
            "default -- ambient values must not reach the suite), or, if no "
            "code path reads it from os.environ, to EXEMPT_ENV_VARS with a "
            "one-line reason. A literal of this shape that is not an env "
            "var at all goes in NOT_ENV_VAR_LITERALS."
        )

    def test_exempt_reasons_are_present(self):
        """An exemption without a reason is the oversight wearing a badge."""
        for register in (
            EXEMPT_ENV_VARS, NOT_ENV_VAR_LITERALS, SCRUBBED_NOT_IN_SOURCE,
        ):
            for name, reason in register.items():
                assert reason.strip(), f"{name} is exempt with no reason given"


class TestEveryScrubbedNameIsAccountedFor:
    """The reverse direction: a scrub entry must still correspond to
    something. Missing this is how ``SSL_CERT_DIR`` and a mistaken second
    entry sat unexamined."""

    def test_no_dead_scrub_entry(self):
        found = set(env_name_literals())
        dead = sorted(
            n for n in SCRUBBED_ENV_VARS
            if n not in found and n not in SCRUBBED_NOT_IN_SOURCE
        )
        assert not dead, (
            f"scrubbed env var(s) that no longer appear anywhere in src/: "
            f"{dead}. Drop them, or record why they are kept in "
            f"SCRUBBED_NOT_IN_SOURCE (a name read below us by a library, or "
            f"one that travels with another in a sourced .env)."
        )

    def test_scrubbed_not_in_source_is_truthful(self):
        """The register is for names genuinely absent. One that comes back
        into the source must leave it, or the reason is now a lie."""
        found = set(env_name_literals())
        resurrected = sorted(n for n in SCRUBBED_NOT_IN_SOURCE if n in found)
        assert not resurrected, (
            f"{resurrected} now appear(s) in src/ but is still listed in "
            f"SCRUBBED_NOT_IN_SOURCE as absent; remove the entry"
        )


class TestRegistersAreWellFormed:
    """Bookkeeping that keeps the three registers readable as one decision
    table."""

    def test_families_compose_scrubbed_env_vars(self):
        composed = sum(SCRUB_FAMILIES.values(), ())
        assert sorted(SCRUBBED_ENV_VARS) == sorted(composed), (
            "SCRUBBED_ENV_VARS is not the concatenation of its families; a "
            "family was added without being wired into it (so it is not "
            "scrubbed) or wired in twice"
        )

    def test_no_duplicate_scrub_entries(self):
        seen: dict[str, str] = {}
        dupes: list[str] = []
        for family, names in SCRUB_FAMILIES.items():
            for name in names:
                if name in seen:
                    dupes.append(f"{name} in both {seen[name]} and {family}")
                seen[name] = family
        assert not dupes, "; ".join(dupes)

    def test_registers_are_disjoint(self):
        """A name cannot be both scrubbed and exempt -- that reads as two
        contradictory decisions, and one of them is stale."""
        scrubbed = set(SCRUBBED_ENV_VARS)
        for label, register in (
            ("EXEMPT_ENV_VARS", EXEMPT_ENV_VARS),
            ("NOT_ENV_VAR_LITERALS", NOT_ENV_VAR_LITERALS),
        ):
            overlap = sorted(scrubbed & set(register))
            assert not overlap, (
                f"{overlap} appear in both SCRUBBED_ENV_VARS and {label}"
            )
        overlap = sorted(set(EXEMPT_ENV_VARS) & set(NOT_ENV_VAR_LITERALS))
        assert not overlap, (
            f"{overlap} are recorded both as an unscrubbed env var and as "
            f"not an env var at all"
        )


class TestScrubActuallyHappens:
    """The registers above are bookkeeping; this is the runtime invariant.
    Run from inside a bridge session or an autodrive drive -- which is the
    acceptance run -- these stay green only because the autouse fixture
    removed the variables."""

    @pytest.mark.parametrize("var", sorted(SCRUBBED_ENV_VARS))
    def test_scrubbed_var_absent_during_tests(self, var: str):
        assert var not in os.environ, (
            f"{var} leaked into the test env; the autouse scrub in "
            f"tests/conftest.py did not run or no longer lists it"
        )


class TestRealDaemonSpawnGuardIsWired:
    """``TIGERHARNESS_AUTODRIVE_AUTOSTART`` gets a stronger assertion than
    scrubbing, because its consequence is worse than a flipped assertion:
    real detached daemons that outlive pytest. The scrub removes the
    trigger; ``block_real_daemon_spawn`` removes the capability. Only the
    second one holds if the list is ever wrong again -- so prove it is
    still wired, rather than trusting that it is."""

    def test_autostart_reaching_the_spawn_raises_through_ensure_running(
        self, tmp_path: Path,
    ):
        """The full incident arc: autostart on, ``ensure_running`` called
        after a queue write, spawn reached. ``ensure_running`` catches every
        ``Exception`` on purpose (a queued task must survive a daemon that
        will not start), so this also proves the guard is *not* swallowed --
        being caught and logged at WARNING is precisely how the leak stayed
        invisible."""
        settings = Settings(env={AUTOSTART_ENV: "1"})
        assert settings.autostart, "sanity: the arc must actually be armed"
        with pytest.raises(RealDaemonSpawnBlocked):
            autodrive_cli.ensure_running(tmp_path / "journal", settings=settings)

    def test_guard_is_not_an_exception_subclass(self):
        """The property the arc above depends on, asserted directly so a
        well-meaning ``class RealDaemonSpawnBlocked(Exception)`` is caught
        here rather than by a fleet of orphaned daemons."""
        assert not issubclass(RealDaemonSpawnBlocked, Exception)
        assert issubclass(RealDaemonSpawnBlocked, BaseException)


class TestNothingWritesTheProcessEnvironment:
    """The production-side twin of the scrub, and the reason the scrub had
    to run before *every* test rather than once per session.

    ``slack_bridge/notify.py``'s ``.env`` loader wrote every key it parsed
    into ``os.environ`` and left it there -- so one Slack post exported a
    team's whole ``.env`` (unrelated secrets included) to the rest of the
    process and to every child spawned with ``{**os.environ}``. The
    registers above answer "is this variable accounted for?"; they cannot
    answer "did something *set* it?", because a write site names its key
    through a variable (``os.environ[key] = value``) and so has no literal
    for the shape scan to find. Hence a detector of its own.
    """

    def test_no_environ_write_in_src(self):
        writes = environ_write_sites()
        assert not writes, (
            f"the package writes the process environment at {writes}. A "
            f"write outlives the call: every later reader in the process "
            f"sees it, every child spawned with {{**os.environ}} inherits "
            f"it, and monkeypatch cannot undo it. Parse config into a dict "
            f"and pass it explicitly -- see "
            f"notify._read_slack_bridge_dotenv / _resolve_env, and "
            f"multi._load_env_file."
        )

    # As with the computed-read detector: this passes by finding nothing,
    # which is how a broken detector also looks, and ``--cov=src`` does not
    # measure ``tests/``. Each form is exercised on a synthetic module.
    @pytest.mark.parametrize("write", [
        pytest.param('os.environ["X_Y"] = v', id="subscript-assign"),
        pytest.param('del os.environ["X_Y"]', id="subscript-del"),
        pytest.param('os.environ.update(d)', id="update"),
        pytest.param('os.environ.setdefault("X_Y", v)', id="setdefault"),
        pytest.param('os.environ.pop("X_Y", None)', id="pop"),
        pytest.param('os.environ.clear()', id="clear"),
        pytest.param('os.putenv("X_Y", v)', id="putenv"),
        pytest.param('os.unsetenv("X_Y")', id="unsetenv"),
        pytest.param('environ["X_Y"] = v', id="bare-environ"),
        pytest.param('putenv("X_Y", v)', id="bare-putenv"),
        # Measured, not assumed: both of these really do mutate the
        # process environment, and neither is a subscript or a sugared
        # method call, so each needed its own branch in the detector.
        pytest.param('os.environ |= d', id="augassign-ior"),
        pytest.param('environ |= d', id="bare-augassign-ior"),
        pytest.param('os.environ.__setitem__("X_Y", v)', id="dunder-setitem"),
        pytest.param('os.environ.__delitem__("X_Y")', id="dunder-delitem"),
        pytest.param('os.environ.__ior__(d)', id="dunder-ior"),
        pytest.param('os.environ = {}', id="attribute-rebind"),
        pytest.param('del os.environ', id="attribute-del"),
    ])
    def test_every_write_form_is_detected(self, tmp_path: Path, write: str):
        (tmp_path / "m.py").write_text(f"{write}\n", encoding="utf-8")
        assert environ_write_sites(tmp_path) == ["m.py:1"], (
            f"an os.environ write spelled {write} is not detected"
        )

    def test_the_exact_loader_body_would_be_caught(self, tmp_path: Path):
        """The incident itself as a fixture, not a paraphrase.

        A guard justified by a specific past defect should be shown to
        catch that defect. This is ``_load_slack_bridge_dotenv``'s former
        body, verbatim in shape: the conditional write inside two loops,
        keyed on a variable so no literal exists to find.
        """
        (tmp_path / "m.py").write_text(
            "def _load_slack_bridge_dotenv():\n"
            "    for env_path in candidates:\n"
            "        for line in env_path.read_text().splitlines():\n"
            "            key, _, value = line.partition('=')\n"
            "            if key and key not in os.environ:\n"
            "                os.environ[key] = value\n",
            encoding="utf-8",
        )
        assert environ_write_sites(tmp_path) == ["m.py:6"]

    @pytest.mark.parametrize("read", [
        pytest.param('v = os.environ["X_Y"]', id="subscript-read"),
        pytest.param('v = os.environ.get("X_Y")', id="get"),
        pytest.param('v = "X_Y" in os.environ', id="contains"),
        pytest.param('some_dict["X_Y"] = v', id="not-environ-assign"),
        pytest.param('parsed.setdefault("X_Y", v)', id="not-environ-setdefault"),
        pytest.param('env_file.pop("X_Y", None)', id="not-environ-pop"),
        pytest.param('parsed |= d', id="not-environ-augassign"),
        pytest.param('for environ in xs: pass', id="bare-name-loop-target"),
        pytest.param('environ = {}', id="bare-name-rebind"),
        pytest.param('cfg.environ', id="attribute-read"),
    ])
    def test_reads_and_other_mappings_are_not_flagged(
        self, tmp_path: Path, read: str
    ):
        """``os.environ[k]`` in a load context is an ordinary read, and a
        mutation of some *other* mapping is an ordinary assignment. Both
        are what the fixed code does -- ``_resolve_env`` reads
        ``os.environ[name]`` and builds a plain dict -- so flagging either
        would make the guard fire on the fix."""
        (tmp_path / "m.py").write_text(f"{read}\n", encoding="utf-8")
        assert environ_write_sites(tmp_path) == []
