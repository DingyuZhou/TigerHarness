"""CI guard: no dangling docs references + INDEX routes every current doc.

Mirrors the skill-hash guard — a deterministic, git-free pytest. It fails when
a ``docs/<name>.md`` reference in code/tests/README/CONTRIBUTING points at a
repo-root doc that does not exist (the rot that let a moved-to-ADR doc linger
as a live source pointer), and when ``docs/INDEX.md`` stops routing a current
doc.

When you remove or rename a doc, repoint its citers in the SAME change; this
guard holds you to it. ``ALLOWLIST`` holds references that are intentionally
NOT repo-root docs (external Claude Agent SDK pages, the consumer-repo memory
doc, agent_sdk's own local docs, and tiger-memory test fixtures) — add to it
only for a genuinely external reference.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOC_REF_RE = re.compile(r"docs/[A-Za-z0-9_./-]+\.md")
_INDEX_LINK_RE = re.compile(r"\]\(([A-Za-z0-9_./-]+\.md)(?:#[\w-]+)?\)")

# References that legitimately do NOT resolve to a repo-root docs/ file.
ALLOWLIST: frozenset = frozenset({
    # consumer-repo doc (referenced from tiger_memory/__init__.py)
    "docs/019_sai_memory_system.md",
    # tiger-memory test fixtures: fake input paths, not real docs
    "docs/001_test.md", "docs/design.md", "docs/readme.md",
    # agent_sdk's own local docs (live under src/.../agent_sdk/docs/)
    "docs/agent_sdk_comparison.md", "docs/HANDOFF.md",
    # external Claude Agent SDK doc pages (cited in the SDK comparison prose)
    "docs/agents.md", "docs/tools.md", "docs/streaming.md",
    "docs/results.md", "docs/running_agents.md",
})


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for rel in ("src", "tests"):
        base = _REPO_ROOT / rel
        files += list(base.rglob("*.py"))
        files += list(base.rglob("*.md"))
    files.append(_REPO_ROOT / "README.md")
    files.append(_REPO_ROOT / "CONTRIBUTING.md")
    return files


def dangling_doc_refs(files, allowlist=ALLOWLIST) -> list:
    """Return ``[(file, ref)]`` for every ``docs/*.md`` reference that neither
    resolves at the repo root nor is allowlisted."""
    bad: list = []
    for f in files:
        text = Path(f).read_text(encoding="utf-8")
        for ref in sorted(set(_DOC_REF_RE.findall(text))):
            if ref in allowlist:
                continue
            if not (_REPO_ROOT / ref).exists():
                bad.append((str(f), ref))
    return bad


def _index_linked_md(index_text: str) -> set:
    """The .md link targets in INDEX.md, as basenames (anchors stripped)."""
    return {Path(m).name for m in _INDEX_LINK_RE.findall(index_text)}


def unrouted_docs(index_text: str, current_docs: set) -> set:
    """Current docs not linked from INDEX.md."""
    return current_docs - _index_linked_md(index_text)


def _current_top_level_docs() -> set:
    # top-level docs/*.md that should be routable, minus INDEX itself and the
    # overall.md redirect stub.
    return {
        p.name for p in (_REPO_ROOT / "docs").glob("*.md")
        if p.name not in {"INDEX.md", "overall.md"}
    }


class TestNoDanglingDocRefs:
    def test_repo_tree_has_no_dangling_doc_refs(self):
        bad = dangling_doc_refs(_scan_files())
        assert bad == [], (
            "dangling docs/*.md references found (repoint them, or add to "
            f"ALLOWLIST only if genuinely external): {bad}"
        )

    def test_guard_flags_a_simulated_dangling_ref(self, tmp_path):
        # build the fake ref dynamically so this test's own source does not
        # contain a literal dangling reference for the real scan to catch.
        ref = "docs/" + "does-not-exist-" + "xyz.md"
        f = tmp_path / "x.py"
        f.write_text(f"# see {ref}\n", encoding="utf-8")
        assert dangling_doc_refs([f]) == [(str(f), ref)]

    def test_allowlisted_ref_is_not_flagged(self, tmp_path):
        f = tmp_path / "y.py"
        f.write_text("see docs/019_sai_memory_system.md\n", encoding="utf-8")
        assert dangling_doc_refs([f]) == []


class TestIndexCoverage:
    def test_index_routes_every_current_doc(self):
        index = (_REPO_ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
        missing = unrouted_docs(index, _current_top_level_docs())
        assert not missing, f"docs not routed from INDEX.md: {sorted(missing)}"

    def test_index_links_resolve(self):
        index = (_REPO_ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
        for link in _INDEX_LINK_RE.findall(index):
            target = (_REPO_ROOT / "docs" / link).resolve()
            assert target.exists(), f"INDEX.md links a missing file: {link}"

    def test_unrouted_docs_detects_a_gap(self):
        # a doc absent from the INDEX link set is reported
        idx = "see [journal](journal.md)"
        assert unrouted_docs(idx, {"journal.md", "tiger-memory.md"}) == {
            "tiger-memory.md"
        }
