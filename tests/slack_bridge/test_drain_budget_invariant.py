"""CI guard: the drain budget stays ordered, and the docs keep saying so.

The shutdown path spends one budget across three files that do not import
each other, so nothing but this test connects them::

    STOP_DRAIN_S + FINISH_POST_S  <=  _DRAIN_TIMEOUT_S  <=  TimeoutStopSec
         bridge.py (both)              __main__.py          gen_service.py

Edit any one of them in isolation and the failure is silent and remote: a
budget that exceeds its own systemd stop timeout means systemd SIGKILLs a
bridge that was draining correctly, and the reply the Operator is waiting
for dies with it. The margins are asserted as floors (>= 20s, >= 30s) and
not as ``> 0``, because one second of headroom is the same disaster
wearing a passing test.

**If this test just failed on a docs edit, that is the guard working.**
``docs/slack-bridge.md`` restates these numbers in prose four times, and
prose is where they rot first. ``b5-doc-draft`` is expected to add a fifth
site later in this same delivery -- which is why the count is a floor and
never an equality. Two rules keep the guard honest, and both are the sort
that fail *open* if you relax them:

* The 90s pattern is built from ``_DRAIN_TIMEOUT_S``, never typed. A typed
  ``90`` would make this file the eighth home for the number it exists to
  police.
* Each doc site is found by a **phrase**, never a line number: the header
  scrubber's own docs edit shifts every line number below it.

When a legitimate reword trips an anchor, repoint the anchor **in this
test, in the same change** -- the failure message names which one. Do not
loosen the pattern to make it pass; the loose version (a bare ``90``, or a
match-and-loop over an empty set) passes on a docs file that says nothing
at all.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from tigerharness.slack_bridge.__main__ import _DRAIN_TIMEOUT_S
from tigerharness.slack_bridge.bridge import (
    FINISH_POST_S,
    STOP_DRAIN_S,
    SlackBridge,
)
from tigerharness.slack_bridge.gen_service import render_systemd_unit

# tests/slack_bridge/<this file> -- so parents[2], not parents[1].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS = _REPO_ROOT / "docs" / "slack-bridge.md"

#: Anchored on the assignment's name, never on its value: that unit
#: template and that docs file both carry other 120s.
_TIMEOUT_STOP_RE = re.compile(r"TimeoutStopSec=(\d+)")

#: Every prose site in docs/slack-bridge.md that restates the drain
#: budget, keyed by a phrase unique to it. Line numbers are deliberately
#: absent -- this delivery's own docs edits move them.
_DOC_ANCHORS = (
    "in-flight dispatches to complete",
    "worst-case total wait is",
    "drain budget is shared across lanes",
    "in-flight turns to finish",
    # The fifth site this file's docstring anticipated, landed by
    # `b5-doc-draft`: the prose that spells the chain out. Anchored like
    # the rest, because an unpinned docs site is one that rots quietly --
    # exactly the failure this guard exists to catch.
    "is the middle link",
)

#: A floor, never an equality, and it counts the ANCHORED sites:
#: docs/slack-bridge.md carries the number on more lines than this, so a
#: reword that merges two sentences of one site must not fail here. The
#: fix for a drop is the docs site that lost the number, never a lower
#: floor.
_DOC_SITE_FLOOR = 5


def _rendered_timeout_stop_sec(tmp_path: Path) -> int:
    """Read TimeoutStopSec out of the unit `gen-service` actually writes."""
    unit = render_systemd_unit(
        teams_root=tmp_path,
        bridges_config=tmp_path / "slack-bridge.yaml",
        env_file=tmp_path / "multi-bridge.env",
        venv_python=tmp_path / ".venv" / "bin" / "python",
    )
    match = _TIMEOUT_STOP_RE.search(unit)
    assert match, (
        "the rendered systemd unit has no TimeoutStopSec= line. It is the "
        "outermost bound on the drain budget; if gen_service.py "
        "(src/tigerharness/slack_bridge/gen_service.py) stopped emitting "
        "it, systemd falls back to its default and this invariant is "
        "no longer enforced anywhere."
    )
    return int(match.group(1))


def test_drain_budget_constants_stay_ordered(tmp_path: Path) -> None:
    timeout_stop_sec = _rendered_timeout_stop_sec(tmp_path)
    inner = STOP_DRAIN_S + FINISH_POST_S

    sources = (
        f"STOP_DRAIN_S={STOP_DRAIN_S} + FINISH_POST_S={FINISH_POST_S} "
        f"(src/tigerharness/slack_bridge/bridge.py) = {inner}; "
        f"_DRAIN_TIMEOUT_S={_DRAIN_TIMEOUT_S} "
        "(src/tigerharness/slack_bridge/__main__.py); "
        f"TimeoutStopSec={timeout_stop_sec} (rendered from "
        "src/tigerharness/slack_bridge/gen_service.py)"
    )

    assert inner <= _DRAIN_TIMEOUT_S, (
        f"the reporter's teardown budget outgrew the drain budget. {sources}. "
        "A lane that spends longer closing its thread than the shutdown path "
        "allows it gets cut off mid-post."
    )
    assert _DRAIN_TIMEOUT_S <= timeout_stop_sec, (
        f"the drain budget outgrew systemd's stop timeout. {sources}. "
        "systemd will SIGKILL a bridge that is draining correctly, and the "
        "in-flight reply dies with the process."
    )
    assert _DRAIN_TIMEOUT_S - inner >= 20, (
        f"under 20s of headroom between teardown and drain. {sources}. "
        "This is a floor and not `> 0` on purpose: one second of slack is "
        "the wedge case dressed up as a pass."
    )
    assert timeout_stop_sec - _DRAIN_TIMEOUT_S >= 30, (
        f"under 30s of headroom between drain and TimeoutStopSec. {sources}. "
        "The drain has to finish AND the process has to exit inside that "
        "gap; notify.py's urlopen(timeout=30) is the last post it may owe."
    )


def test_docs_still_state_the_drain_budget() -> None:
    # No skip guard: a missing docs file is a failure, not a reason to
    # pass. read_text() raising is the correct outcome.
    lines = _DOCS.read_text(encoding="utf-8").splitlines()

    # Integrality FIRST -- int() truncates, so 90.5 would build a `90`
    # pattern and green against docs that still say 90.
    assert _DRAIN_TIMEOUT_S == int(_DRAIN_TIMEOUT_S), (
        f"_DRAIN_TIMEOUT_S is {_DRAIN_TIMEOUT_S}, not a whole number of "
        "seconds, and the pattern below is built with int(). A fractional "
        "budget needs docs/slack-bridge.md and this test moved together: "
        "decide how the docs spell it, then teach this test that spelling."
    )
    # \s* is load-bearing: one site writes "90 s" with a space, and it is
    # the only one carrying both ends of the invariant.
    budget_re = re.compile(rf"{int(_DRAIN_TIMEOUT_S)}\s*s")

    carrying = [line for line in lines if budget_re.search(line)]
    assert len(carrying) >= _DOC_SITE_FLOOR, (
        f"docs/slack-bridge.md states the {int(_DRAIN_TIMEOUT_S)}s drain "
        f"budget on {len(carrying)} line(s); at least {_DOC_SITE_FLOOR} are "
        "expected. This is a floor, so the fix is a docs site that lost the "
        "number, never lowering the floor."
    )

    for anchor in _DOC_ANCHORS:
        hits = [line for line in carrying if anchor in line]
        assert hits, (
            f"docs anchor {anchor!r} no longer names the "
            f"{int(_DRAIN_TIMEOUT_S)}s drain budget in "
            "docs/slack-bridge.md. If that sentence was legitimately "
            "reworded, repoint this anchor in _DOC_ANCHORS in THIS FILE, "
            "in the same change -- do not delete it and do not widen the "
            "pattern."
        )


def test_docs_still_state_the_systemd_stop_timeout(tmp_path: Path) -> None:
    lines = _DOCS.read_text(encoding="utf-8").splitlines()
    timeout_stop_sec = _rendered_timeout_stop_sec(tmp_path)

    # Anchored on `TimeoutStopSec=`, never on 120: this file holds four
    # 120s and three of them are HEADER_MAX and --min-quiet-seconds.
    documented = [
        int(match.group(1))
        for match in (_TIMEOUT_STOP_RE.search(line) for line in lines)
        if match
    ]
    assert documented, (
        "docs/slack-bridge.md no longer mentions TimeoutStopSec= at all. "
        "It is the outer bound of the drain invariant and the one number "
        "an operator has to reconcile against their installed unit."
    )
    assert all(value == timeout_stop_sec for value in documented), (
        f"docs/slack-bridge.md documents TimeoutStopSec={documented} but "
        f"gen_service.py renders {timeout_stop_sec}. Reaching an "
        "already-installed unit needs a `gen-service` re-run, so a doc that "
        "disagrees with the template is worse than no doc."
    )


def test_wait_for_drain_has_no_default_timeout() -> None:
    """The old ``= 120.0`` default silently equalled TimeoutStopSec.

    A bare ``wait_for_drain()`` would then drain for exactly as long as
    systemd waits before SIGKILL -- no headroom, and no error. The caller
    passes ``_DRAIN_TIMEOUT_S``; making the parameter required is what
    keeps a second caller from re-acquiring the old number by accident.
    """
    default = inspect.signature(SlackBridge.wait_for_drain).parameters[
        "timeout"
    ].default
    assert default is inspect.Parameter.empty, (
        f"wait_for_drain's timeout default is back ({default!r}). It must "
        "stay required: src/tigerharness/slack_bridge/__main__.py passes "
        "_DRAIN_TIMEOUT_S explicitly, and a default re-introduces a fourth "
        "home for a number this test exists to keep in three."
    )
