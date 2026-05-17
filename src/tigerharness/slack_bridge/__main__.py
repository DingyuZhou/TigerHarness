"""Run the Slack bridge: `python -m tigerharness.slack_bridge`.

Loads config from env/.env, builds the bridge, and starts the
Socket-Mode handler. On SIGTERM the bridge drains in-flight
dispatches so replies are posted before the process exits.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .bridge import build_bridge
from .config import load

log = logging.getLogger("tigerharness.slack_bridge")

# How long to wait for in-flight dispatches before hard-exiting.
_DRAIN_TIMEOUT_S = 90.0


async def _run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = load()
    bridge = build_bridge(cfg)
    handler = AsyncSocketModeHandler(bridge.app, cfg.slack_app_token)

    shutdown_complete = asyncio.Event()

    async def _graceful_shutdown() -> None:
        log.info("caught termination signal -- draining in-flight dispatches")
        bridge.request_shutdown()
        drained = await bridge.wait_for_drain(timeout=_DRAIN_TIMEOUT_S)
        if drained:
            log.info("all dispatches drained -- shutting down cleanly")
        else:
            log.warning("drain timed out -- shutting down with work in-flight")
        try:
            await handler.close_async()
        except Exception:
            log.warning("handler.close_async raised", exc_info=True)
        shutdown_complete.set()

    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        asyncio.ensure_future(_graceful_shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    log.info(
        "starting bridge -- cwd=%s allowed_users=%s",
        cfg.agent_cwd,
        sorted(cfg.allowed_user_ids),
    )

    start_task = asyncio.create_task(handler.start_async())
    shutdown_task = asyncio.create_task(shutdown_complete.wait())

    done, pending = await asyncio.wait(
        {start_task, shutdown_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    log.info("bridge process exiting")


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
