"""Operator read/fix loop (inspect_tools.py + the search/forget/doctor CLI).

Covers the three operator verbs end to end — substring search across the
stores + team event log (persona and team scope), audited operator-authority
forget (sidecar + briefing refresh + the operator_explicit bypass), the
team-wide doctor health table (healthy vs flagged, human + JSON, exit
codes) — plus the ``.last-sweep-report.json`` persistence hooks written by
``compact-apply`` / ``ingest-staged`` and consumed by doctor.

The team fixture mirrors ``test_cli_sweep``'s layout, but with REAL persona
configs — search ``--team`` and doctor ``load_config`` each one:

    <tmp>/configs/personas.yaml
    <tmp>/mem/<name>/tiger-memory.config.yaml   (store root = <tmp>/mem/<name>)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import (
    cli,
    compaction,
    inspect_tools as it,
    sweep,
    team_events as tev,
)
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.briefing import rebuild_briefing
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.cursor import Cursor, save_cursor
from tigerharness.tiger_memory.entries import (
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    STORE_TOPICS,
    MustRememberEntry,
    SkillEntry,
    TopicEntry,
)
from tigerharness.tiger_memory.store import Store

NOW = "2026-08-01T12:00:00+00:00"


def _team(
    tmp_path: Path,
    personas: tuple[str, ...] = ("anzai", "ayako"),
    memory_yaml: str = "",
) -> dict[str, Path]:
    """Team layout with real, loadable per-persona configs. Returns
    ``{name: config_path}``; team_memories_dir = ``<tmp>/mem``."""
    mem = tmp_path / "mem"
    (tmp_path / "configs").mkdir(exist_ok=True)
    roster = "\n".join(f"  - name: {n}" for n in personas)
    (tmp_path / "configs" / "personas.yaml").write_text(
        f"personas:\n{roster}\n"
    )
    paths: dict[str, Path] = {}
    for name in personas:
        pdir = mem / name
        pdir.mkdir(parents=True, exist_ok=True)
        cfg_path = pdir / "tiger-memory.config.yaml"
        cfg_path.write_text(dedent(f"""\
            agent: {{name: {name}, role: persona}}
            store: {{root: {pdir}}}
            sources:
              - kind: claude_code
                project_path: {tmp_path}/proj/
            summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
            rebuild:
              lock_path: {pdir}/.rebuild.lock
        """) + memory_yaml)
        paths[name] = cfg_path
    return paths


def _bstore(cfg_path: Path):
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store, BoundedStore(cfg, store)


def _mr(text: str, kind: str = "preference") -> MustRememberEntry:
    return MustRememberEntry(
        text=text, created_at=NOW, last_used=NOW, source="test", kind=kind
    )


def _skill(
    procedure: str, name: str = "Git add", trigger: str = "committing"
) -> SkillEntry:
    return SkillEntry(
        text=procedure, created_at=NOW, last_used=NOW, source="test",
        name=name, trigger=trigger, procedure=procedure,
    )


def _topic(
    name: str,
    summary: str = "a topic summary",
    text: str = "## 2026-07-01\n- a detail line\n",
) -> TopicEntry:
    return TopicEntry(
        text=text, created_at=NOW, last_used=NOW, source="test",
        name=name, summary=summary,
    )


# ----- search ---------------------------------------------------------------


def test_search_hits_all_stores_and_events(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    cfg, _, bstore = _bstore(paths["anzai"])
    bstore.save_atomic(
        STORE_SKILLS, [_skill(procedure="always use tigerbolt lens")]
    )
    bstore.save_atomic(
        STORE_MUST_REMEMBER, [_mr("never push tigerbolt to main")]
    )
    bstore.save_atomic(STORE_TOPICS, [_topic(name="Tigerbolt Migration")])
    tev.append_events(
        cfg, persona="anzai", day="2026-07-30",
        events=["shipped tigerbolt v2"], now=NOW,
    )
    # Case-insensitive: the query casing differs from every stored form.
    assert cli.main(
        ["--config", str(paths["anzai"]), "search", "TigerBolt"]
    ) == 0
    lines = capsys.readouterr().out.strip().split("\n")
    assert lines[-1] == "4 match(es)"
    skill_id = bstore.load(STORE_SKILLS)[0].id
    mr_id = bstore.load(STORE_MUST_REMEMBER)[0].id
    assert f"anzai  skills  {skill_id}  always use tigerbolt lens" in lines
    assert (
        f"anzai  must_remember  {mr_id}  never push tigerbolt to main" in lines
    )
    # Topics are addressed by slug; the first matching field (name) is shown.
    assert "anzai  topics  tigerbolt-migration  Tigerbolt Migration" in lines
    assert "team  events  2026-07-30  - anzai shipped tigerbolt v2." in lines


def test_search_trims_long_matching_line(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    _, _, bstore = _bstore(paths["anzai"])
    bstore.save_atomic(STORE_MUST_REMEMBER, [_mr("needle " + "x" * 150)])
    assert cli.main(
        ["--config", str(paths["anzai"]), "search", "needle"]
    ) == 0
    lines = capsys.readouterr().out.strip().split("\n")
    hit = next(line for line in lines if "must_remember" in line)
    shown = hit.split("  ", 3)[3]
    assert len(shown) == 120 and shown.endswith("…")


def test_search_store_filter(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    cfg, _, bstore = _bstore(paths["anzai"])
    bstore.save_atomic(STORE_TOPICS, [_topic(name="Tigerbolt Migration")])
    tev.append_events(
        cfg, persona="anzai", day="2026-07-30",
        events=["shipped tigerbolt v2"], now=NOW,
    )
    # --store topics: the event log is NOT searched.
    assert cli.main(
        ["--config", str(paths["anzai"]), "search", "tigerbolt",
         "--store", "topics"]
    ) == 0
    lines = capsys.readouterr().out.strip().split("\n")
    assert lines[-1] == "1 match(es)"
    assert lines[0].startswith("anzai  topics  ")
    # --store events: the persona stores are NOT searched.
    assert cli.main(
        ["--config", str(paths["anzai"]), "search", "tigerbolt",
         "--store", "events"]
    ) == 0
    lines = capsys.readouterr().out.strip().split("\n")
    assert lines[-1] == "1 match(es)"
    assert lines[0].startswith("team  events  2026-07-30  ")


def test_search_team_covers_every_persona(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path)
    for name in ("anzai", "ayako"):
        _, _, bstore = _bstore(paths[name])
        bstore.save_atomic(
            STORE_MUST_REMEMBER, [_mr(f"{name} scouted kainan")]
        )
    assert cli.main(
        ["--config", str(paths["anzai"]), "search", "kainan", "--team"]
    ) == 0
    lines = capsys.readouterr().out.strip().split("\n")
    assert lines[-1] == "2 match(es)"
    assert any(line.startswith("anzai  ") for line in lines)
    assert any(line.startswith("ayako  ") for line in lines)


def test_search_team_skips_malformed_persona_config(
    tmp_path: Path, capsys, caplog
) -> None:
    paths = _team(tmp_path)
    _, _, bstore = _bstore(paths["anzai"])
    bstore.save_atomic(STORE_MUST_REMEMBER, [_mr("kainan game tape")])
    paths["ayako"].write_text("agent: [broken")
    with caplog.at_level(
        logging.WARNING, logger="tigerharness.tiger_memory.inspect_tools"
    ):
        rc = cli.main(
            ["--config", str(paths["anzai"]), "search", "kainan", "--team"]
        )
    assert rc == 0
    assert "1 match(es)" in capsys.readouterr().out
    assert "skipping persona ayako" in caplog.text


def test_search_no_match_is_still_exit_0(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    cfg, _, bstore = _bstore(paths["anzai"])
    # Populated stores + event log that simply don't contain the term.
    bstore.save_atomic(STORE_MUST_REMEMBER, [_mr("something unrelated")])
    tev.append_events(
        cfg, persona="anzai", day="2026-07-30", events=["did y"], now=NOW
    )
    assert cli.main(
        ["--config", str(paths["anzai"]), "search", "zilch"]
    ) == 0
    assert capsys.readouterr().out.strip() == "0 match(es)"


# ----- forget ---------------------------------------------------------------


def test_forget_by_id_audits_and_refreshes_briefing(
    tmp_path: Path, capsys
) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    cfg, store, bstore = _bstore(paths["anzai"])
    victim = _mr("never push to main")
    keep = _mr("always run tests")
    bstore.save_atomic(STORE_MUST_REMEMBER, [victim, keep])
    rebuild_briefing(cfg, store)
    briefing_mr = store.paths.briefing / "must_remember.md"
    assert "never push to main" in briefing_mr.read_text(encoding="utf-8")

    rc = cli.main(
        ["--config", str(paths["anzai"]), "forget",
         "--store", "must_remember", "--id", victim.id]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert f"forgot must_remember {victim.id}: never push to main" in out
    assert [e.id for e in bstore.load(STORE_MUST_REMEMBER)] == [keep.id]
    # No silent loss: the removed entry landed in the audit sidecar first.
    sidecar = store.paths.journal / "must_remember.forgotten.md"
    audit = sidecar.read_text(encoding="utf-8")
    assert "<!-- forgotten " in audit
    assert victim.id in audit and "never push to main" in audit
    # The read surface updated immediately.
    refreshed = briefing_mr.read_text(encoding="utf-8")
    assert "never push to main" not in refreshed
    assert "always run tests" in refreshed


def test_forget_by_slug_appends_audit_blocks(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    _, store, bstore = _bstore(paths["anzai"])
    long_text = "## 2026-07-01\n" + "detail " * 30
    bstore.save_atomic(
        STORE_TOPICS,
        [_topic(name="Alpha Topic", text=long_text), _topic(name="Beta Topic")],
    )
    rc = cli.main(
        ["--config", str(paths["anzai"]), "forget",
         "--store", "topics", "--slug", "alpha-topic"]
    )
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out.startswith("forgot topics alpha-topic: ")
    head = out.split(": ", 1)[1]
    assert len(head) == 80 and head.endswith("…")  # long text is elided
    rc = cli.main(
        ["--config", str(paths["anzai"]), "forget",
         "--store", "topics", "--slug", "beta-topic"]
    )
    assert rc == 0
    capsys.readouterr()
    audit = (store.paths.journal / "topics.forgotten.md").read_text(
        encoding="utf-8"
    )
    assert audit.count("<!-- forgotten ") == 2  # appended, never overwritten
    assert "Alpha Topic" in audit and "Beta Topic" in audit
    assert bstore.load(STORE_TOPICS) == []


def test_forget_operator_explicit_warns_but_removes(
    tmp_path: Path, capsys, caplog
) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    _, _, bstore = _bstore(paths["anzai"])
    victim = _mr("protected directive", kind="operator_explicit")
    bstore.save_atomic(STORE_MUST_REMEMBER, [victim])
    with caplog.at_level(
        logging.WARNING, logger="tigerharness.tiger_memory.inspect_tools"
    ):
        rc = cli.main(
            ["--config", str(paths["anzai"]), "forget",
             "--store", "must_remember", "--id", victim.id]
        )
    assert rc == 0
    capsys.readouterr()
    # The compaction forget-guard is deliberately bypassed — but loudly.
    assert "operator authority" in caplog.text
    assert bstore.load(STORE_MUST_REMEMBER) == []


def test_forget_no_match_exits_1(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    _bstore(paths["anzai"])
    rc = cli.main(
        ["--config", str(paths["anzai"]), "forget",
         "--store", "must_remember", "--id", "nope"]
    )
    assert rc == 1
    assert "no must_remember entry matches 'nope'" in capsys.readouterr().err
    rc = cli.main(
        ["--config", str(paths["anzai"]), "forget",
         "--store", "topics", "--slug", "nope-slug"]
    )
    assert rc == 1
    assert "'nope-slug'" in capsys.readouterr().err


def test_forget_slug_outside_topics_exits_2(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    rc = cli.main(
        ["--config", str(paths["anzai"]), "forget",
         "--store", "skills", "--slug", "x"]
    )
    assert rc == 2
    assert "--slug only addresses the topics store" in capsys.readouterr().err


def test_forget_bad_flag_combos_argparse_exit_2(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    base = ["--config", str(paths["anzai"]), "forget", "--store", "topics"]
    with pytest.raises(SystemExit) as exc:
        cli.main(base + ["--id", "a", "--slug", "b"])  # both refs
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(base)  # neither ref
    assert exc.value.code == 2
    capsys.readouterr()


def test_forget_locked_store_exits_1(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    _, store, bstore = _bstore(paths["anzai"])
    victim = _mr("memo under a held lock")
    bstore.save_atomic(STORE_MUST_REMEMBER, [victim])
    # Our own live pid holds the lock; skip the retry sleeps for speed.
    (store.paths.journal / ".must_remember.lock").write_text(
        f"{os.getpid()} 0"
    )
    import tigerharness.tiger_memory.bounded_store as bs_mod
    monkeypatch.setattr(bs_mod.time, "sleep", lambda s: None)
    rc = cli.main(
        ["--config", str(paths["anzai"]), "forget",
         "--store", "must_remember", "--id", victim.id]
    )
    assert rc == 1
    assert "store locked" in capsys.readouterr().err
    assert len(bstore.load(STORE_MUST_REMEMBER)) == 1  # untouched


# ----- doctor ---------------------------------------------------------------


def test_doctor_healthy_exits_0(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path)
    mem = tmp_path / "mem"
    for name, cfg_path in paths.items():
        cfg, store, _ = _bstore(cfg_path)
        rebuild_briefing(cfg, store)
        sweep.record_persona_done(mem, name)
    assert sweep.mark_sweep_complete(
        mem, datetime(2026, 8, 1, tzinfo=timezone.utc)
    )
    cfg, store, _ = _bstore(paths["anzai"])
    # Two cursors: data-through is the freshest last_event_at.
    save_cursor(store, "c1", Cursor("2026-07-30T10:00:00+00:00", 3))
    save_cursor(store, "c2", Cursor("2026-07-29T09:00:00+00:00", 1))
    tev.append_events(
        cfg, persona="anzai", day="2026-07-30", events=["did x"], now=NOW
    )
    rc = cli.main(["--config", str(paths["anzai"]), "doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "FLAGS: none" in out
    assert "PERSONA" in out and "DATA_THROUGH" in out
    assert "2026-07-30T10:00" in out           # anzai's data-through cell
    assert "last_sweep_at: 2026-08-01" in out
    assert "claim_held: no" in out
    assert "event log: 1 section(s)" in out


def test_doctor_flagged_exits_1(tmp_path: Path, capsys) -> None:
    tiny = (
        "memory:\n"
        "  must_remember:\n"
        "    max_length: 60\n"
        "    overflow_limit: 80\n"
    )
    paths = _team(tmp_path, memory_yaml=tiny)
    mem = tmp_path / "mem"
    _, store_a, bs_a = _bstore(paths["anzai"])
    bs_a.save_atomic(STORE_MUST_REMEMBER, [_mr("x" * 120)])  # over overflow
    bs_a.save_atomic(
        STORE_TOPICS, [_topic(name="Topic Store"), _topic(name="Unique Thing")]
    )
    (store_a.paths.journal / "topics.rejected.md").write_text("bad block\n")
    staging = store_a.root / ".sweep-staging"
    staging.mkdir()
    (staging / "manifest.json").write_text("{}")   # excluded from the count
    (staging / "u1.prompt.md").write_text("p")
    compact_staging = store_a.root / ".compact-staging"
    compact_staging.mkdir()
    (compact_staging / "skills.prompt.md").write_text("p")
    it.record_sweep_report(
        store_a, "compact_apply", {"still_over": ["must_remember"]}
    )
    # ayako: a normalized-equal topic slug ("topicstore" vs "topic-store").
    _, _, bs_b = _bstore(paths["ayako"])
    bs_b.save_atomic(STORE_TOPICS, [_topic(name="TopicStore")])
    # A live claim mid-run, one persona done, the other never swept.
    assert sweep.try_claim_sweep(
        mem, now=datetime(2026, 8, 1, tzinfo=timezone.utc), token="T"
    ).claimed
    sweep.record_persona_done(mem, "anzai")

    rc = cli.main(["--config", str(paths["anzai"]), "doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FLAGS:" in out
    assert "anzai: must_remember over_overflow (120 chars, max 60)" in out
    assert "120/60!" in out                     # over marker in the table cell
    assert "anzai: rejected file(s): topics.rejected.md" in out
    assert "ayako: never swept" in out
    assert "anzai: last compact-apply left still_over: must_remember" in out
    assert "briefing missing" in out
    assert (
        "topic slug collision: topic-store/topicstore across anzai, ayako"
        in out
    )
    assert "claim_held: yes (progress: anzai)" in out
    assert "last_sweep_at: never" in out
    assert "1+1" in out                         # staged counts, manifest excluded


def test_doctor_flags_malformed_persona_config(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path)
    cfg, store, _ = _bstore(paths["anzai"])
    rebuild_briefing(cfg, store)
    sweep.record_persona_done(tmp_path / "mem", "anzai")
    paths["ayako"].write_text("agent: [broken")
    rc = cli.main(["--config", str(paths["anzai"]), "doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "ayako: config error" in out
    table = out.split("\nTEAM:")[0]
    assert "ayako" not in table  # a broken persona gets a flag, not a row


# ----- report persistence (compact-apply / ingest-staged → doctor) ----------


_NONE_BUNDLE = dedent("""\
    @@SKILLS@@
    NONE
    @@MUST_REMEMBER@@
    NONE
    @@TOPICS@@
    NONE
    @@TEAM_EVENTS@@
    NONE
""")


def test_report_persistence_and_doctor_json(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    cfg, store, _ = _bstore(paths["anzai"])
    # compact-apply records its outcome (empty targets → clean apply).
    compaction.compact_plan(cfg, store, now=NOW)
    compaction.compact_apply(cfg, store, now=NOW)
    data = json.loads(it.sweep_report_path(store).read_text(encoding="utf-8"))
    assert data["compact_apply"]["applied"] == []
    assert data["compact_apply"]["at"]
    # ingest-staged records its summary ALONGSIDE (merge, not clobber).
    staging = store.root / ".sweep-staging"
    staging.mkdir(exist_ok=True)
    (staging / "manifest.json").write_text(json.dumps(
        {"items": [{"conversation_uuid": "u1", "source": "claude_code"}]}
    ))
    (staging / "u1.extract.md").write_text(_NONE_BUNDLE)
    assert cli.main(["--config", str(paths["anzai"]), "ingest-staged"]) == 0
    capsys.readouterr()
    data = json.loads(it.sweep_report_path(store).read_text(encoding="utf-8"))
    assert data["ingest"]["ingested"] == 1 and data["ingest"]["at"]
    assert "compact_apply" in data
    # doctor --json surfaces both persisted outcomes.
    rc = cli.main(["--config", str(paths["anzai"]), "doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1  # briefing missing / never swept are still flagged
    persona = payload["personas"][0]
    assert persona["persona"] == "anzai"
    assert persona["last_ingest"]["ingested"] == 1
    assert persona["last_compact_apply"]["applied"] == []
    assert payload["team"]["event_log_sections"] == 0
    assert payload["flags"]


def test_sweep_report_tolerant_reads(tmp_path: Path) -> None:
    paths = _team(tmp_path, personas=("anzai",))
    _, store, _ = _bstore(paths["anzai"])
    path = it.sweep_report_path(store)
    assert it.load_sweep_report(store) == {}       # missing file
    path.write_text("{not json")
    assert it.load_sweep_report(store) == {}       # corrupt JSON
    path.write_text("[1, 2]")
    assert it.load_sweep_report(store) == {}       # not a dict
    it.record_sweep_report(store, "ingest", {"ingested": 0})
    assert it.load_sweep_report(store)["ingest"]["ingested"] == 0
