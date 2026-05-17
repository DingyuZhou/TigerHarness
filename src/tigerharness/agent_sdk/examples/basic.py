"""One-shot Q&A through the claude_p backend.

Run:
    python -m tigerharness.agent_sdk.examples.basic
"""

from __future__ import annotations

import asyncio

from tigerharness.agent_sdk import AgentConfig, get_backend


async def main() -> None:
    backend = get_backend("claude_p")
    cfg = AgentConfig(
        name="qa",
        instructions="You are a concise assistant. Reply in one sentence.",
    )
    result = await backend.run(cfg, "What is 2 + 2?")
    print("answer:", result.final_output)
    print("stop_reason:", result.stop_reason)
    print("cost_usd:", result.cost_usd)


if __name__ == "__main__":
    asyncio.run(main())
