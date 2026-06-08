"""Tests for ``tigerharness.journal.worklog``: the per-turn,
persona-attributed worklog records that feed per-persona memory.

Coverage intent: render/parse round-trip (incl. against the real
yaml-backed ``tiger_memory.frontmatter`` parser, to prove the
hand-rolled yaml-free output is also valid standard YAML), atomic
write + sequence allocation, read-back, and the completion-check
inspection helpers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tigerharness.journal import worklog
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.worklog import WorklogEntry


TASK_ID = "20260608-demo-abcd1234"


@pytest.fixture()
def paths(tmp_path: Path) -> JournalPaths:
    return JournalPaths(tmp_path / "journal").ensure()


def _entry(**kw) -> WorklogEntry:
    base = dict(
        task_id=TASK_ID,
        persona="Rukawa",
        step="task-work",
        kind="task",
    )
    base.update(kw)
    return WorklogEntry(**base)


# ---------------------------------------------------------------------------
# paths accessor
# ---------------------------------------------------------------------------

class TestPathsWorklog:
    def test_active_and_archived(self, paths: JournalPaths):
        active = paths.worklog(TASK_ID)
        archived = paths.worklog(TASK_ID, archived=True)
        assert active == paths.task_dir(TASK_ID) / "worklog"
        assert archived == paths.task_dir(TASK_ID, archived=True) / "worklog"
        assert active != archived


# ---------------------------------------------------------------------------
# render / parse
# ---------------------------------------------------------------------------

class TestRenderParse:
    def test_render_omits_none_fields(self):
        text = worklog.render(_entry(role=None, verdict=None))
        assert "role:" not in text
        assert "verdict:" not in text
        assert 'task_id: "20260608-demo-abcd1234"' in text
        assert 'persona: "Rukawa"' in text

    def test_render_includes_set_optional_fields(self):
        text = worklog.render(
            _entry(role="Developer", verdict="APPROVE", objective="ship it")
        )
        assert 'role: "Developer"' in text
        assert 'verdict: "APPROVE"' in text
        assert 'objective: "ship it"' in text

    def test_render_body_gets_trailing_newline(self):
        text = worklog.render(_entry(body="line without newline"))
        assert text.endswith("line without newline\n")

    def test_render_empty_body(self):
        text = worklog.render(_entry(body=""))
        assert text.endswith("---\n")

    def test_roundtrip_via_module_parse(self):
        original = _entry(
            role="Developer",
            objective="fix a: b (with colon) and \"quotes\"",
            verdict="REVISE",
            started_at="2026-06-08T12:00:00Z",
            ended_at="2026-06-08T12:30:00Z",
            body="Did the thing.\nMultiple lines.\n",
        )
        fm, body = worklog.parse(worklog.render(original))
        assert fm["task_id"] == original.task_id
        assert fm["persona"] == original.persona
        assert fm["objective"] == original.objective
        assert fm["verdict"] == "REVISE"
        assert fm["started_at"] == "2026-06-08T12:00:00Z"
        assert body == original.body

    def test_roundtrip_unicode(self):
        original = _entry(objective="实现每个角色的记忆", body="记笔记")
        fm, body = worklog.parse(worklog.render(original))
        assert fm["objective"] == "实现每个角色的记忆"
        assert body == "记笔记\n"

    def test_output_is_valid_standard_yaml(self):
        """The hand-rolled yaml-free renderer must emit frontmatter that a
        real YAML parser reads identically -- otherwise the Phase 2
        memory adapter (which may use yaml) would disagree with the core
        completion-check (which uses our parser)."""
        tm_frontmatter = pytest.importorskip(
            "tigerharness.tiger_memory.frontmatter"
        )
        original = _entry(
            role="Developer",
            objective='tricky: value with "quotes", commas, and 漢字',
            verdict="APPROVE",
            reason="rescue",
            body="body text\n",
        )
        rendered = worklog.render(original)
        fm_ours, body_ours = worklog.parse(rendered)
        fm_yaml, body_yaml = tm_frontmatter.parse(rendered)
        assert fm_ours == fm_yaml
        assert body_ours == body_yaml

    def test_parse_no_frontmatter(self):
        fm, body = worklog.parse("just a body, no frontmatter\n")
        assert fm == {}
        assert body == "just a body, no frontmatter\n"

    def test_parse_empty_text(self):
        fm, body = worklog.parse("")
        assert fm == {}
        assert body == ""

    def test_parse_unterminated_frontmatter(self):
        fm, body = worklog.parse("---\ntask_id: \"x\"\nno closing delim\n")
        assert fm == {}
        assert body == "---\ntask_id: \"x\"\nno closing delim\n"

    def test_parse_skips_blank_and_colonless_lines(self):
        text = '---\n\nnocolon\ntask_id: "x"\n---\nbody\n'
        fm, body = worklog.parse(text)
        assert fm == {"task_id": "x"}
        assert body == "body\n"

    def test_parse_tolerates_single_quoted_scalar(self):
        fm, _ = worklog.parse("---\nobjective: 'single quoted'\n---\n")
        assert fm["objective"] == "single quoted"

    def test_parse_tolerates_bare_scalar(self):
        fm, _ = worklog.parse("---\nobjective: bareword\n---\n")
        assert fm["objective"] == "bareword"

    def test_parse_strips_one_leading_blank_line_from_body(self):
        # A blank line between the closing delim and the body is dropped
        # (mirrors tiger_memory.frontmatter), so the body starts at real
        # content.
        fm, body = worklog.parse('---\ntask_id: "x"\n---\n\nreal body\n')
        assert fm == {"task_id": "x"}
        assert body == "real body\n"


# ---------------------------------------------------------------------------
# write_entry / sequencing
# ---------------------------------------------------------------------------

class TestWriteEntry:
    def test_writes_and_stamps_seq_and_path(self, paths: JournalPaths):
        stamped = worklog.write_entry(paths, _entry())
        assert stamped.seq == 1
        assert stamped.path is not None
        assert stamped.path.name == "0001-rukawa-task-work.md"
        assert stamped.path.is_file()

    def test_sequence_increments(self, paths: JournalPaths):
        a = worklog.write_entry(paths, _entry(persona="Rukawa"))
        b = worklog.write_entry(paths, _entry(persona="Mitsui"))
        assert a.seq == 1
        assert b.seq == 2
        assert b.path.name == "0002-mitsui-task-work.md"

    def test_explicit_seq_respected(self, paths: JournalPaths):
        stamped = worklog.write_entry(paths, _entry(seq=42))
        assert stamped.seq == 42
        assert stamped.path.name == "0042-rukawa-task-work.md"

    def test_next_seq_on_missing_dir_is_one(self, tmp_path: Path):
        assert worklog._next_seq(tmp_path / "does-not-exist") == 1

    def test_next_seq_ignores_non_matching_files(self, paths: JournalPaths):
        wl = paths.worklog(TASK_ID)
        wl.mkdir(parents=True, exist_ok=True)
        (wl / "README.md").write_text("not a worklog entry\n")
        stamped = worklog.write_entry(paths, _entry())
        assert stamped.seq == 1

    def test_written_file_reads_back(self, paths: JournalPaths):
        worklog.write_entry(
            paths,
            _entry(
                kind="workflow",
                role="QA",
                step="critic-pass",
                objective="review",
                verdict="BLOCK",
                body="found a hole\n",
            ),
        )
        [entry] = worklog.list_entries(paths, TASK_ID)
        assert entry.kind == "workflow"
        assert entry.role == "QA"
        assert entry.step == "critic-pass"
        assert entry.verdict == "BLOCK"
        assert entry.body == "found a hole\n"
        assert entry.seq == 1


# ---------------------------------------------------------------------------
# read_entry
# ---------------------------------------------------------------------------

class TestReadEntry:
    def test_seq_from_filename(self, paths: JournalPaths):
        stamped = worklog.write_entry(paths, _entry())
        got = worklog.read_entry(stamped.path)
        assert got.seq == 1
        assert got.persona == "Rukawa"

    def test_no_seq_prefix_yields_none(self, tmp_path: Path):
        p = tmp_path / "notes.md"
        p.write_text('---\ntask_id: "x"\npersona: "Anzai"\n---\nhi\n')
        got = worklog.read_entry(p)
        assert got.seq is None
        assert got.persona == "Anzai"

    def test_defaults_when_fields_missing(self, tmp_path: Path):
        p = tmp_path / "0003-x-y.md"
        p.write_text("---\n---\nbody\n")
        got = worklog.read_entry(p)
        assert got.task_id == ""
        assert got.persona == ""
        assert got.kind == "task"
        assert got.role is None
        assert got.seq == 3


# ---------------------------------------------------------------------------
# list / inspect helpers
# ---------------------------------------------------------------------------

class TestInspectHelpers:
    def test_list_empty_when_no_dir(self, paths: JournalPaths):
        assert worklog.list_entries(paths, TASK_ID) == []

    def test_list_is_ordered_and_filters_noise(self, paths: JournalPaths):
        worklog.write_entry(paths, _entry(persona="Rukawa"))
        worklog.write_entry(paths, _entry(persona="Mitsui"))
        wl = paths.worklog(TASK_ID)
        # Noise the discovery must ignore: a non-md file, a non-seq md
        # file, and a subdirectory whose name matches the seq pattern.
        (wl / "scratch.txt").write_text("ignore me\n")
        (wl / "notes.md").write_text("---\n---\nignore\n")
        (wl / "0009-a-subdir").mkdir()
        entries = worklog.list_entries(paths, TASK_ID)
        assert [e.seq for e in entries] == [1, 2]
        assert [e.persona for e in entries] == ["Rukawa", "Mitsui"]

    def test_personas_and_steps_and_membership(self, paths: JournalPaths):
        worklog.write_entry(paths, _entry(persona="Rukawa", step="task-work"))
        worklog.write_entry(paths, _entry(persona="Mitsui", step="review"))
        assert worklog.personas_with_entries(paths, TASK_ID) == {
            "Rukawa",
            "Mitsui",
        }
        assert worklog.steps_with_entries(paths, TASK_ID) == {
            "task-work",
            "review",
        }
        assert worklog.has_entry_for_persona(paths, TASK_ID, "Rukawa")
        assert not worklog.has_entry_for_persona(paths, TASK_ID, "Sakuragi")

    def test_archived_entries_visible(self, paths: JournalPaths):
        worklog.write_entry(paths, _entry(), archived=True)
        assert worklog.personas_with_entries(
            paths, TASK_ID, archived=True
        ) == {"Rukawa"}
        # ...and not under active/
        assert worklog.list_entries(paths, TASK_ID) == []
