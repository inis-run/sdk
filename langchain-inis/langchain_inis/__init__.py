"""inis.run sandbox backend for LangChain Deep Agents.

    from inis import Client
    from deepagents import create_deep_agent
    from langchain_inis import InisSandboxBackend

    backend = InisSandboxBackend.create()  # owned: this app destroys it
    try:
        agent = create_deep_agent(model="anthropic:claude-sonnet-4-6", backend=backend)
        result = agent.invoke({"messages": [{"role": "user", "content": "..."}]})
    finally:
        backend.destroy()

See `langchain_inis.backend` for the full verified contract (exact
`deepagents` version, public-vs-internal extension-point finding, what is
and isn't implemented) and README.md for owned-vs-attached usage.
"""

from __future__ import annotations

from langchain_inis.backend import InisSandboxBackend

__all__ = ["InisSandboxBackend"]

__version__ = "0.1.0"
