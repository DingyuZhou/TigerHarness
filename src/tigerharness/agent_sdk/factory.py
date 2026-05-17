"""Backend registry. Switch backends by changing one string.

Built-in registrations:
    "claude_p"        -> ClaudePBackend
                         Spawns ``claude -p`` as a subprocess. Always available;
                         requires the Claude Code CLI on ``PATH``.
    "anthropic_sdk"   -> AnthropicSDKBackend
                         Wraps Anthropic's official ``claude-agent-sdk`` Python
                         package. Install with ``pip install tigerharness[anthropic]``.
    "openai_sdk"      -> OpenAISDKBackend
                         Stub. Will wrap ``openai-agents`` when implemented.

You can register your own backends:

    from tigerharness.agent_sdk import register_backend, AgentBackend

    class MyBackend:
        # implement AgentBackend Protocol
        ...

    register_backend("mine", lambda **kw: MyBackend(**kw))
    backend = get_backend("mine")
"""

from __future__ import annotations

from typing import Any, Callable

from .errors import AgentSDKError
from .types import AgentBackend


_REGISTRY: dict[str, Callable[..., AgentBackend]] = {}


def register_backend(name: str, factory: Callable[..., AgentBackend]) -> None:
    """Register a backend factory under `name`. Overwrites any existing
    registration.
    """
    _REGISTRY[name] = factory


def get_backend(name: str = "claude_p", **kwargs: Any) -> AgentBackend:
    """Look up and instantiate a backend by name.

    Backend-specific kwargs are forwarded to the backend's constructor.

    Examples:
        backend = get_backend("claude_p")
        backend = get_backend("claude_p", cli="/usr/local/bin/claude")
    """
    if name not in _REGISTRY:
        raise AgentSDKError(
            f"Unknown backend {name!r}. Registered: {sorted(_REGISTRY)}. "
            "Use `register_backend` to add a custom one."
        )
    return _REGISTRY[name](**kwargs)


def list_backends() -> list[str]:
    """Return the names of all registered backends."""
    return sorted(_REGISTRY)


# ----- Lazy registrations of built-in backends -----
# Each factory imports its module on demand so the SDK doesn't pay the
# import cost (or fail to import) for backends the caller isn't using.

def _claude_p_factory(**kw: Any) -> AgentBackend:
    from .backends.claude_p import ClaudePBackend
    return ClaudePBackend(**kw)


def _anthropic_sdk_factory(**kw: Any) -> AgentBackend:
    from .backends.anthropic_sdk import AnthropicSDKBackend
    return AnthropicSDKBackend(**kw)


def _openai_sdk_factory(**kw: Any) -> AgentBackend:
    from .backends.openai_sdk import OpenAISDKBackend
    return OpenAISDKBackend(**kw)


register_backend("claude_p", _claude_p_factory)
register_backend("anthropic_sdk", _anthropic_sdk_factory)
register_backend("openai_sdk", _openai_sdk_factory)
