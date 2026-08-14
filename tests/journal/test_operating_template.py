"""Lightweight smoke test for the OPERATING.md template content.

The template is design copy. Tests pin only the load-bearing landmarks
(headings + the decision-procedure step numbers) so we catch an
accidental delete; they do NOT pin prose, which the design may evolve.
"""

from __future__ import annotations

import pytest

from tigerharness.journal.operating_template import OPERATING_MD


def test_template_is_nonempty_string():
    assert isinstance(OPERATING_MD, str)
    assert len(OPERATING_MD) > 500  # well above any accidental truncation


def test_required_headings_present():
    """If any of these go missing the protocol contract is broken."""
    for h in [
        "# OPERATING.md",
        "## Where state lives",
        "## How to read state",
        "## The decision procedure",
        "## Stop conditions",
        "## Heartbeat cadence",
        "## What NOT to do",
    ]:
        assert h in OPERATING_MD, f"missing required heading: {h!r}"


def test_decision_procedure_lists_six_steps():
    """The 6-step procedure (sweep, pick, read, work, on-stop, cascade)
    is the load-bearing contract. Any rename breaks the driver skill."""
    for marker in ["1. **Lazy sweep**", "2. **Pick exactly ONE",
                   "3. **Read context**", "4. **Work the task continuously**",
                   "5. **On stop**", "6. **Cascade"]:
        assert marker in OPERATING_MD, f"missing step marker: {marker!r}"


def test_soft_lease_concept_named():
    """The 'fresh in_progress = leave alone' rule must be in the
    on-disk protocol, not just in the design doc -- the driver reads
    OPERATING.md, not the design doc."""
    text = OPERATING_MD.lower()
    assert "soft lease" in text
    assert "fresh" in text
    assert "stale" in text


def test_pick_prioritises_finishing_before_starting():
    """Finish-before-start: an in_progress task is resumed before any
    new pending task begins (a later task may depend on the in-flight
    one). This is the load-bearing serial-queue contract."""
    text = OPERATING_MD.lower()
    assert "priority order" in text
    assert "resumable" in text


def test_attach_signal_claim_release_documented():
    """The instant-resume contract: session_ref is the attach signal,
    the idle/busy/crashed classification, the claim/release CLIs, and
    that a clean hand-off resumes with no wait."""
    text = OPERATING_MD.lower()
    assert "session_ref" in text
    for word in ("idle", "busy", "crashed"):
        assert word in text, f"missing classification term: {word!r}"
    assert "journal claim" in text
    assert "journal release" in text
    assert "immediately" in text


def test_continuity_contract_pinned():
    """The continuity rules (2026-06-08) are load-bearing — pin them so a
    future edit can't silently re-introduce one-session-per-loop-fire.

    1) a busy-only queue is a cheap no-op (don't read further); 2) cascade
    is a hard, same-turn loop, never one-per-fire; 3) "context heavy" is
    answered by compaction, not a hand-off; 4) the stuck-timeout is
    operator-configurable."""
    text = OPERATING_MD
    low = text.lower()
    assert "cheap no-op fast path" in low          # busy-only -> stop cheaply
    assert "one-session-per-loop-fire" in low      # the named anti-pattern
    assert "same turn" in low                      # cascade is same-turn
    assert "compaction" in low                     # compact, don't hand off
    assert "TIGERHARNESS_JOURNAL_STUCK_TIMEOUT" in text  # configurable


def test_finish_before_start_guard_pinned():
    """Priority branch (b): a *busy* `in_progress` task defers any `pending`
    pickup (finish-before-start). The lazy-loaded skill once dropped this
    (critique R2) -- pin it in the contract so it can't silently vanish here
    too, since the skill defers to OPERATING.md on conflict."""
    assert "do NOT start new" in OPERATING_MD       # busy -> defer pending
    assert "finish before any" in OPERATING_MD.lower()


def test_task_done_note_durable_record_guidance_pinned():
    """The cascade x compaction x per-persona-memory interaction: a
    `kind=task`'s only ingested memory is the single done-note, written at
    the end, so it must be assembled from the durable record (progress.md /
    artifacts) -- a long cascade may have compacted earlier sessions away,
    and tiger-memory ingests only worklog/, never progress.md. Pin this
    landmark so the guidance can't be silently dropped (critique R5)."""
    low = OPERATING_MD.lower()
    assert "durable record" in low
    assert "ingests only" in low   # ...worklog/ (never progress.md)


def test_current_template_not_in_prior_hash_manifest():
    """Maintenance footgun guard (mirror of the skill manifest test in
    test_init): the CURRENT OPERATING_MD must never be listed in
    scaffold._PRIOR_OPERATING_HASHES. The manifest records *prior* shipped
    templates (so an unmodified earlier ship refreshes); listing the
    current one stops propagation and is the 'appended the new hash instead
    of the old one' slip."""
    import hashlib
    from tigerharness.journal import scaffold
    current = hashlib.sha256(OPERATING_MD.encode("utf-8")).hexdigest()
    assert current not in scaffold._PRIOR_OPERATING_HASHES


def test_graph_walk_step_file_describes_both_halves():
    """A compiled step file has frontmatter AND a body -- the drafter's
    per-step instructions, landed below the closing `---`. Graph-walk
    step 1 must send the seat there.

    This test used to pin the opposite ("It carries no instructions."),
    which was accurate then: the parser dropped every body, so a seat
    that believed its step file briefed it read eleven lines of
    frontmatter and invented the rest. The channel exists now and the
    failure mode inverts -- a seat that skips the body reinvents work
    the compile already specified. Pin three things: the body is named,
    the seat is sent to it, and the surrounding sources stay named for
    the tasks compiled before bodies existed."""
    # Scope to step 1 itself: every source below is named elsewhere in
    # the document too, so a document-wide search would pass even after
    # step 1 stopped mentioning any of them.
    after = OPERATING_MD.split(
        "1. **Adopt the step's persona and read the step file.**", 1,
    )
    assert len(after) == 2, "graph-walk step 1 heading is gone"
    step1 = after[1].split("2. **End the turn at the gate", 1)[0]

    # Substrings only -- no line breaks or indentation, which would pin
    # prose wrapping this module deliberately leaves free.
    assert "Below the closing `---`" in step1
    assert "this step's per-run instructions" in step1
    # The pre-bodies fallback, which live journals still need.
    assert "compiled before bodies existed" in step1
    # The surrounding sources, named where the seat is told to look.
    # Names only -- the parentheticals wrap.
    for source in (
        "`playbook_snapshot.md`",
        "`task_brief.md`",
        "`artifacts/`",
        "`worklog/`",
    ):
        assert source in step1, f"graph-walk step 1 no longer names {source}"


@pytest.mark.parametrize(
    "pre_edit",
    [
        # pre step-body render (2026-08-14, this change)
        "5cd76431b04dbf9d1ebab7f54ed6c386099eca502f65d8e1f39461800190a5d8",
        # pre graph-walk-wording render (2026-08-14, the change before it)
        "41e3ddf1e9070ff476a7334b40e3513f54471e460f38e0eaf7d1e13f6ed41d97",
    ],
)
def test_pre_edit_render_registered_as_prior(pre_edit):
    """Each render shipped before a wording change is a copy some live
    journal carries on disk. Its sha256 must stay in
    scaffold._PRIOR_OPERATING_HASHES or that journal silently stops
    receiving protocol updates -- the refresh only happens for a byte
    match against a *registered* prior.

    This is the half of the propagation contract no other test covers:
    `test_operating_md_refreshed_when_unmodified_prior_ship` proves
    "registered => refreshed" against a monkeypatched manifest, and
    `test_operating_md_not_overwritten_when_present` proves
    "unregistered => untouched". Neither notices a missing entry in the
    real manifest, which is exactly the step most easily forgotten."""
    from tigerharness.journal import scaffold
    assert pre_edit in scaffold._PRIOR_OPERATING_HASHES


def test_per_persona_memory_gates_documented():
    """Per-persona memory protocol: the CLI gates that route each
    persona's worklog note exist, but they only fire if the driver
    learns to call them. Pin the landmarks so a doc edit can't silently
    drop the contract that activates Phases 1-3 in a live drive:

    - claim takes --driver (attribution + double-count suppression); the
      drive thread_ts flows automatically via TIGERHARNESS_SLACK_THREAD_TS,
      with --drive-thread retained as an explicit override;
    - a kind=task `done` requires --output (the note is the ticket);
    - a kind=workflow walk advances via `journal step-done`;
    - the worklog is named as the per-persona memory record.
    """
    text = OPERATING_MD
    assert "--driver" in text
    # The thread_ts is harness-supplied, not hand-copied -- pin both the
    # env-var transport and the override flag so an edit can't silently
    # revert to "paste the thread id by hand".
    assert "TIGERHARNESS_SLACK_THREAD_TS" in text
    assert "--drive-thread" in text
    assert "--output" in text
    assert "journal step-done" in text
    assert "[bridge-context]" in text
    assert "worklog" in text.lower()
    # The "note is the ticket" enforcement phrasing must survive edits --
    # it is the load-bearing mental model for the gates.
    assert "the ticket" in text.lower()


def test_materialize_is_not_a_turn_end_callout_present():
    """The deferred/materialize seam must explicitly say it is NOT a
    turn-end (the cascade-after-materialize fix). Regression-lock the
    load-bearing phrasing so a future edit can't silently re-open the
    "stop at the materialize seam" defect."""
    import pathlib
    import tigerharness.init as _init

    skill = (
        pathlib.Path(_init.__file__).parent
        / "_bundled_skills" / "drive-journal" / "SKILL.md"
    ).read_text(encoding="utf-8")
    for text in (OPERATING_MD, skill):
        low = text.lower()
        assert "materialize" in low
        # the no-stop callout, phrased either way
        assert "not a turn-end" in low or "not a turn end" in low
        assert "do not stop" in low or "do NOT stop".lower() in low
