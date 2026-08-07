"""Provider adapters for instruction-surface discovery."""

from .base import DiscoveryResult, ProviderAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter

__all__ = [
    "ClaudeAdapter",
    "CodexAdapter",
    "DiscoveryResult",
    "ProviderAdapter",
]
