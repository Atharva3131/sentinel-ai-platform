"""LangGraph runtime adapter.

This package is the only location in the backend allowed to reference LangGraph APIs.
The rest of the codebase depends exclusively on the framework-neutral runtime contracts in
`backend.runtime`.
"""

from backend.runtime.langgraph.runtime import LangGraphRuntime

__all__ = ["LangGraphRuntime"]

