"""tiger-memory: agent-agnostic conversation memory.

See ``docs/019_sai_memory_system.md`` (in the consumer repo) for the
design. Public surface is the CLI (``tiger-memory <subcommand>``) and
the ``Store`` / ``Config`` types for library use.
"""
from .config import Config, load_config
from .store import Store

__all__ = ["Config", "Store", "load_config"]
__version__ = "0.1.0"
