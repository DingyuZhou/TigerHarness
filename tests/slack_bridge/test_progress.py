"""Tests for ``slack_bridge.progress`` — the turn-progress substrate.

Rows map onto the plan's acceptance criteria; the criterion id is in
each test's name so review is a grep. Criteria that assert what
``_dispatch`` *passes* (3b, 5 mode 4, 6, 6a, 6d, 6e(c), 6f, 7b(b),
7c(b), 7c(c)) belong to the bridge-wiring seat and live with it.

Every test drives a millisecond ``interval_s`` through the constructor
seam and a fake clock for measurement — the two are different jobs
(cadence runs on real loop time, elapsed/stall render off ``clock``), so
a test that conflates them either renders ``0m`` on every line or hangs
forever waiting for a fake clock to wake an event.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from tigerharness.agent_sdk.types import (
    ErrorEvent,
    RunStart,
    ToolCall,
    ToolResult,
    ToolOutput,
)
from tigerharness.slack_bridge import progress as progress_mod
from tigerharness.slack_bridge.progress import (
    CHANNEL_ENV_VARS,
    DEFAULT_INTERVAL_S,
    TurnProgress,
    build_turn_progress,
    resolve_progress_channel,
    sanitize_header,
    tool_hint,
)


PROGRESS_LOGGER = "tigerharness.slack_bridge.progress"
TICK = 0.002


# ---------- Fakes ----------------------------------------------------------

class _FakeNotifier:
    """Records ``(text, channel, thread_ts)`` for every post.

    ``post_text`` is what the real ``SlackNotifier`` exposes: it returns
    the message ``ts`` or ``None`` — failure is a falsy return, not an
    exception — so both failure shapes are configurable here.
    """

    def __init__(
        self,
        *,
        ts: str | None = "parent-ts",
        raises: bool = False,
        block: threading.Event | None = None,
    ) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self._ts = ts
        self._raises = raises
        self._block = block
        self._n = 0

    def post_text(
        self,
        text: str,
        *,
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> str | None:
        self.calls.append((text, channel, thread_ts))
        self._n += 1
        if self._block is not None and self._n == 1:
            self._block.wait(timeout=5.0)
        if self._raises:
            raise RuntimeError("slack transport exploded")
        return self._ts

    @property
    def texts(self) -> list[str]:
        return [call[0] for call in self.calls]


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make(
    notifier: _FakeNotifier | None = None,
    *,
    channel: str | None = "C-OPS",
    header: str = "refresh the knowledge index",
    interval_s: float = TICK,
    clock: _FakeClock | None = None,
) -> tuple[TurnProgress, _FakeNotifier | None, _FakeClock]:
    clk = clock or _FakeClock()
    return (
        TurnProgress(
            notifier,  # type: ignore[arg-type]
            channel,
            header=header,
            interval_s=interval_s,
            clock=clk,
        ),
        notifier,
        clk,
    )


async def _run_until(
    reporter: TurnProgress,
    notifier: _FakeNotifier,
    *,
    posts: int,
    timeout: float = 2.0,
) -> asyncio.Task:
    """Start the pulse loop and stop it once *posts* posts have landed."""
    task = asyncio.create_task(reporter.run())
    deadline = time.monotonic() + timeout
    while len(notifier.calls) < posts and time.monotonic() < deadline:
        await asyncio.sleep(0.001)
    reporter.request_stop()
    await asyncio.wait_for(task, timeout=timeout)
    return task


def _tool(name: str, **arguments: object) -> ToolCall:
    return ToolCall(id="t1", name=name, arguments=dict(arguments))


# ---------- Criterion 3 / 4a: redaction is an allowlist --------------------

@pytest.mark.parametrize("name", ["Read", "Edit", "Write"])
def test_c3_path_tools_render_file_path_only(name: str) -> None:
    hint = tool_hint(
        name,
        {"file_path": "src/x.py", "content": "SECRET=xoxb-abc123"},
    )
    assert hint == "src/x.py"


def test_c3_bash_renders_first_token_only() -> None:
    assert tool_hint("Bash", {"command": "pytest -k secret --token=x"}) == (
        "pytest"
    )


def test_c3_bash_env_assignment_renders_no_hint() -> None:
    """``SECRET=... cmd`` is a legal command whose first token is the
    secret; the naive first-word rule would post it verbatim."""
    assert tool_hint("Bash", {"command": "SECRET=xoxb-abc123 curl x"}) == ""


def test_c3_bash_empty_command_renders_no_hint() -> None:
    assert tool_hint("Bash", {"command": "   "}) == ""


def test_c3_unwhitelisted_tool_renders_no_hint() -> None:
    """A tool added next month is safe without anyone editing this."""
    assert tool_hint("Grep", {"pattern": "xoxb-abc123"}) == ""
    assert tool_hint("mcp__vendor__do", {"token": "xoxb-abc"}) == ""


def test_c3_missing_or_non_string_argument_renders_no_hint() -> None:
    assert tool_hint("Read", {}) == ""
    assert tool_hint("Read", {"file_path": 17}) == ""
    assert tool_hint("Bash", {"command": None}) == ""


def test_c3_non_dict_arguments_render_no_hint() -> None:
    """``claude -p`` builds arguments from ``blk.get("input") or {}``, so
    a non-dict can arrive off the wire."""
    assert tool_hint("Read", ["file_path"]) == ""


def test_c3_hint_is_truncated() -> None:
    hint = tool_hint("Read", {"file_path": "a/" * 200})
    assert len(hint) == progress_mod.HINT_MAX
    assert hint.endswith("…")


def test_c3_header_is_flattened_unfenced_and_truncated() -> None:
    raw = "line one\nline two ```python\nprint('x')\n``` " + "z" * 200
    out = sanitize_header(raw)
    assert "\n" not in out
    assert "`" not in out
    assert len(out) == progress_mod.HEADER_MAX
    assert out.endswith("…")
    assert out.startswith("line one line two python")


# ---------- Item 3 (D2): the header scrubber -------------------------------
#
# Every test here asserts BOTH halves. "The secret is absent" is satisfied
# by a scrubber that eats the whole excerpt, or by a reporter that posts
# nothing at all -- so each case also pins the prose that must survive.

def test_d2_header_drops_an_assignment_and_keeps_the_prose() -> None:
    out = sanitize_header("run SLACK_TOKEN=xoxb-deadbeef against staging")
    # Absent.
    assert "xoxb-deadbeef" not in out
    assert "SLACK_TOKEN" not in out
    # Present -- the excerpt still says what the turn was about.
    assert out == "run against staging"


def test_d2_header_keeps_an_assignment_with_an_empty_value() -> None:
    """``--flag=`` carries nothing, so the shape rule lets it through.

    Pinned because the obvious implementation -- drop any token with an
    ``=`` -- would silently eat ordinary command-line prose.
    """
    out = sanitize_header("re-run it with --profile= and see")
    assert out == "re-run it with --profile= and see"


def test_d2_header_scrubs_before_it_truncates() -> None:
    """Ordering, pinned by what survives rather than by reading the code.

    Scrub-then-truncate spends all 120 characters on prose. The reverse
    order spends the first 21 of them on a token it is about to throw
    away, and the excerpt comes back short.
    """
    out = sanitize_header("SECRET=xoxb-deadbeef " + "w" * 130)
    assert "xoxb-deadbeef" not in out
    assert len(out) == progress_mod.HEADER_MAX
    assert out == "w" * (progress_mod.HEADER_MAX - 1) + "…"


def test_d2_header_of_nothing_but_a_secret_renders_an_empty_excerpt() -> None:
    """The whole excerpt can legitimately scrub to nothing.

    Asserted as the exact rendered line, not as "a message exists": the
    failure this guards against is a caller that special-cases the empty
    string and posts something else, or nothing.
    """
    reporter, _, _ = _make(_FakeNotifier(), header="SLACK_TOKEN=xoxb-deadbeef")
    assert reporter._render_parent() == ':hourglass: still working — ""'


@pytest.mark.asyncio
async def test_d2_parent_post_carries_the_prose_and_not_the_secret() -> None:
    """End to end: the scrubber runs at the post site, not at the caller."""
    notifier = _FakeNotifier()
    reporter, _, _ = _make(
        notifier, header="deploy AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI now"
    )
    reporter.set_persona("Mitsui")
    await _run_until(reporter, notifier, posts=1)
    parent = notifier.texts[0]
    assert "wJalrXUtnFEMI" not in parent
    assert parent == ':hourglass: Mitsui still working — "deploy now"'


# ---------- Item 3 (D2): where the shape rule stops ------------------------
#
# The rule is token-shaped by design, so it has an edge, and QA found two
# ways over it. The backtick one was closed (backticks are deleted, not
# spaced out). The spaces-around-equals one cannot be closed by any
# token-shaped rule and is documented instead -- pinned here so the code
# and `docs/slack-bridge.md` describe the same boundary.

def test_d2_header_spaces_around_the_equals_defeat_the_shape_rule() -> None:
    """``KEY = value`` is three tokens, none of them an assignment.

    ``KEY`` and ``value`` carry no ``=``; the bare ``=`` has an empty
    right-hand side and is kept by the ``--profile=`` exemption. Nothing
    is dropped and the secret is posted verbatim.
    """
    out = sanitize_header("run SLACK_TOKEN = xoxb-deadbeef against staging")
    assert out == "run SLACK_TOKEN = xoxb-deadbeef against staging"


def test_d2_backticking_the_value_does_not_defeat_the_shape_rule() -> None:
    """Backticking a value must scrub identically to not backticking it.

    Spacing backticks out split ``SECRET=`xoxb-...``` into ``SECRET=``
    -- empty right-hand side, kept by the ``--profile=`` exemption --
    plus a bare value with no ``=`` at all, so the backticked form
    leaked where the plain form did not. Backticking a value is
    idiomatic in Slack, so the two forms must not disagree. Asserted as
    equality between them, not just as absence, so a scrubber that ate
    the whole excerpt could not satisfy it.
    """
    backticked = sanitize_header("run SECRET=`xoxb-deadbeef` against staging")
    plain = sanitize_header("run SECRET=xoxb-deadbeef against staging")
    assert backticked == plain == "run against staging"


def test_d2_header_leaks_every_colon_shape_the_docs_promise_it_leaks() -> None:
    """Pins the *leak* side, which `docs/slack-bridge.md` documents at length.

    The drop side above is thoroughly covered; what leaks was described
    only in prose, and that prose shipped wrong twice. These are the
    shapes credentials actually arrive in, and an operator decides what
    is safe to paste from the doc's word that they survive -- so the doc
    is wrong the moment this test goes red, in either direction.
    """
    for text in (
        "token: xoxb-deadbeef",
        '{"token": "xoxb-deadbeef"}',
        "Authorization: Bearer xoxb-deadbeef",
        "the token is xoxb-deadbeef",
    ):
        assert sanitize_header(text) == text


def test_d2_header_drops_per_token_so_a_colon_line_is_only_half_dropped() -> None:
    """A colon form whose *value* carries an ``=`` loses that token alone.

    The unit is the whitespace-delimited token, not the message. Pinned
    because the natural misreading -- that the colon protected it -- is
    the one the docs warn against, and because it bounds the claim above:
    colon shapes survive when their values carry no ``=``, not always.
    """
    assert sanitize_header("Cookie: session=abc123") == "Cookie:"


def test_d2_header_over_drops_a_url_carrying_a_query_parameter() -> None:
    """Documented as intended, and pinned so it is not "fixed" away.

    ``docs/slack-bridge.md`` predicts exactly this edit -- someone
    loosening the rule to preserve query strings -- and says it would
    reopen the commonest way a token reaches a channel. Prose cannot stop
    that; this can.
    """
    out = sanitize_header("curl https://example.invalid/a?token=deadbeef")
    assert "deadbeef" not in out
    assert out == "curl"


@pytest.mark.asyncio
async def test_c3_pulse_carries_the_hint_and_not_the_secret() -> None:
    """Both halves: absence alone is satisfied by posting nothing."""
    notifier = _FakeNotifier()
    reporter, _, _ = _make(notifier)
    reporter.on_event(_tool("Bash", command="pytest -k xoxb-secret"))
    reporter.on_event(
        ToolResult(id="t1", name="Bash", output=ToolOutput.of("xoxb-secret"))
    )
    await _run_until(reporter, notifier, posts=2)
    pulse = notifier.texts[1]
    assert "Bash(pytest)" in pulse
    assert "xoxb-secret" not in pulse
    assert "1 tool calls" in pulse


# ---------- Criterion 2: pulse content -------------------------------------

# The render-only tests take the real interval rather than ``_make``'s
# millisecond default: under a 2 ms interval every clock advance below is
# past ``2 * interval_s``, so the stall branch would win and hide the
# very text under assertion.

def test_c2_pulse_renders_elapsed_count_and_last_tool() -> None:
    reporter, _, clock = _make(
        _FakeNotifier(), interval_s=DEFAULT_INTERVAL_S
    )
    reporter.on_event(RunStart(session_id="s", model="m"))
    reporter.on_event(_tool("Edit", file_path="docs/slack-bridge.md"))
    clock.advance(300.0)
    line = reporter._render_pulse()
    assert line == (
        "5m · 1 tool calls · last: Edit(docs/slack-bridge.md)"
    )


def test_c2_tool_without_hint_renders_bare_name() -> None:
    reporter, _, clock = _make(
        _FakeNotifier(), interval_s=DEFAULT_INTERVAL_S
    )
    reporter.on_event(_tool("Grep", pattern="x"))
    clock.advance(300.0)
    assert reporter._render_pulse().endswith("last: Grep")


def test_c2_no_events_yet_renders_elapsed_and_count_only() -> None:
    reporter, _, clock = _make(
        _FakeNotifier(), interval_s=DEFAULT_INTERVAL_S
    )
    clock.advance(300.0)
    assert reporter._render_pulse() == "5m · 0 tool calls"


def test_c2_stall_boundary_is_inclusive_at_two_intervals() -> None:
    """``>=``, at pulse time, on monotonic timestamps — both sides."""
    clock = _FakeClock()
    reporter, _, _ = _make(
        _FakeNotifier(), interval_s=300.0, clock=clock
    )
    reporter.on_event(_tool("Read", file_path="a.py"))
    clock.advance(599.0)
    assert "no activity" not in reporter._render_pulse()
    clock.advance(1.0)
    line = reporter._render_pulse()
    assert line == "10m · 1 tool calls · no activity for 10m :warning:"


def test_c2_run_start_resets_the_per_attempt_tool_count() -> None:
    """Attempt 2 redoes the work, so tool calls repeat; elapsed does not
    reset because wall time is the honest answer to "still running?"."""
    reporter, _, clock = _make(_FakeNotifier())
    reporter.on_event(_tool("Read", file_path="a.py"))
    reporter.on_event(_tool("Read", file_path="b.py"))
    clock.advance(300.0)
    reporter.on_event(RunStart(session_id="s", model="m"))
    line = reporter._render_pulse()
    assert line.startswith("5m · 0 tool calls")


def test_c2_non_tool_events_only_refresh_liveness() -> None:
    reporter, _, clock = _make(_FakeNotifier(), interval_s=10.0)
    reporter.on_event(_tool("Read", file_path="a.py"))
    clock.advance(19.0)
    reporter.on_event(ErrorEvent(message="xoxb-secret", fatal=False))
    line = reporter._render_pulse()
    assert "no activity" not in line
    assert "xoxb-secret" not in line
    assert "1 tool calls" in line


# ---------- Criterion 7b(a)/3.4: backoff is not a stall --------------------

def test_c7b_retrying_replaces_the_stall_warning() -> None:
    clock = _FakeClock()
    reporter, _, _ = _make(_FakeNotifier(), interval_s=10.0, clock=clock)
    reporter.on_event(_tool("Bash", command="uv run pytest"))
    clock.advance(60.0)
    reporter.retrying(1)
    line = reporter._render_pulse()
    assert "retrying (attempt 2)" in line
    assert "no activity" not in line


def test_c7b_next_run_start_reverts_and_names_the_attempt() -> None:
    clock = _FakeClock()
    reporter, _, _ = _make(_FakeNotifier(), interval_s=10.0, clock=clock)
    reporter.retrying(1)
    reporter.on_event(RunStart(session_id="s", model="m"))
    reporter.on_event(_tool("Read", file_path="a.py"))
    clock.advance(300.0)
    line = reporter._render_pulse()
    assert "retrying" not in line
    # A silent counter reset reads as a bug to whoever is watching.
    assert line.endswith("attempt 2")


# ---------- Criterion 7c(a): the idle-compact window -----------------------

def test_c7c_compacting_replaces_the_stall_and_freezes_the_count() -> None:
    clock = _FakeClock()
    reporter, _, _ = _make(_FakeNotifier(), interval_s=10.0, clock=clock)
    reporter.on_event(_tool("Read", file_path="a.py"))
    reporter.on_event(_tool("Read", file_path="b.py"))
    clock.advance(600.0)
    reporter.compacting()
    line = reporter._render_pulse()
    assert line == "10m · 2 tool calls · compacting session"


def test_c7c_compacting_is_idempotent_and_never_clears() -> None:
    """Nothing follows a compaction except teardown, so a clear would be
    a branch with one reachable side."""
    clock = _FakeClock()
    reporter, _, _ = _make(_FakeNotifier(), interval_s=10.0, clock=clock)
    reporter.compacting()
    reporter.compacting()
    reporter.on_event(RunStart(session_id="s", model="m"))
    clock.advance(600.0)
    assert "compacting session" in reporter._render_pulse()


# ---------- Criterion 6e(a)/(b): the persona arrives late -------------------

def test_c6e_parent_renders_without_a_persona() -> None:
    reporter, _, _ = _make(_FakeNotifier(), header="fix the drain bug")
    parent = reporter._render_parent()
    assert parent == ':hourglass: still working — "fix the drain bug"'


def test_c6e_set_persona_names_the_turn() -> None:
    reporter, _, _ = _make(_FakeNotifier(), header="fix the drain bug")
    reporter.set_persona("Anzai")
    reporter.set_persona("Anzai")
    assert reporter._render_parent().startswith(
        ':hourglass: Anzai still working'
    )


@pytest.mark.asyncio
async def test_c6e_parent_posts_even_if_persona_never_arrives() -> None:
    """A turn wedged before the persona resolves is precisely a turn
    worth pulsing through, so the parent must not wait for the name."""
    notifier = _FakeNotifier()
    reporter, _, _ = _make(notifier)
    await _run_until(reporter, notifier, posts=1)
    assert notifier.texts[0].startswith(":hourglass: still working")


# ---------- Criterion 1 / 6: suppression and the happy path ----------------

@pytest.mark.asyncio
async def test_c1_short_turn_posts_nothing_at_all() -> None:
    """Not even a parent. Exactly zero — the deterministic half."""
    notifier = _FakeNotifier()
    reporter, _, _ = _make(notifier, interval_s=30.0)
    task = asyncio.create_task(reporter.run())
    await asyncio.sleep(0.005)
    reporter.request_stop()
    await asyncio.wait_for(task, timeout=2.0)
    await reporter.finish(ok=True)
    assert notifier.calls == []
    assert reporter.started is False


@pytest.mark.asyncio
async def test_c1_long_turn_posts_parent_then_pulses_then_closer() -> None:
    notifier = _FakeNotifier()
    reporter, _, clock = _make(notifier)
    reporter.set_persona("Mitsui")
    reporter.on_event(_tool("Bash", command="uv run pytest"))
    await _run_until(reporter, notifier, posts=3)
    clock.advance(1020.0)
    await reporter.finish(ok=True)
    assert reporter.started is True
    # Lower bound + ordering, not an exact count: an exact count off
    # real millisecond sleeps is a flake, and a flaky test gating this
    # feature gets deleted in six months.
    assert len(notifier.calls) >= 3
    assert notifier.texts[0].startswith(":hourglass: Mitsui still working")
    assert notifier.texts[-1] == (
        ":white_check_mark: done in 17m · 1 tool calls"
    )
    for text in notifier.texts[1:-1]:
        assert text.startswith("0m · 1 tool calls")


@pytest.mark.asyncio
async def test_c1_closer_reports_failure_with_detail() -> None:
    notifier = _FakeNotifier()
    reporter, _, clock = _make(notifier)
    await _run_until(reporter, notifier, posts=1)
    clock.advance(1020.0)
    await reporter.finish(ok=False, detail="TimeoutError")
    assert notifier.texts[-1] == ":x: failed after 17m · TimeoutError"


@pytest.mark.asyncio
async def test_c1_closer_without_detail_omits_the_suffix() -> None:
    notifier = _FakeNotifier()
    reporter, _, clock = _make(notifier)
    await _run_until(reporter, notifier, posts=1)
    clock.advance(60.0)
    await reporter.finish(ok=False)
    assert notifier.texts[-1] == ":x: failed after 1m"


@pytest.mark.asyncio
async def test_c1_pulses_thread_under_the_parent() -> None:
    notifier = _FakeNotifier(ts="P123")
    reporter, _, _ = _make(notifier)
    await _run_until(reporter, notifier, posts=2)
    await reporter.finish(ok=True)
    assert notifier.calls[0][2] is None
    assert all(call[2] == "P123" for call in notifier.calls[1:])


# ---------- Criterion 4: the DM thread is never touched --------------------

@pytest.mark.asyncio
async def test_c4a_every_post_carries_the_resolved_ops_channel() -> None:
    notifier = _FakeNotifier()
    reporter, _, _ = _make(notifier, channel="C-OPS")
    await _run_until(reporter, notifier, posts=2)
    await reporter.finish(ok=True)
    assert notifier.calls
    assert all(call[1] == "C-OPS" for call in notifier.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", [None, ""])
async def test_c4b_unconfigured_channel_produces_zero_calls(
    channel: str | None,
) -> None:
    notifier = _FakeNotifier()
    reporter, _, _ = _make(notifier, channel=channel)
    task = asyncio.create_task(reporter.run())
    await asyncio.wait_for(task, timeout=2.0)
    await reporter.finish(ok=True)
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_c4b_missing_notifier_produces_zero_calls() -> None:
    reporter, _, _ = _make(None, channel="C-OPS")
    await asyncio.wait_for(
        asyncio.create_task(reporter.run()), timeout=2.0
    )
    await reporter.finish(ok=True)
    assert reporter.started is False


@pytest.mark.asyncio
async def test_c4c_post_site_guard_runs_on_a_live_reporter() -> None:
    """The guard is per-post, not per-construction: ``post_text`` treats
    a falsy channel as the Operator's DM, so a reporter that validated
    once and cached the answer is one edit away from posting into the
    thread this feature exists to protect.

    Constructed LIVE so the inert short-circuit does not fire — then the
    channel is emptied before the post site is reached.
    """
    notifier = _FakeNotifier()
    reporter, _, _ = _make(notifier, channel="C-OPS")
    assert reporter._inert is False
    reporter._channel = ""
    task = asyncio.create_task(reporter.run())
    await asyncio.wait_for(task, timeout=2.0)
    await reporter.finish(ok=True)
    assert notifier.calls == []


# ---------- Criterion 5 / 6c: everything fails to silence ------------------

@pytest.mark.asyncio
async def test_c5_slack_outage_degrades_to_silence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    notifier = _FakeNotifier(raises=True)
    reporter, _, _ = _make(notifier)
    with caplog.at_level(logging.WARNING, logger=PROGRESS_LOGGER):
        task = asyncio.create_task(reporter.run())
        await asyncio.wait_for(task, timeout=2.0)
        await reporter.finish(ok=True)
    assert reporter.started is False
    assert len(notifier.calls) == 1


@pytest.mark.asyncio
async def test_c6c_failed_parent_post_makes_the_reporter_inert(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exactly ONE attempt for the whole turn, not one per interval:
    retrying a permanently bad channel id never succeeds and would log
    forever."""
    notifier = _FakeNotifier(ts=None)
    reporter, _, _ = _make(notifier)
    with caplog.at_level(logging.WARNING, logger=PROGRESS_LOGGER):
        task = asyncio.create_task(reporter.run())
        await asyncio.wait_for(task, timeout=2.0)
        await asyncio.sleep(0.01)
        await reporter.finish(ok=True)
    assert len(notifier.calls) == 1
    assert reporter.started is False
    failure_1 = [
        r for r in caplog.records
        if r.name == PROGRESS_LOGGER and "post failed" in r.message
    ]
    assert len(failure_1) == 1


@pytest.mark.asyncio
async def test_c5b_failure_1_logs_on_a_falsy_return(
    caplog: pytest.LogCaptureFixture,
) -> None:
    notifier = _FakeNotifier(ts=None)
    reporter, _, _ = _make(notifier)
    with caplog.at_level(logging.WARNING, logger=PROGRESS_LOGGER):
        await asyncio.wait_for(
            asyncio.create_task(reporter.run()), timeout=2.0
        )
    records = [r for r in caplog.records if r.name == PROGRESS_LOGGER]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "slack post failed for channel C-OPS" in records[0].getMessage()


@pytest.mark.asyncio
async def test_c5b_failure_1_logs_a_distinct_line_on_a_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    notifier = _FakeNotifier(raises=True)
    reporter, _, _ = _make(notifier)
    with caplog.at_level(logging.WARNING, logger=PROGRESS_LOGGER):
        await asyncio.wait_for(
            asyncio.create_task(reporter.run()), timeout=2.0
        )
    records = [r for r in caplog.records if r.name == PROGRESS_LOGGER]
    assert len(records) == 1
    assert "slack post raised for channel C-OPS" in records[0].getMessage()
    assert records[0].exc_info is not None


@pytest.mark.asyncio
async def test_c5_a_crashing_render_never_escapes_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The pulse loop's own last-resort guard. A reporter that raised
    into ``_dispatch`` would be a progress feature that breaks turns."""
    notifier = _FakeNotifier()
    reporter, _, _ = _make(notifier)
    boom = RuntimeError("render blew up")

    def _explode() -> str:
        raise boom

    reporter._render_parent = _explode  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger=PROGRESS_LOGGER):
        task = asyncio.create_task(reporter.run())
        await asyncio.sleep(0.02)
        reporter.request_stop()
        await asyncio.wait_for(task, timeout=2.0)
    assert notifier.calls == []
    assert any(
        "pulse failed" in r.getMessage()
        for r in caplog.records
        if r.name == PROGRESS_LOGGER
    )


# ---------- Criterion 6b: the cooperative stop -----------------------------

@pytest.mark.asyncio
async def test_c6b_post_in_flight_at_stop_is_not_orphaned() -> None:
    """``asyncio.to_thread`` is not cancellable — cancelling only
    abandons the wait, so a cancel-first stop leaves a lone
    ":hourglass: still working" in the channel with no closer under it.
    """
    gate = threading.Event()
    notifier = _FakeNotifier(block=gate)
    reporter, _, clock = _make(notifier)
    task = asyncio.create_task(reporter.run())
    try:
        while not notifier.calls:
            await asyncio.sleep(0.001)
        reporter.request_stop()
        await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        gate.set()
    await asyncio.wait_for(task, timeout=2.0)
    clock.advance(120.0)
    await reporter.finish(ok=True)
    assert notifier.texts[0].startswith(":hourglass:")
    assert notifier.texts[-1].startswith(":white_check_mark:")


@pytest.mark.asyncio
async def test_c6b_request_stop_is_idempotent() -> None:
    notifier = _FakeNotifier()
    reporter, _, _ = _make(notifier, interval_s=30.0)
    reporter.request_stop()
    reporter.request_stop()
    await asyncio.wait_for(
        asyncio.create_task(reporter.run()), timeout=2.0
    )
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_c6b_cancellation_propagates_out_of_run() -> None:
    """``run`` and ``finish`` must both let ``CancelledError`` through:
    the bridge bounds them with ``wait_for``, and that bound works only
    by cancelling at the ``to_thread`` await."""
    notifier = _FakeNotifier()
    reporter, _, _ = _make(notifier, interval_s=30.0)
    task = asyncio.create_task(reporter.run())
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_c6b_cancellation_propagates_out_of_finish() -> None:
    gate = threading.Event()
    notifier = _FakeNotifier(block=gate)
    reporter, _, _ = _make(notifier)
    reporter._started = True
    try:
        task = asyncio.create_task(reporter.finish(ok=True))
        while not notifier.calls:
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        gate.set()


# ---------- Criterion 1b + channel resolution ------------------------------

def test_c1b_shipped_interval_is_five_minutes() -> None:
    """Every other test injects a millisecond interval through §9's
    seam, so the one number the Operator specified is the one number
    nothing else checks."""
    assert DEFAULT_INTERVAL_S == 300.0
    reporter = TurnProgress(None, None, header="h")
    assert reporter._interval_s == 300.0


def test_c1b_build_passes_the_default_interval_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        progress_mod.SlackNotifier, "try_load", staticmethod(lambda: None)
    )
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    assert build_turn_progress("h")._interval_s == 300.0
    assert build_turn_progress("h", interval_s=1.0)._interval_s == 1.0


def test_channel_precedence_prefers_the_bridge_specific_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    monkeypatch.setenv(CHANNEL_ENV_VARS[0], "C-BRIDGE")
    monkeypatch.setenv(CHANNEL_ENV_VARS[1], "C-NOTIFY")
    assert resolve_progress_channel() == "C-BRIDGE"


def test_channel_falls_through_to_the_notify_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    monkeypatch.setenv(CHANNEL_ENV_VARS[1], "C-NOTIFY")
    assert resolve_progress_channel() == "C-NOTIFY"


def test_empty_value_does_not_count_as_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An or-chain would let an empty override win and silently disable
    the feature — indistinguishable from "not configured"."""
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    monkeypatch.setenv(CHANNEL_ENV_VARS[0], "   ")
    monkeypatch.setenv(CHANNEL_ENV_VARS[1], "C-NOTIFY")
    assert resolve_progress_channel() == "C-NOTIFY"


def test_no_channel_configured_resolves_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    assert resolve_progress_channel() is None


def test_channel_is_read_from_the_bridge_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A team that declared its ops channel once in ``.env`` should not
    have to declare it again."""
    env = tmp_path / ".env"
    env.write_text(f"{CHANNEL_ENV_VARS[0]}=C-FROM-DOTENV\n")
    monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(env))
    assert resolve_progress_channel() == "C-FROM-DOTENV"


def test_build_returns_an_inert_reporter_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never ``None``: the caller must not branch on configuration."""
    monkeypatch.setattr(
        progress_mod.SlackNotifier, "try_load", staticmethod(lambda: None)
    )
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    reporter = build_turn_progress("h")
    assert isinstance(reporter, TurnProgress)
    assert reporter._inert is True


def test_c5b_failure_2_logs_creds_without_a_channel(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """§6 promises the logs distinguish "configured but failing" from
    "not configured"; without this line the two look identical."""
    monkeypatch.setattr(
        progress_mod.SlackNotifier,
        "try_load",
        staticmethod(lambda: _FakeNotifier()),
    )
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        reporter = build_turn_progress("h")
    assert reporter._inert is True
    records = [
        r for r in caplog.records
        if r.name == PROGRESS_LOGGER and "no ops-log channel" in r.message
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert CHANNEL_ENV_VARS[0] in records[0].getMessage()


def test_build_returns_a_live_reporter_when_fully_configured(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        progress_mod.SlackNotifier,
        "try_load",
        staticmethod(lambda: _FakeNotifier()),
    )
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    monkeypatch.setattr(progress_mod, "_ANNOUNCED", set())
    monkeypatch.setenv(CHANNEL_ENV_VARS[0], "C-OPS")
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        reporter = build_turn_progress("h")
    assert reporter._inert is False
    assert reporter._channel == "C-OPS"
    # It must not COMPLAIN. It does now announce readiness once — the
    # positive case being silent was itself a finding, so this asserts
    # the absence of the complaint rather than the absence of logging.
    assert not [
        r for r in caplog.records
        if r.name == PROGRESS_LOGGER and "no ops-log channel" in r.message
    ]
    assert len([
        r for r in caplog.records
        if r.name == PROGRESS_LOGGER and "ARMED" in r.message
    ]) == 1


# ---------------------------------------------------------------------------
# Lane-scoped construction — the multi-lane deployment fix.
#
# The bug these cover shipped GREEN at a 100% floor, because every
# resolution test above hands `resolve_progress_channel()` an
# environment the deployed bridge does not have (`monkeypatch.setenv`,
# or `TIGERHARNESS_SLACK_ENV` pointing at a tmp .env). Coverage measures
# which lines ran, not which environments were modelled. These tests
# model the deployed one: os.environ carries NO progress keys at all.
# ---------------------------------------------------------------------------

def test_lane_scoped_build_never_reads_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployment-shaped regression test.

    A multi-lane bridge parses each lane's .env into a dict WITHOUT
    exporting it, so the process environment is empty of progress keys.
    This asserts the reporter comes up ENABLED anyway — the assertion
    that fails against the os.environ-resolving build.
    """
    for name in CHANNEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    # Would raise if the lane path consulted it at all.
    monkeypatch.setattr(
        progress_mod,
        "resolve_progress_channel",
        lambda: pytest.fail("lane path must not read os.environ"),
    )

    reporter = build_turn_progress(
        "h", bot_token="xoxb-lane-1", channel="C-LANE", lane="Shohoku"
    )

    assert reporter._inert is False
    assert reporter._channel == "C-LANE"


def test_each_lane_posts_with_its_own_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two lanes must not share one process-global token+channel pair.

    Sharing them is a confidentiality bug, not just a routing one: lane
    2's turns would post under lane 1's bot identity.
    """
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    one = build_turn_progress(
        "h", bot_token="xoxb-one", channel="C-ONE", lane="TeamOne"
    )
    two = build_turn_progress(
        "h", bot_token="xoxb-two", channel="C-TWO", lane="TeamTwo"
    )
    assert one._notifier._creds.bot_token == "xoxb-one"
    assert two._notifier._creds.bot_token == "xoxb-two"
    assert (one._channel, two._channel) == ("C-ONE", "C-TWO")


def test_lane_notifier_cannot_fall_back_to_a_dm() -> None:
    """No DM fallback, enforced structurally rather than by convention.

    ``_post`` refuses a falsy channel, and the lane notifier carries an
    empty ``target_user_id``, so there is no DM for a later edit to
    leak a pulse into.
    """
    reporter = build_turn_progress(
        "h", bot_token="xoxb-lane", channel="C-LANE", lane="Shohoku"
    )
    assert reporter._notifier._creds.target_user_id == ""


def test_lane_without_a_token_is_inert_not_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    reporter = build_turn_progress(
        "h", bot_token="", channel="C-LANE", lane="Shohoku"
    )
    assert reporter._notifier is None
    assert reporter._inert is True


def test_blank_lane_channel_falls_back_to_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The embedded single-team bridge keeps working unchanged: it has
    no lane channel to declare, so the process environment still wins."""
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    monkeypatch.setenv(CHANNEL_ENV_VARS[0], "C-FROM-ENV")
    reporter = build_turn_progress(
        "h", bot_token="xoxb-lane", channel="   ", lane="Shohoku"
    )
    assert reporter._channel == "C-FROM-ENV"
    assert reporter._inert is False


def test_parent_carries_the_lane_discriminator() -> None:
    """Several lanes may share one ops-log channel; an unlabelled parent
    from a shared channel cannot be attributed to a team at all."""
    reporter = TurnProgress(
        _FakeNotifier(), "C-OPS", header="ship it", lane="Shohoku"
    )
    reporter.set_persona("Anzai")
    rendered = reporter._render_parent()
    assert "[Shohoku]" in rendered
    assert "Anzai still working" in rendered


def test_parent_without_a_lane_is_unchanged() -> None:
    """The embedded bridge has one team, so it renders no prefix."""
    reporter = TurnProgress(_FakeNotifier(), "C-OPS", header="ship it")
    reporter.set_persona("Anzai")
    assert reporter._render_parent().startswith(":hourglass: Anzai")


def test_lane_name_cannot_break_the_one_line_parent() -> None:
    """The post site cleans whatever the caller supplied — the lane name
    comes from operator YAML, so it gets the same treatment as the
    header rather than being trusted."""
    reporter = TurnProgress(
        _FakeNotifier(), "C-OPS", header="ship it",
        lane="Sho\nhoku   Team",
    )
    rendered = reporter._render_parent()
    assert "\n" not in rendered
    assert "[Sho hoku Team]" in rendered


# ---------------------------------------------------------------------------
# Readiness signal — the positive case must not be silent either.
# ---------------------------------------------------------------------------

def test_armed_heartbeats_announce_themselves_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Configured-and-quiet and broken-and-quiet were indistinguishable
    until a turn happened to run past the interval."""
    monkeypatch.setattr(progress_mod, "_ANNOUNCED", set())
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        build_turn_progress(
            "h", bot_token="xoxb-1", channel="C-OPS", lane="Shohoku"
        )
    armed = [
        r for r in caplog.records
        if r.name == PROGRESS_LOGGER and "ARMED" in r.message
    ]
    assert len(armed) == 1
    rendered = armed[0].getMessage()
    assert "Shohoku" in rendered and "C-OPS" in rendered


def test_readiness_line_does_not_repeat_every_turn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``build_turn_progress`` runs on every dispatch; an unguarded INFO
    would log a line per message the bridge ever handles."""
    monkeypatch.setattr(progress_mod, "_ANNOUNCED", set())
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        for _ in range(5):
            build_turn_progress(
                "h", bot_token="xoxb-1", channel="C-OPS", lane="Shohoku"
            )
    assert len([
        r for r in caplog.records
        if r.name == PROGRESS_LOGGER and "ARMED" in r.message
    ]) == 1


def test_each_lane_announces_separately(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One quiet lane in a multi-lane bridge must still be visible."""
    monkeypatch.setattr(progress_mod, "_ANNOUNCED", set())
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        build_turn_progress(
            "h", bot_token="xoxb-1", channel="C-ONE", lane="TeamOne"
        )
        build_turn_progress(
            "h", bot_token="xoxb-2", channel="C-TWO", lane="TeamTwo"
        )
    armed = [
        r.getMessage() for r in caplog.records
        if r.name == PROGRESS_LOGGER and "ARMED" in r.message
    ]
    assert len(armed) == 2
    assert any("TeamOne" in m for m in armed)
    assert any("TeamTwo" in m for m in armed)


def test_inert_reporter_never_claims_to_be_armed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(progress_mod, "_ANNOUNCED", set())
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    for name in CHANNEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        reporter = build_turn_progress("h", bot_token="xoxb-1")
    assert reporter._inert is True
    assert not [
        r for r in caplog.records
        if r.name == PROGRESS_LOGGER and "ARMED" in r.message
    ]


def test_enabled_is_the_public_readiness_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented probe must not have to read a private attribute."""
    monkeypatch.setattr(progress_mod, "_ANNOUNCED", set())
    monkeypatch.setattr(
        progress_mod, "_load_slack_bridge_dotenv", lambda: None
    )
    live = build_turn_progress(
        "h", bot_token="xoxb-1", channel="C-OPS", lane="Shohoku"
    )
    assert live.enabled is True

    for name in CHANNEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    inert = build_turn_progress("h", bot_token="xoxb-1")
    assert inert.enabled is False


def test_enabled_answers_before_started_does() -> None:
    """`started` stays False for the whole first interval of a healthy
    turn, so it cannot serve as the readiness check."""
    reporter = TurnProgress(_FakeNotifier(), "C-OPS", header="h")
    assert reporter.enabled is True
    assert reporter.started is False
