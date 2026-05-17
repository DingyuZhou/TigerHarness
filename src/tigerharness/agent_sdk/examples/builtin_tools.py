"""Use Claude Code's built-in tools (Read, Bash) via the claude_p backend.

Run:
    python -m tigerharness.agent_sdk.examples.builtin_tools

Note: this example uses ``permission_mode="bypassPermissions"`` so the agent
doesn't pause to ask for tool approval. Only run it in directories where
you're comfortable letting the agent read files and run shell commands.
"""

from __future__ import annotations

import asyncio

from tigerharness.agent_sdk import (
    AgentConfig,
    BuiltinTool,
    MessageComplete,
    RunDone,
    ToolCall,
    ToolResult,
    get_backend,
)


async def main() -> None:
    backend = get_backend("claude_p")
    cfg = AgentConfig(
        name="dev",
        instructions=(
            "You are a coding assistant. Use the Bash and Read tools to "
            "answer questions about the local filesystem. Keep responses "
            "short."
        ),
        builtin_tools=[BuiltinTool(name="Bash"), BuiltinTool(name="Read")],
        max_turns=5,
        extra={"permission_mode": "bypassPermissions"},
    )

    handle = backend.run_stream(
        cfg, "List the Python files in the current directory."
    )
    async for event in handle:
        match event:
            case ToolCall(name=n, arguments=a):
                print(f"-> {n}({a})")
            case ToolResult(name=n, output=o):
                print(f"<- {n}: {(o.text or str(o.data))[:160]}")
            case MessageComplete(text=t):
                print(f"[msg] {t}")
            case RunDone(stop_reason=sr, cost_usd=c):
                print(f"[done] {sr} ${c}")

    print("final:", handle.result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
