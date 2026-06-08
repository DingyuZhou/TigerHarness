"""Lightweight smoke test for the OPERATING.md template content.

The template is design copy. Tests pin only the load-bearing landmarks
(headings + the decision-procedure step numbers) so we catch an
accidental delete; they do NOT pin prose, which the design may evolve.
"""

from __future__ import annotations

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
