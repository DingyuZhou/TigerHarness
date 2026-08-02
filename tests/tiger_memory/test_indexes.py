"""Tests for the skill/topic index + detail renderers (indexes.py, ADR 0007).

Pure functions — no config, no I/O. Covers the empty/populated branch of
every renderer, the deterministic orderings (importance desc + id for
skills; freshest-first + id tie-break for topics), and the detail
filenames.
"""
from __future__ import annotations

from tigerharness.tiger_memory import indexes
from tigerharness.tiger_memory.entries import SkillEntry, TopicEntry

NOW = "2026-07-23T12:00:00Z"


def _skill(
    name: str = "Skill",
    trigger: str = "when x",
    proc: str = "do y",
    *,
    importance: float = 0.0,
    usage: int = 0,
    last: str = NOW,
    id: str | None = None,
) -> SkillEntry:
    kw = {"id": id} if id else {}
    return SkillEntry(
        text=proc, created_at=NOW, last_used=last, source="t",
        name=name, trigger=trigger, procedure=proc,
        usage_count=usage, importance=importance, **kw,
    )


def _topic(
    name: str = "Topic",
    *,
    summary: str = "sum",
    last: str = NOW,
    touch: int = 1,
    text: str = "## 2026-07-01\n- a",
    id: str | None = None,
) -> TopicEntry:
    kw = {"id": id} if id else {}
    return TopicEntry(
        text=text, created_at=NOW, last_used=last, source="t",
        name=name, summary=summary, touch_count=touch, **kw,
    )


# ----- filenames -------------------------------------------------------------


def test_skill_detail_filename_slugs_name_and_appends_id():
    e = _skill("My Skill!", id="abc123def456")
    assert indexes.skill_detail_filename(e) == "my-skill-abc123def456.md"


def test_skill_detail_filename_unsluggable_name_falls_back():
    """A name with no sluggable characters (blocked at extraction since
    ADR 0007, but a pre-existing store may carry one) falls back to the
    bare `skill-<id>.md` shape instead of raising and bricking rebuild."""
    e = _skill("日本語", id="abc123def456")
    assert indexes.skill_detail_filename(e) == "skill-abc123def456.md"
    assert indexes.skill_detail_filename(
        _skill("!!!", id="feedc0ffee42")
    ) == "skill-feedc0ffee42.md"
    # The index renderer survives such an entry too (it embeds the filename).
    assert "skill-abc123def456.md" in indexes.render_skill_index([e])


def test_topic_detail_filename_is_slug():
    t = _topic("Deploy Pipeline")
    assert t.slug == "deploy-pipeline"
    assert indexes.topic_detail_filename(t) == "deploy-pipeline.md"


# ----- skill index ------------------------------------------------------------


def test_render_skill_index_empty():
    out = indexes.render_skill_index([])
    assert out.startswith("# Skill index")
    assert "_(no skills learned yet)_" in out
    assert out.endswith("\n")


def test_render_skill_index_orders_by_importance_desc_then_id():
    a = _skill("Alpha", importance=1.0, id="bbbbbbbbbbbb")
    b = _skill("Bravo", importance=2.0, id="cccccccccccc")
    c = _skill("Charlie", importance=1.0, id="aaaaaaaaaaaa")
    out = indexes.render_skill_index([a, b, c])
    # Highest importance first; equal importance ties break on id ascending.
    assert out.index("**Bravo**") < out.index("**Charlie**") < out.index("**Alpha**")


def test_render_skill_index_lines_and_detail_pointer():
    e = _skill("Fix Deploys", trigger="deploy fails", id="deadbeef0123")
    out = indexes.render_skill_index([e])
    assert "- **Fix Deploys** — deploy fails" in out
    assert "  ↳ `skills/fix-deploys-deadbeef0123.md`" in out
    assert "read the skill's detail file under `skills/`" in out


def test_render_skill_detail():
    e = _skill(
        "N", trigger="T", proc="P1\nP2\n", importance=1.5, usage=3,
        last="2026-07-01T09:00:00Z",
    )
    assert indexes.render_skill_detail(e) == (
        "# N\n\n"
        "- **When:** T\n"
        "- **Used:** 3× · importance 1.50 · last used 2026-07-01\n\n"
        "P1\nP2\n"
    )


# ----- topic index ------------------------------------------------------------


def test_render_topic_index_empty():
    out = indexes.render_topic_index([])
    assert out.startswith("# Topic index")
    assert "_(no topics yet)_" in out


def test_render_topic_index_freshest_first_with_id_tiebreak():
    tie_a = _topic("Tie A", last="2026-07-10T00:00:00Z", id="aaaaaaaaaaaa")
    newest = _topic("Newest", last="2026-07-20T00:00:00Z")
    tie_z = _topic("Tie Z", last="2026-07-10T00:00:00Z", id="zzzzzzzzzzzz")
    out = indexes.render_topic_index([tie_a, newest, tie_z])
    # Most recently touched first; equal last_used ties break on id (desc,
    # because the whole (last_used, id) key sorts reversed).
    assert out.index("**Newest**") < out.index("**Tie Z**") < out.index("**Tie A**")


def test_render_topic_index_block_content():
    t = _topic(
        "My Topic", summary="Sum here", last="2026-07-20T08:00:00Z", touch=4
    )
    out = indexes.render_topic_index([t])
    assert "- **My Topic** (`my-topic`) · last 2026-07-20 · 4×" in out
    assert "\n  Sum here" in out
    assert "read its detail file under `topics/<slug>.md`" in out


def test_render_topic_detail():
    t = _topic(
        "My Topic", summary="Sum here", last="2026-07-20T08:00:00Z", touch=4,
        text="## 2026-07-20\n- x\n",
    )
    assert indexes.render_topic_detail(t) == (
        "# My Topic (`my-topic`)\n\n"
        "_Last touched 2026-07-20 · 4× · Sum here_\n\n"
        "## 2026-07-20\n- x\n"
    )


# ----- topic routing list ------------------------------------------------------


def test_render_topic_routing_list_empty():
    assert indexes.render_topic_routing_list([]) == (
        "(no topics exist yet — every topic you emit will be NEW)"
    )


def test_render_topic_routing_list_populated_freshest_first():
    old = _topic("Old Topic", summary="old sum", last="2026-06-01T00:00:00Z")
    new = _topic("New Topic", summary="new sum", last="2026-07-20T00:00:00Z")
    out = indexes.render_topic_routing_list([old, new])
    assert "- `new-topic` — New Topic (last 2026-07-20): new sum" in out
    assert "- `old-topic` — Old Topic (last 2026-06-01): old sum" in out
    assert out.index("`new-topic`") < out.index("`old-topic`")
    # Compact: no trailing newline (embedded inside a prompt).
    assert not out.endswith("\n")
