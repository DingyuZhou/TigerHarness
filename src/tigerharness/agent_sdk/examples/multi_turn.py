"""Multi-turn conversation using session resume.

Run:
    python -m tigerharness.agent_sdk.examples.multi_turn
"""

from __future__ import annotations

import asyncio

from tigerharness.agent_sdk import AgentConfig, get_backend


async def main() -> None:
    backend = get_backend("claude_p")
    cfg = AgentConfig(name="chat", instructions="Be brief and friendly.")

    session = await backend.open_session()
    print(f"opened session (id will populate after the first turn): {session.id!r}")

    r1 = await backend.run(
        cfg, "What's the capital of France?", session=session
    )
    print(f"turn 1: {r1.final_output}")
    print(f"session.id is now: {session.id!r}")

    r2 = await backend.run(
        cfg, "What language do they speak there?", session=session
    )
    print(f"turn 2: {r2.final_output}")

    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
