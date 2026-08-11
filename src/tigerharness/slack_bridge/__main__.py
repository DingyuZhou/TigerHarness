"""Run the Slack bridge: ``python -m tigerharness.slack_bridge``.

There is **one** bridge; it serves 1..N teams (lanes).
``TIGERHARNESS_BRIDGES_CONFIG`` must point at a ``slack-bridge.yaml``
index: the bridge loads N lanes via ``multi.load_multi()``, builds N
bridges, and opens N Socket-Mode connections in one process. A single
team is just a one-lane index. Each lane's logs are tagged with
``lane=<name>`` (contextvar-based, inherited by Bolt's dispatch tasks).

The former single-tenant fallback (tokens read from process env /
``.env`` when the index var was unset) was removed on 2026-08-11 after
its announced deprecation -- see ADR 0009 and
``docs/slack-bridge.md`` ("Migrating off single-tenant"). Startup now
fails fast with a migration pointer instead of silently running a
one-off deployment shape.

On SIGTERM the bridge drains in-flight dispatches across all lanes
before exiting so replies are posted before the process dies.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import signal
from pathlib import Path

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .bridge import SlackBridge, build_team_bridge
from .multi import MultiBridgeConfig, load_multi

log = logging.getLogger("tigerharness.slack_bridge")

# How long to wait for in-flight dispatches before hard-exiting. Each
# lane gets the same budget; since lanes drain concurrently, the
# worst-case total wait is still this value, not N*value.
_DRAIN_TIMEOUT_S = 90.0


# ---------------------------------------------------------------------------
# Lane-aware logging
# ---------------------------------------------------------------------------

# Contextvar holds the current lane's name. Tasks created inside a
# lane's coroutine inherit it via asyncio's per-task context copy --
# Bolt's child tasks (one per Slack message) see it too, so all logs
# from inside a lane's dispatch get ``lane=<name>`` automatically.
_lane_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "lane", default=""
)


class _LaneFilter(logging.Filter):
    """Ensure every log record has a ``lane`` attribute so the format
    string ``lane=%(lane)s`` doesn't blow up on records produced before
    a lane is entered. Reads the value from the contextvar."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.lane = _lane_var.get()
        return True


def _setup_logging() -> None:
    """Configure root logging with the lane-aware format: ``lane=`` in
    the format string + a filter that fills it from the contextvar (a
    one-lane index reads naturally -- ``lane=<team>`` on every line).

    Uses ``force=True`` so this re-applies cleanly when tests have
    already configured logging in the same process.
    """
    fmt = "%(asctime)s lane=%(lane)s %(name)s %(levelname)s %(message)s"
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(_LaneFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

async def _drive_handlers(
    handlers: list[AsyncSocketModeHandler],
    bridges: list[SlackBridge],
    lane_names: list[str] | None = None,
) -> None:
    """Drive N Socket-Mode handlers concurrently until SIGTERM / SIGINT.

    On signal: request_shutdown on every bridge, ``asyncio.gather`` on
    each bridge's ``wait_for_drain`` (so the budget is shared across
    lanes rather than serialized), close handlers, exit.

    When *lane_names* is provided, each handler is launched inside a
    coroutine that sets the lane contextvar first, so all log records
    emitted while it runs (including Bolt's internal dispatch) carry
    that lane's name.
    """
    shutdown_complete = asyncio.Event()

    async def _graceful_shutdown() -> None:
        log.info("caught termination signal -- draining %d lane(s)", len(bridges))
        for b in bridges:
            b.request_shutdown()
        drains = [b.wait_for_drain(timeout=_DRAIN_TIMEOUT_S) for b in bridges]
        results = await asyncio.gather(*drains, return_exceptions=True)
        # `wait_for_drain` returns True on clean drain, False on timeout;
        # exceptions are caught by return_exceptions so we don't take
        # down the shutdown path.
        if all(r is True for r in results):
            log.info("all lanes drained -- shutting down cleanly")
        else:
            log.warning("drain timed out for some lane(s) -- shutting down anyway")
        for h in handlers:
            try:
                await h.close_async()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                log.warning("handler.close_async raised", exc_info=True)
        shutdown_complete.set()

    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        asyncio.ensure_future(_graceful_shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    async def _start_in_lane(
        handler: AsyncSocketModeHandler, name: str | None
    ) -> None:
        # Set the contextvar INSIDE the task so its context (and the
        # contexts of any child tasks Bolt spawns) carry the lane name.
        if name:
            _lane_var.set(name)
        await handler.start_async()

    start_tasks = [
        asyncio.create_task(
            _start_in_lane(h, lane_names[i] if lane_names else None)
        )
        for i, h in enumerate(handlers)
    ]
    shutdown_task = asyncio.create_task(shutdown_complete.wait())

    done, pending = await asyncio.wait(
        {*start_tasks, shutdown_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    log.info("bridge process exiting")


# ---------------------------------------------------------------------------
# Multi-tenant entrypoint
# ---------------------------------------------------------------------------

async def _run_multi(multi_cfg: MultiBridgeConfig) -> None:
    _setup_logging()
    bridges: list[SlackBridge] = []
    handlers: list[AsyncSocketModeHandler] = []
    for lane in multi_cfg.lanes:
        b = build_team_bridge(lane.team_ctx, state_path=lane.state_path)
        h = AsyncSocketModeHandler(b.app, lane.team_ctx.slack_app_token)
        bridges.append(b)
        handlers.append(h)
        log.info(
            "lane=%s registered -- cwd=%s personas=%s allowed_users=%s",
            lane.name,
            lane.team_ctx.agent_cwd,
            sorted(lane.team_ctx.personas.keys()),
            sorted(lane.team_ctx.allowed_user_ids),
        )
    log.info("starting bridge with %d lane(s)", len(multi_cfg.lanes))
    await _drive_handlers(
        handlers, bridges,
        lane_names=[lane.name for lane in multi_cfg.lanes],
    )


# ---------------------------------------------------------------------------
# main(): the index env var is required (fail fast with the migration path)
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        index_env = os.environ.get("TIGERHARNESS_BRIDGES_CONFIG", "").strip()
        if not index_env:
            raise SystemExit(
                "slack-bridge: TIGERHARNESS_BRIDGES_CONFIG is not set. The "
                "single-tenant fallback was REMOVED on 2026-08-11 (ADR 0009) "
                "after its announced deprecation -- the bridge always runs "
                "from a slack-bridge.yaml lanes index now, and a single team "
                "is just a one-lane index. To migrate: create the index, set "
                "TIGERHARNESS_BRIDGES_CONFIG to it, and run `tigerharness "
                "slack-bridge gen-service` to emit the systemd unit; see "
                "docs/slack-bridge.md ('Migrating off single-tenant')."
            )
        multi_cfg = load_multi(Path(index_env))
        asyncio.run(_run_multi(multi_cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
