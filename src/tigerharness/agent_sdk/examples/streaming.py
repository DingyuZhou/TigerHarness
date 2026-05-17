"""Consume streaming events from the agent.

Run:
    python -m tigerharness.agent_sdk.examples.streaming
"""

from __future__ import annotations

import asyncio

from tigerharness.agent_sdk import (
    AgentConfig,
    ErrorEvent,
    MessageComplete,
    RunDone,
    RunStart,
    TextDelta,
    Thinking,
    ToolCall,
    ToolResult,
    get_backend,
)


async def main() -> None:
    backend = get_backend("claude_p")
    cfg = AgentConfig(
        name="story",
        instructions="Write a 3-sentence bedtime story about the topic.",
    )
    # `async with` guarantees the subprocess is reaped even if you `break`
    # out of the loop early.
    async with backend.run_stream(cfg, "a friendly robot meets a cat") as handle:
        async for event in handle:
            match event:
                case RunStart(session_id=sid, model=m):
                    print(f"[start]   session={sid} model={m}")
                case TextDelta(text=t):
                    print(t, end="", flush=True)
                case MessageComplete(text=t):
                    print(f"\n[message] {t}")
                case Thinking(text=t):
                    print(f"[think]   {t[:80]}...")
                case ToolCall(name=n, arguments=a):
                    print(f"[tool->]  {n}({a})")
                case ToolResult(name=n, output=o):
                    snippet = (o.text or str(o.data) or "")[:120]
                    print(f"[tool<-]  {n}: {snippet}")
                case ErrorEvent(message=m, fatal=f):
                    print(f"[error]   ({'fatal' if f else 'warn'}) {m}")
                case RunDone(stop_reason=sr, cost_usd=c):
                    print(f"[done]    reason={sr} cost=${c}")

        # After iteration completes, the full RunResult is available.
        print(f"\nfinal: {handle.result.final_output!r}")


if __name__ == "__main__":
    asyncio.run(main())
