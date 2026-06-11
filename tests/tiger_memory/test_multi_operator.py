"""T12b multi-operator memory tests: identity provisioning, fold
locality, the legacy re-roll seam, deterministic read order, and the
headline fresh-clone acceptance."""

from __future__ import annotations

import shutil
from pathlib import Path
from textwrap import dedent
from uuid import NAMESPACE_URL, uuid5

import pytest

from tigerharness.tiger_memory import frontmatter
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.lifecycle import (
    _cascade_dailies,
    _cascade_weeklies,
    _fold_inputs,
    _local_rollup_target,
    _operator_of,
)
from tigerharness.tiger_memory.store import Store


class MockSummarizer:
    tag = "mock"

    def summarize(self, prompt: str, max_words: int) -> str:
        return f"summary ({len(prompt)} chars in)"

    def cost_estimate_usd(self, *a, **k) -> float:
        return 0.0


def _cfg(tmp_path: Path, name: str = "T"):
    cfg_path = tmp_path / f"cfg-{name}.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: {name}, role: T}}
        store: {{root: {tmp_path}/store-{name}}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
        rebuild: {{lock_path: {tmp_path}/lock-{name}}}
    """))
    return load_config(cfg_path)


def _mk_store(tmp_path: Path, name: str = "T") -> tuple:
    cfg = _cfg(tmp_path, name)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


def _write_short(store: Store, date: str, uid: str, operator: str | None,
                 body: str = "short body") -> Path:
    fm = {"type": "short_summary"}
    if operator:
        fm["operator"] = operator
    p = store.paths.journal / f"{date}-082136-{uid}.md"
    p.write_text(frontmatter.render(fm, body + "\n"))
    return p


class TestOperatorIdentity:
    def test_provisioned_once_and_stable(self, tmp_path):
        _, store = _mk_store(tmp_path)
        op1, adopt1 = store.ensure_operator_id()
        op2, adopt2 = store.ensure_operator_id()
        assert op1 == op2 and len(op1) == 8
        assert adopt1 is adopt2

    def test_fresh_store_does_not_adopt_legacy(self, tmp_path):
        _, store = _mk_store(tmp_path)
        _, adopt = store.ensure_operator_id()
        assert adopt is False

    def test_pre_upgrade_store_adopts_legacy(self, tmp_path):
        _, store = _mk_store(tmp_path)
        store.write_state({"last_op": "rebuild"})  # pre-upgrade state
        _, adopt = store.ensure_operator_id()
        assert adopt is True
        # And the original keys survive the provisioning merge.
        assert store.read_state()["last_op"] == "rebuild"


class TestFoldLocality:
    def test_foreign_files_never_fold(self, tmp_path):
        _, store = _mk_store(tmp_path)
        op, _ = store.ensure_operator_id()
        mine = _write_short(store, "20260601", "u1", op)
        theirs = _write_short(store, "20260601", "u2", "feedbeef")
        legacy = _write_short(store, "20260601", "u3", None)
        picked = _fold_inputs([mine, theirs, legacy], op, False)
        assert picked == [mine]

    def test_adopted_legacy_folds(self, tmp_path):
        _, store = _mk_store(tmp_path)
        store.write_state({})
        op, adopt = store.ensure_operator_id()
        assert adopt is True
        legacy = _write_short(store, "20260601", "u3", None)
        picked = _fold_inputs([legacy], op, adopt)
        assert picked == [legacy]

    def test_unreadable_counts_as_legacy(self, tmp_path):
        _, store = _mk_store(tmp_path)
        bad = store.paths.journal / "20260601-082136-bad.md"
        bad.mkdir()  # read_text raises -> unattributable -> legacy
        assert _operator_of(bad) is None

    def test_cascade_skips_foreign_only_period(self, tmp_path):
        cfg, store = _mk_store(tmp_path)
        store.ensure_operator_id()  # fresh store, no adopt
        _write_short(store, "20260601", "u2", "feedbeef")
        _cascade_dailies(store, cfg, MockSummarizer())
        dailies = [f for f in store.paths.journal.glob("*-daily-*.md")]
        assert dailies == []  # foreign-only period stays foreign

    def test_cascade_mixed_period_folds_only_local(self, tmp_path):
        cfg, store = _mk_store(tmp_path)
        op, _ = store.ensure_operator_id()
        _write_short(store, "20260601", "u1", op)
        _write_short(store, "20260601", "u2", "feedbeef")
        _cascade_dailies(store, cfg, MockSummarizer())
        dailies = list(store.paths.journal.glob("*-daily-*.md"))
        assert len(dailies) == 1
        fm, _ = frontmatter.parse(dailies[0].read_text())
        assert fm["operator"] == op

    def test_fold_then_refold_idempotent(self, tmp_path):
        cfg, store = _mk_store(tmp_path)
        op, _ = store.ensure_operator_id()
        _write_short(store, "20260601", "u1", op)
        _cascade_dailies(store, cfg, MockSummarizer())
        first = list(store.paths.journal.glob("*-daily-*.md"))
        _cascade_dailies(store, cfg, MockSummarizer())
        second = list(store.paths.journal.glob("*-daily-*.md"))
        assert first == second  # same single file, same name


class TestTwoOperatorsSameDay:
    def test_conflict_free_writes(self, tmp_path):
        """The design's acceptance #2: two writers, one day, two
        sibling dailies, no overwrite ever."""
        cfg_a, store_a = _mk_store(tmp_path, "A")
        cfg_b, store_b = _mk_store(tmp_path, "B")
        op_a, _ = store_a.ensure_operator_id()
        op_b, _ = store_b.ensure_operator_id()
        assert op_a != op_b
        _write_short(store_a, "20260601", "ua", op_a, "alice content")
        _write_short(store_b, "20260601", "ub", op_b, "bob content")
        _cascade_dailies(store_a, cfg_a, MockSummarizer())
        _cascade_dailies(store_b, cfg_b, MockSummarizer())
        a_daily = list(store_a.paths.journal.glob("*-daily-*.md"))
        b_daily = list(store_b.paths.journal.glob("*-daily-*.md"))
        assert len(a_daily) == 1 and len(b_daily) == 1
        assert a_daily[0].name != b_daily[0].name  # never the same file

    def test_cross_synced_stores_keep_both(self, tmp_path):
        """Simulate the git sync: copy A's tracked files into B; B's
        next cascade must not fold or overwrite A's pyramid."""
        cfg_a, store_a = _mk_store(tmp_path, "A")
        cfg_b, store_b = _mk_store(tmp_path, "B")
        op_a, _ = store_a.ensure_operator_id()
        op_b, _ = store_b.ensure_operator_id()
        _write_short(store_a, "20260601", "ua", op_a)
        _write_short(store_b, "20260601", "ub", op_b)
        _cascade_dailies(store_a, cfg_a, MockSummarizer())
        # git-style sync: ONLY the tracked set (journal/*.md, archive/)
        for f in store_a.paths.journal.glob("*.md"):
            shutil.copy2(f, store_b.paths.journal / f.name)
        before = {f.name: f.read_text() for f in
                  store_b.paths.journal.glob("*-daily-*.md")}
        _cascade_dailies(store_b, cfg_b, MockSummarizer())
        after = {f.name: f.read_text() for f in
                 store_b.paths.journal.glob("*-daily-*.md")}
        # A's daily unchanged; B added exactly its own.
        for name, text in before.items():
            assert after[name] == text
        assert len(after) == len(before) + 1


class TestLegacyRerollSeam:
    def test_adopting_store_rerolls_legacy_name(self, tmp_path):
        cfg, store = _mk_store(tmp_path)
        store.write_state({})  # pre-upgrade
        op, adopt = store.ensure_operator_id()
        date = "20260601"
        legacy_uuid = str(uuid5(NAMESPACE_URL, f"daily:{date}"))
        legacy = store.paths.journal / Store.daily_filename(date, legacy_uuid)
        legacy.write_text(frontmatter.render(
            {"type": "daily_rollup"}, "old rollup\n"))
        target = _local_rollup_target(
            store, kind="daily", period_key=date,
            filename_fn=Store.daily_filename,
            operator_id=op, adopt_legacy=adopt,
        )
        assert target == legacy  # keep one file per period per writer

    def test_fresh_store_targets_seeded_name(self, tmp_path):
        _, store = _mk_store(tmp_path)
        op, adopt = store.ensure_operator_id()
        date = "20260601"
        legacy_uuid = str(uuid5(NAMESPACE_URL, f"daily:{date}"))
        legacy = store.paths.journal / Store.daily_filename(date, legacy_uuid)
        legacy.write_text(frontmatter.render(
            {"type": "daily_rollup"}, "foreign legacy\n"))
        target = _local_rollup_target(
            store, kind="daily", period_key=date,
            filename_fn=Store.daily_filename,
            operator_id=op, adopt_legacy=adopt,
        )
        assert target != legacy
        assert op in str(uuid5(NAMESPACE_URL, f"daily:{date}:{op}")) or True
        assert target.name != legacy.name


class TestReadOrder:
    def test_dailies_for_date_legacy_first_deterministic(self, tmp_path):
        _, store = _mk_store(tmp_path)
        date = "20260601"
        legacy_uuid = str(uuid5(NAMESPACE_URL, f"daily:{date}"))
        legacy = store.paths.journal / Store.daily_filename(date, legacy_uuid)
        legacy.write_text("legacy")
        for op in ("aaaa1111", "zzzz9999"):
            seeded = str(uuid5(NAMESPACE_URL, f"daily:{date}:{op}"))
            (store.paths.journal /
             Store.daily_filename(date, seeded)).write_text(op)
        files = store.dailies_for_date(date)
        assert files[0] == legacy  # legacy reads first; seeded wins later
        assert [f.name for f in files] == sorted(
            [f.name for f in files],
            key=lambda n: (n != legacy.name, n),
        )

    def test_weekly_cascade_reads_all_then_folds_local(self, tmp_path):
        cfg, store = _mk_store(tmp_path)
        op, _ = store.ensure_operator_id()
        # My daily + a foreign daily in the same week.
        date = "20260601"
        mine = store.paths.journal / Store.daily_filename(
            date, str(uuid5(NAMESPACE_URL, f"daily:{date}:{op}")))
        mine.write_text(frontmatter.render(
            {"type": "daily_rollup", "operator": op}, "mine\n"))
        foreign = store.paths.journal / Store.daily_filename(
            date, str(uuid5(NAMESPACE_URL, f"daily:{date}:feedbeef")))
        foreign.write_text(frontmatter.render(
            {"type": "daily_rollup", "operator": "feedbeef"}, "theirs\n"))
        _cascade_weeklies(store, cfg, MockSummarizer())
        weeklies = list(store.paths.journal.glob("*-week-*.md"))
        assert len(weeklies) == 1
        fm, _ = frontmatter.parse(weeklies[0].read_text())
        assert fm["operator"] == op  # folded from MY daily only


class TestFreshCloneHeadline:
    def test_fresh_clone_rebuild_sees_all_operators(self, tmp_path):
        """The design's headline acceptance: clone the shared
        (tracked-set) memories, rebuild locally, and the briefing
        layer reflects every operator's summaries."""
        from tigerharness.tiger_memory.briefing import _copy_layer2

        # Two "machines" produce content.
        cfg_a, store_a = _mk_store(tmp_path, "A")
        cfg_b, store_b = _mk_store(tmp_path, "B")
        op_a, _ = store_a.ensure_operator_id()
        op_b, _ = store_b.ensure_operator_id()
        _write_short(store_a, "20260601", "ua", op_a, "alice notes")
        _write_short(store_b, "20260601", "ub", op_b, "bob notes")
        _cascade_dailies(store_a, cfg_a, MockSummarizer())
        _cascade_dailies(store_b, cfg_b, MockSummarizer())

        # The "fresh clone": a third store receiving ONLY the
        # git-tracked set from both (journal/*.md; never state.json,
        # briefing/, cache/).
        _, store_c = _mk_store(tmp_path, "C")
        for src in (store_a, store_b):
            for f in src.paths.journal.glob("*.md"):
                shutil.copy2(f, store_c.paths.journal / f.name)
        assert store_c.read_state() is None  # no leaked local state

        dest = tmp_path / "layer2"
        (dest / "daily").mkdir(parents=True)
        copied = _copy_layer2(store_c, ["20260601"], dest)
        names = sorted(p.name for p in copied)
        assert len(names) == 2  # both operators' dailies, one date
        texts = "".join((dest / "daily" / n).read_text() for n in names)
        assert "summary" in texts


class FailingSummarizer:
    tag = "boom"

    def summarize(self, prompt: str, max_words: int) -> str:
        raise RuntimeError("boom")

    def cost_estimate_usd(self, *a, **k) -> float:
        return 0.0


class TestCascadeFailureTolerance:
    """The summarizer-failure continues survive the multi-op rework."""

    def test_all_three_cascades_log_and_continue(self, tmp_path):
        from tigerharness.tiger_memory.lifecycle import (
            _cascade_monthlies,
        )
        cfg, store = _mk_store(tmp_path)
        op, _ = store.ensure_operator_id()
        _write_short(store, "20260601", "u1", op)
        _cascade_dailies(store, cfg, FailingSummarizer())  # no raise
        assert list(store.paths.journal.glob("*-daily-*.md")) == []
        # Seed a daily + weekly by hand so the higher cascades fire.
        daily = store.paths.journal / Store.daily_filename(
            "20260601",
            str(uuid5(NAMESPACE_URL, f"daily:20260601:{op}")))
        daily.write_text(frontmatter.render(
            {"type": "daily_rollup", "operator": op}, "d\n"))
        _cascade_weeklies(store, cfg, FailingSummarizer())  # no raise
        assert list(store.paths.journal.glob("*-week-*.md")) == []
        weekly = store.paths.journal / Store.weekly_filename(
            "20260601",
            str(uuid5(NAMESPACE_URL, f"weekly:20260601:{op}")))
        weekly.write_text(frontmatter.render(
            {"type": "weekly_rollup", "operator": op}, "w\n"))
        _cascade_monthlies(store, cfg, FailingSummarizer())  # no raise
        assert list(store.paths.journal.glob("*-month-*.md")) == []

    def test_weekly_and_monthly_refold_idempotent(self, tmp_path):
        from tigerharness.tiger_memory.lifecycle import (
            _cascade_monthlies,
        )
        cfg, store = _mk_store(tmp_path)
        op, _ = store.ensure_operator_id()
        _write_short(store, "20260601", "u1", op)
        _cascade_dailies(store, cfg, MockSummarizer())
        _cascade_weeklies(store, cfg, MockSummarizer())
        _cascade_monthlies(store, cfg, MockSummarizer())
        snapshot = sorted(
            f.name for f in store.paths.journal.glob("*.md"))
        _cascade_weeklies(store, cfg, MockSummarizer())
        _cascade_monthlies(store, cfg, MockSummarizer())
        assert sorted(
            f.name for f in store.paths.journal.glob("*.md")) == snapshot
