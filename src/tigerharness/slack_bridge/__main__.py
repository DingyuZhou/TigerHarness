"""Run the Slack bridge: ``python -m tigerharness.slack_bridge``.

Two modes, dispatched by env var:

* **Single-tenant (default).** Reads tokens from ``.env`` / process env
  via ``config.load()``, builds one bridge, opens one Socket-Mode
  connection.
* **Multi-tenant.** When ``TIGERHARNESS_BRIDGES_CONFIG`` points at a
  ``slack-bridge.yaml`` index, loads N lanes via ``multi.load_multi()``,
  builds N bridges, opens N Socket-Mode connections in one process.
  Each lane's logs are tagged with ``lane=<name>`` (contextvar-based,
  inherited by Bolt's dispatch tasks).

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

from .bridge import SlackBridge, build_bridge, build_team_bridge
from .config import load
from .multi import MultiBridgeConfig, load_multi
from .persistence import default_state_path

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


def _setup_logging(*, multi: bool) -> None:
    """Configure root logging. Multi-mode adds ``lane=`` to the format
    + a filter that fills it from the contextvar. Single-tenant mode
    keeps the original format -- zero output change for existing users.

    Uses ``force=True`` so this re-applies cleanly when tests have
    already configured logging in the same process.
    """
    if multi:
        fmt = "%(asctime)s lane=%(lane)s %(name)s %(levelname)s %(message)s"
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(fmt))
        handler.addFilter(_LaneFilter())
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            force=True,
        )


# ---------------------------------------------------------------------------
# Lifecycle helpers (shared by single- and multi-tenant entrypoints)
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
# Single-tenant entrypoint (default; backward compatible)
# ---------------------------------------------------------------------------

async def _run_single() -> None:
    _setup_logging(multi=False)
    cfg = load()
    bridge = build_bridge(cfg, state_path=default_state_path())
    handler = AsyncSocketModeHandler(bridge.app, cfg.slack_app_token)
    log.info(
        "starting bridge -- cwd=%s allowed_users=%s",
        cfg.agent_cwd,
        sorted(cfg.allowed_user_ids),
    )
    await _drive_handlers([handler], [bridge])


# ---------------------------------------------------------------------------
# Multi-tenant entrypoint
# ---------------------------------------------------------------------------

async def _run_multi(multi_cfg: MultiBridgeConfig) -> None:
    _setup_logging(multi=True)
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
# main(): env var dispatches to single or multi
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        index_env = os.environ.get("TIGERHARNESS_BRIDGES_CONFIG", "").strip()
        if index_env:
            multi_cfg = load_multi(Path(index_env))
            asyncio.run(_run_multi(multi_cfg))
        else:
            asyncio.run(_run_single())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
