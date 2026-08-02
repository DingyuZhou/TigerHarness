"""Team-wide event log (ADR 0008): store, append, compaction, CLI, config.

Covers the full surface of ``tiger_memory.team_events`` plus its
integration points: the v3 card contract's ``@@TEAM_EVENTS@@`` section,
the executor's dated append, the two team-level CLI verbs, and the
``memory.team_events`` config block.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import cli, lifecycle as lc, team_events as tev
from tigerharness.tiger_memory.config import ConfigError, load_config
from tigerharness.tiger_memory.executor import ingest_extraction
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers.base import Summarizer

NOW = "2026-08-01T12:00:00+00:00"


def _cfg(tmp_path: Path, team_events_yaml: str = ""):
    cfg_path = tmp_path / "cfg.yaml"
    extra = ""
    if team_events_yaml:
        extra = "memory:\n  team_events:\n" + "".join(
            f"    {line}\n" for line in team_events_yaml.splitlines()
        )
    cfg_path.write_text(
        dedent(
            f"""\
            agent:
              name: TestTiger
              role: "Test consumer."

            store:
              root: {tmp_path}/memory

            sources:
              - kind: claude_code
                project_path: {tmp_path}/fake-claude-projects/

            summarizer:
              backend: anthropic
              model: claude-opus-4-7
              prompts: default/v1

            rebuild:
              lock_path: {tmp_path}/test.lock
            """
        )
        + extra
    )
    return load_config(cfg_path), cfg_path


# ----- load / render / kinds -------------------------------------------------


def test_load_sections_missing_file_is_empty(tmp_path):
    assert tev.load_sections(tmp_path / "nope.md") == []


def test_load_render_roundtrip_and_order(tmp_path):
    cfg, _ = _cfg(tmp_path)
    path = tev.events_path(cfg)
    path.parent.mkdir(parents=True)
    path.write_text(
        "# Team event log\n\npreamble prose (dropped)\n\n"
        "## 2025\n- Old year summary.\n\n"
        "## 2026-07\n- Month summary.\nstray non-bullet kept\n\n"
        "## 2026-07-31\n- TestTiger did a thing.\n",
        encoding="utf-8",
    )
    sections = tev.load_sections(path)
    assert [s.period for s in sections] == ["2025", "2026-07", "2026-07-31"]
    assert [s.kind for s in sections] == [
        tev.KIND_YEAR, tev.KIND_MONTH, tev.KIND_DAY,
    ]
    assert "stray non-bullet kept" in sections[1].lines
    rendered = tev.render(sections)
    # Newest first: the day above its month, the month above the old year.
    assert rendered.index("## 2026-07-31") < rendered.index("## 2026-07\n")
    assert rendered.index("## 2026-07\n") < rendered.index("## 2025")
    assert rendered.startswith("# Team event log")


def test_render_skips_empty_sections():
    out = tev.render([
        tev.PeriodSection("2026-08-01", ["- A did x."]),
        tev.PeriodSection("2026-07-31", []),
    ])
    assert "2026-07-31" not in out


# ----- append ----------------------------------------------------------------


def test_append_creates_file_and_formats_bullets(tmp_path):
    cfg, _ = _cfg(tmp_path)
    n = tev.append_events(
        cfg, persona="TestTiger", day="2026-08-01",
        events=["shipped   the widget..", ""], now=NOW,
    )
    assert n == 1
    text = tev.events_path(cfg).read_text(encoding="utf-8")
    assert "## 2026-08-01\n- TestTiger shipped the widget.\n" in text


def test_append_dedups_to_count_suffix(tmp_path):
    cfg, _ = _cfg(tmp_path)
    for _ in range(3):
        tev.append_events(
            cfg, persona="TestTiger", day="2026-08-01",
            events=["reviewed PR #7"], now=NOW,
        )
    text = tev.events_path(cfg).read_text(encoding="utf-8")
    assert "- TestTiger reviewed PR #7. (x3)" in text
    assert text.count("reviewed PR #7") == 1


def test_append_different_days_get_own_sections(tmp_path):
    cfg, _ = _cfg(tmp_path)
    tev.append_events(cfg, persona="A", day="2026-08-01", events=["did x"], now=NOW)
    tev.append_events(cfg, persona="B", day="2026-07-31", events=["did y"], now=NOW)
    text = tev.events_path(cfg).read_text(encoding="utf-8")
    assert text.index("## 2026-08-01") < text.index("## 2026-07-31")
    assert "- A did x." in text and "- B did y." in text


def test_append_ignores_non_bullet_lines_for_dedup(tmp_path):
    cfg, _ = _cfg(tmp_path)
    path = tev.events_path(cfg)
    path.parent.mkdir(parents=True)
    path.write_text(
        "## 2026-08-01\nstray non-bullet line\n- A did x.\n", encoding="utf-8"
    )
    tev.append_events(cfg, persona="A", day="2026-08-01", events=["did x"], now=NOW)
    text = path.read_text(encoding="utf-8")
    assert "stray non-bullet line" in text        # preserved verbatim
    assert "- A did x. (x2)" in text              # dedup still works


def test_append_bad_day_falls_back_to_now(tmp_path):
    cfg, _ = _cfg(tmp_path)
    tev.append_events(
        cfg, persona="A", day="not-a-date", events=["did x"], now=NOW
    )
    assert "## 2026-08-01" in tev.events_path(cfg).read_text(encoding="utf-8")


def test_append_disabled_or_empty_is_noop(tmp_path):
    cfg_off, _ = _cfg(tmp_path, "enabled: false")
    assert tev.append_events(
        cfg_off, persona="A", day="2026-08-01", events=["did x"], now=NOW
    ) == 0
    cfg, _ = _cfg(tmp_path)
    assert tev.append_events(
        cfg, persona="A", day="2026-08-01", events=[], now=NOW
    ) == 0
    assert not tev.events_path(cfg).exists()


def test_append_all_blank_events_saves_nothing(tmp_path):
    cfg, _ = _cfg(tmp_path)
    assert tev.append_events(
        cfg, persona="A", day="2026-08-01", events=["   "], now=NOW
    ) == 0
    assert not tev.events_path(cfg).exists()


def test_append_skips_on_live_lock(tmp_path, caplog):
    cfg, _ = _cfg(tmp_path)
    lock = tev.team_dir(cfg) / tev.LOCK_FILENAME
    lock.parent.mkdir(parents=True)
    lock.write_text(f"{os.getpid()} 0")  # our own live pid holds it
    n = tev.append_events(
        cfg, persona="A", day="2026-08-01", events=["did x"], now=NOW,
    )
    assert n == 0
    assert not tev.events_path(cfg).exists()


# ----- lock ------------------------------------------------------------------


def test_lock_reclaims_dead_and_garbage_holders(tmp_path):
    cfg, _ = _cfg(tmp_path)
    lock = tev.team_dir(cfg) / tev.LOCK_FILENAME
    lock.parent.mkdir(parents=True)
    lock.write_text("garbage")
    with tev._lock(cfg):
        assert lock.exists()
    assert not lock.exists()
    lock.write_text("999999999 0")  # dead pid
    with tev._lock(cfg):
        pass
    assert not lock.exists()


def test_pid_alive(monkeypatch):
    assert tev._pid_alive(os.getpid()) is True

    def _dead(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(tev.os, "kill", _dead)
    assert tev._pid_alive(12345) is False


# ----- date helpers + backstop ----------------------------------------------


def test_month_end():
    assert tev._month_end("2026-02") == date(2026, 2, 28)
    assert tev._month_end("2026-12") == date(2026, 12, 31)


def _tiny_cfg(tmp_path, **over):
    knobs = {
        "recent_days": 30, "year_after_days": 400,
        "month_max_chars": 120, "year_max_chars": 150,
        "max_length": 400, "overflow_limit": 600,
    }
    knobs.update(over)
    yaml = "\n".join(f"{k}: {v}" for k, v in knobs.items())
    return _cfg(tmp_path, yaml)


def test_backstop_trim_under_overflow_is_noop(tmp_path):
    cfg, _ = _tiny_cfg(tmp_path)
    sections = [tev.PeriodSection("2026-08-01", ["- A did x."])]
    survivors, dropped = tev._backstop_trim(cfg, sections)
    assert survivors == sections and dropped == []


def test_backstop_trim_drops_oldest_years_then_months_never_days(tmp_path):
    cfg, _ = _tiny_cfg(tmp_path, max_length=260, overflow_limit=300)
    pad = "x" * 60
    sections = [
        tev.PeriodSection("2024", [f"- old year {pad}."]),
        tev.PeriodSection("2025", [f"- newer year {pad}."]),
        tev.PeriodSection("2026-05", [f"- a month {pad}."]),
        tev.PeriodSection("2026-08-01", ["- today."]),
    ]
    survivors, dropped = tev._backstop_trim(cfg, sections)
    assert "2024" in dropped  # oldest year first
    assert "2026-08-01" in [s.period for s in survivors]  # days never dropped
    assert len(tev.render(survivors)) <= 300


def test_backstop_trim_stops_once_under_max(tmp_path):
    cfg, _ = _tiny_cfg(tmp_path, max_length=460, overflow_limit=470)
    pad = "x" * 120
    sections = [
        tev.PeriodSection("2023", [f"- oldest year {pad}."]),
        tev.PeriodSection("2024", [f"- newer year {pad}."]),
        tev.PeriodSection("2026-08-01", ["- today."]),
    ]
    survivors, dropped = tev._backstop_trim(cfg, sections)
    # Dropping the oldest year is enough — the newer year survives.
    assert dropped == ["2023"]
    assert "2024" in [s.period for s in survivors]


def test_backstop_trim_days_only_drops_nothing(tmp_path):
    cfg, _ = _tiny_cfg(tmp_path, max_length=100, overflow_limit=150)
    sections = [
        tev.PeriodSection("2026-08-01", [f"- day bullet {'y' * 200}."]),
    ]
    survivors, dropped = tev._backstop_trim(cfg, sections)
    assert dropped == [] and survivors == sections


# ----- compact_plan ----------------------------------------------------------


def test_compact_plan_empty_store(tmp_path):
    cfg, _ = _cfg(tmp_path)
    manifest = tev.compact_plan(cfg, now=NOW)
    assert manifest["targets"] == [] and manifest["dropped_periods"] == []
    assert (tev._staging_dir(cfg) / "manifest.json").exists()


def test_compact_plan_fresh_days_not_staged(tmp_path):
    cfg, _ = _cfg(tmp_path)
    tev.append_events(cfg, persona="A", day="2026-07-31", events=["did x"], now=NOW)
    manifest = tev.compact_plan(cfg, now=NOW)
    assert manifest["targets"] == []


def test_compact_plan_stages_aged_month_with_existing_month_section(tmp_path):
    cfg, _ = _cfg(tmp_path)
    path = tev.events_path(cfg)
    path.parent.mkdir(parents=True)
    path.write_text(
        "## 2026-06\n- A earlier June summary.\n\n"
        "## 2026-06-10\n- A did x.\n\n## 2026-06-20\n- B did y.\n\n"
        "## 2026-07-31\n- C fresh work.\n",
        encoding="utf-8",
    )
    manifest = tev.compact_plan(cfg, now=NOW)  # cutoff 2026-07-02
    assert len(manifest["targets"]) == 1
    t = manifest["targets"][0]
    assert t["kind"] == tev.KIND_MONTH and t["period"] == "2026-06"
    assert t["source_periods"] == ["2026-06", "2026-06-10", "2026-06-20"]
    assert t["snapshot"]["2026-06-10"] == ["- A did x."]
    prompt = Path(t["prompt_path"]).read_text(encoding="utf-8")
    assert "- A did x." in prompt and "2026-06" in prompt
    # A re-plan rebuilds staging from scratch.
    stale = tev._staging_dir(cfg) / "stale.txt"
    stale.write_text("junk")
    tev.compact_plan(cfg, now=NOW)
    assert not stale.exists()


def test_compact_plan_stages_aged_year_and_defers_year_with_days(tmp_path):
    cfg, _ = _cfg(tmp_path)
    path = tev.events_path(cfg)
    path.parent.mkdir(parents=True)
    path.write_text(
        "## 2024\n- A old summary.\n\n"
        "## 2024-11\n- A did x in Nov.\n\n## 2024-12\n- B did y in Dec.\n\n"
        "## 2023-05\n- C ancient month.\n\n## 2023-05-10\n- C stray day.\n",
        encoding="utf-8",
    )
    manifest = tev.compact_plan(cfg, now=NOW)  # year cutoff 2025-06-27
    kinds = {(t["kind"], t["period"]) for t in manifest["targets"]}
    # 2024 folds (existing year section included); 2023 has a stray day →
    # the day folds to its month this sweep, the year waits for the next.
    assert (tev.KIND_YEAR, "2024") in kinds
    assert (tev.KIND_MONTH, "2023-05") in kinds
    assert (tev.KIND_YEAR, "2023") not in kinds
    year_target = next(t for t in manifest["targets"] if t["kind"] == tev.KIND_YEAR)
    assert year_target["source_periods"] == ["2024", "2024-11", "2024-12"]


def test_compact_plan_year_fold_without_existing_year_section(tmp_path):
    cfg, _ = _cfg(tmp_path)
    path = tev.events_path(cfg)
    path.parent.mkdir(parents=True)
    path.write_text("## 2024-03\n- A did spring work.\n", encoding="utf-8")
    manifest = tev.compact_plan(cfg, now=NOW)
    t = manifest["targets"][0]
    assert (t["kind"], t["period"]) == (tev.KIND_YEAR, "2024")
    assert t["source_periods"] == ["2024-03"]


def test_compact_plan_runs_backstop_and_reports_drops(tmp_path):
    cfg, _ = _tiny_cfg(tmp_path, max_length=250, overflow_limit=280)
    path = tev.events_path(cfg)
    path.parent.mkdir(parents=True)
    pad = "z" * 80
    path.write_text(
        f"## 2023\n- A ancient {pad}.\n\n## 2024\n- B old {pad}.\n\n"
        "## 2026-07-31\n- C fresh.\n",
        encoding="utf-8",
    )
    manifest = tev.compact_plan(cfg, now=NOW)
    assert "2023" in manifest["dropped_periods"]
    assert "2023" not in path.read_text(encoding="utf-8")


def test_compact_plan_backstop_skipped_when_locked(tmp_path):
    cfg, _ = _tiny_cfg(tmp_path, max_length=250, overflow_limit=280)
    path = tev.events_path(cfg)
    path.parent.mkdir(parents=True)
    pad = "z" * 80
    body = (
        f"## 2023\n- A ancient {pad}.\n\n## 2024\n- B old {pad}.\n"
    )
    path.write_text(body, encoding="utf-8")
    lock = tev.team_dir(cfg) / tev.LOCK_FILENAME
    lock.write_text(f"{os.getpid()} 0")
    manifest = tev.compact_plan(cfg, now=NOW)
    assert manifest["dropped_periods"] == []
    assert "2023" in path.read_text(encoding="utf-8")
    lock.unlink()


# ----- card parsing + trim ---------------------------------------------------


def test_parse_card_variants():
    good = "@@TEAM_EVENTS@@\n- A did x.\n\n- B did y.\n"
    assert tev._parse_card(good) == ["- A did x.", "- B did y."]
    for bad in (
        "", "   \n", "no marker\n- A did x.",
        "@@TEAM_EVENTS@@\n\n", "@@TEAM_EVENTS@@\n- ok.\nnot a bullet",
    ):
        with pytest.raises(tev.TeamEventsError):
            tev._parse_card(bad)


def test_trim_bullets_keeps_at_least_one():
    huge = "- " + "x" * 500
    kept, trimmed = tev._trim_bullets([huge, "- small."], 100)
    assert kept == [huge] and trimmed is True
    kept, trimmed = tev._trim_bullets(["- a.", "- b."], 1000)
    assert kept == ["- a.", "- b."] and trimmed is False


# ----- compact_apply ---------------------------------------------------------


def _plan_one_month(tmp_path, **over):
    cfg, cfg_path = _tiny_cfg(tmp_path, **over)
    path = tev.events_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "## 2026-06-10\n- A did x.\n\n## 2026-06-20\n- B did y.\n\n"
        "## 2026-07-31\n- C fresh work.\n",
        encoding="utf-8",
    )
    manifest = tev.compact_plan(cfg, now=NOW)
    assert len(manifest["targets"]) == 1
    return cfg, cfg_path, manifest["targets"][0]


def test_compact_apply_no_manifest_raises(tmp_path):
    cfg, _ = _cfg(tmp_path)
    with pytest.raises(FileNotFoundError):
        tev.compact_apply(cfg)


def test_compact_apply_skips_missing_card(tmp_path):
    cfg, _, t = _plan_one_month(tmp_path)
    report = tev.compact_apply(cfg)
    assert report["skipped_no_card"] == [t["key"]]
    assert report["applied"] == [] and report["still_over"] is False


def test_compact_apply_folds_month_and_keeps_post_plan_appends(tmp_path):
    cfg, _, t = _plan_one_month(tmp_path)
    # A bullet appended AFTER plan (old transcript swept late) must survive.
    tev.append_events(
        cfg, persona="D", day="2026-06-10", events=["late arrival"], now=NOW
    )
    Path(t["card_path"]).write_text(
        "@@TEAM_EVENTS@@\n- A did x; B did y.\n", encoding="utf-8"
    )
    report = tev.compact_apply(cfg)
    assert report["applied"] == [t["key"]]
    text = tev.events_path(cfg).read_text(encoding="utf-8")
    assert "## 2026-06\n" in text
    assert "## 2026-06-10" not in text and "## 2026-06-20" not in text
    assert "- A did x; B did y." in text
    assert "- D late arrival." in text          # snapshot survivor
    assert "- C fresh work." in text            # untouched section intact
    assert not Path(t["card_path"]).exists()
    assert not Path(t["prompt_path"]).exists()


def test_compact_apply_malformed_card_reported_and_kept(tmp_path):
    cfg, _, t = _plan_one_month(tmp_path)
    Path(t["card_path"]).write_text("no marker at all", encoding="utf-8")
    report = tev.compact_apply(cfg)
    assert report["malformed"][0]["key"] == t["key"]
    assert Path(t["card_path"]).exists()
    assert "## 2026-06-10" in tev.events_path(cfg).read_text(encoding="utf-8")


def test_compact_apply_trims_oversized_card(tmp_path):
    cfg, _, t = _plan_one_month(tmp_path, month_max_chars=40)
    Path(t["card_path"]).write_text(
        "@@TEAM_EVENTS@@\n- A did a very long list of things this month.\n"
        "- B also did quite a lot of long things.\n",
        encoding="utf-8",
    )
    report = tev.compact_apply(cfg)
    assert report["forced_trims"] == [t["key"]]
    text = tev.events_path(cfg).read_text(encoding="utf-8")
    assert "- B also did" not in text


def test_compact_apply_reports_still_over(tmp_path):
    cfg, _, t = _plan_one_month(tmp_path, max_length=60, overflow_limit=61)
    Path(t["card_path"]).write_text(
        "@@TEAM_EVENTS@@\n- A did x; B did y.\n", encoding="utf-8"
    )
    report = tev.compact_apply(cfg)
    assert report["still_over"] is True


# ----- CLI verbs -------------------------------------------------------------


def test_cli_team_events_plan_and_apply(tmp_path, capsys):
    cfg, cfg_path, t = _plan_one_month(tmp_path)
    # apply before any card → clean exit 0, skipped_no_card
    assert cli.main(
        ["--config", str(cfg_path), "team-events-compact-apply"]
    ) == 0
    capsys.readouterr()
    # plan verb re-stages and prints the manifest
    assert cli.main(
        ["--config", str(cfg_path), "team-events-compact-plan", "--now", NOW]
    ) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["targets"][0]["period"] == "2026-06"
    # malformed card → exit 1
    Path(manifest["targets"][0]["card_path"]).write_text("junk", encoding="utf-8")
    assert cli.main(
        ["--config", str(cfg_path), "team-events-compact-apply"]
    ) == 1
    capsys.readouterr()


def test_cli_team_events_apply_without_manifest_exits_2(tmp_path, capsys):
    _, cfg_path = _cfg(tmp_path)
    assert cli.main(
        ["--config", str(cfg_path), "team-events-compact-apply"]
    ) == 2
    assert "no team-events manifest" in capsys.readouterr().err


# ----- contract v3 parsing + executor + in-process ingest --------------------

_V3_EVENTS_BUNDLE = dedent("""\
    @@SKILLS@@
    NONE
    @@MUST_REMEMBER@@
    NONE
    @@TOPICS@@
    NONE
    @@TEAM_EVENTS@@
    EVENT: shipped the team event log
    EVENT: reviewed PR #9

    EVENT:
    """)


def test_parse_extraction_reads_events_and_skips_blank():
    c = lc.parse_extraction(_V3_EVENTS_BUNDLE, now=NOW, source="claude_code")
    assert c.team_events == ["shipped the team event log", "reviewed PR #9"]
    assert c.is_empty() is False
    assert c.total() == 0  # events are team-level, not store entries


def test_parse_extraction_event_continuation_line():
    c = lc.parse_extraction(
        "@@SKILLS@@\nNONE\n@@MUST_REMEMBER@@\nNONE\n@@TOPICS@@\nNONE\n"
        "@@TEAM_EVENTS@@\nEVENT: shipped the widget\n  and its tests\n",
        now=NOW, source="x",
    )
    assert c.team_events == ["shipped the widget and its tests"]


def test_parse_extraction_stray_line_before_first_event_ignored():
    c = lc.parse_extraction(
        "@@SKILLS@@\nNONE\n@@MUST_REMEMBER@@\nNONE\n@@TOPICS@@\nNONE\n"
        "@@TEAM_EVENTS@@\nstray preamble prose\nEVENT: shipped it\n",
        now=NOW, source="x",
    )
    assert c.team_events == ["shipped it"]


def test_candidates_empty_without_events():
    c = lc.parse_extraction(
        "@@SKILLS@@\nNONE\n@@MUST_REMEMBER@@\nNONE\n@@TOPICS@@\nNONE\n"
        "@@TEAM_EVENTS@@\nNONE\n",
        now=NOW, source="x",
    )
    assert c.is_empty() is True


def test_ingest_extraction_appends_dated_team_events(tmp_path):
    cfg, _ = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    result = ingest_extraction(
        store, cfg, conversation_uuid="c1", source="claude_code",
        bundle_text=_V3_EVENTS_BUNDLE, now=NOW, event_day="2026-07-15",
    )
    assert result.team_events_added == 2
    assert result.total_added == 0
    text = tev.events_path(cfg).read_text(encoding="utf-8")
    assert "## 2026-07-15" in text
    assert "- TestTiger shipped the team event log." in text


def test_ingest_extraction_event_day_defaults_to_now(tmp_path):
    cfg, _ = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    ingest_extraction(
        store, cfg, conversation_uuid="c1", source="claude_code",
        bundle_text=_V3_EVENTS_BUNDLE, now=NOW,
    )
    assert "## 2026-08-01" in tev.events_path(cfg).read_text(encoding="utf-8")


class _EventsOnlySummarizer(Summarizer):
    name = "events-only"
    version = "v1"

    def summarize(self, *, prompt: str, max_words: int) -> str:
        return _V3_EVENTS_BUNDLE


def test_extract_and_ingest_appends_team_events(tmp_path):
    from datetime import datetime, timezone

    from tigerharness.tiger_memory.sources.base import SourceRecord

    cfg, _ = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    rec = SourceRecord(
        conversation_uuid="11111111-1111-4111-8111-111111111111",
        source="claude_code", source_id="s1",
        first_event_at=datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc),
        last_event_at=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
        activity_mtime=0.0,
        content="user: hi\nassistant: shipped it\n",
        raw_path=tmp_path / "raw.jsonl",
    )
    added = lc.extract_and_ingest(
        cfg, store, _EventsOnlySummarizer(), rec, now=NOW
    )
    assert added["team_events"] == 2
    assert "## 2026-07-15" in tev.events_path(cfg).read_text(encoding="utf-8")


# ----- config block ----------------------------------------------------------


def test_team_events_config_defaults(tmp_path):
    cfg, _ = _cfg(tmp_path)
    te = cfg.memory.team_events
    assert te.enabled is True
    assert (te.recent_days, te.year_after_days) == (30, 400)
    assert (te.month_max_chars, te.year_max_chars) == (700, 1000)
    assert (te.max_length, te.overflow_limit) == (24000, 30000)
    assert cfg.memory_extract.team_event_words == 15


def test_team_events_config_overrides(tmp_path):
    cfg, _ = _cfg(
        tmp_path,
        "enabled: false\nrecent_days: 10\nyear_after_days: 20\n"
        "month_max_chars: 50\nyear_max_chars: 60\n"
        "max_length: 100\noverflow_limit: 200",
    )
    te = cfg.memory.team_events
    assert te.enabled is False and te.recent_days == 10
    assert te.year_after_days == 20 and te.max_length == 100


@pytest.mark.parametrize(
    "yaml_body, needle",
    [
        ("recent_days: -1", "recent_days"),
        ("recent_days: 50\nyear_after_days: 40", "year_after_days"),
        ("month_max_chars: 0", "month_max_chars"),
        ("year_max_chars: -5", "month_max_chars and year_max_chars"),
        ("max_length: 100\noverflow_limit: 100", "overflow_limit"),
        ("recent_days: lots", "must be an integer"),
    ],
)
def test_team_events_config_validation(tmp_path, yaml_body, needle):
    with pytest.raises(ConfigError, match=needle):
        _cfg(tmp_path, yaml_body)
