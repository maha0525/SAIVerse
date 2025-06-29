"""In-memory tracker for tool invocations."""
from collections import defaultdict

_called_counts: dict[str, int] = defaultdict(int)


def record_tool_call(name: str) -> None:
    """Record a tool invocation by name."""
    _called_counts[name] += 1


def get_called_count(name: str) -> int:
    """Return how many times the given tool was called."""
    return _called_counts.get(name, 0)


# Public alias for direct access if needed
called_tools = _called_counts
