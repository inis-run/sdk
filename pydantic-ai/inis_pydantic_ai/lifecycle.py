"""Opt-in toolsets for session/sandbox administration.

`inis_toolset()` (in `toolset.py`) deliberately excludes anything that
creates, destroys, pauses, exposes, or otherwise administers a session or
sandbox — the model-facing default exposes only the bounded execution/file
surface it needs, while credentials and lifecycle administration remain
host-controlled or explicitly opt-in. The two toolsets here are that
opt-in: import and add them to `toolsets=[...]` alongside `inis_toolset()`
only when your application deliberately wants the model to have this control
(e.g. an agent whose job *is* infra ops on its own sandbox).

    from inis_pydantic_ai import inis_toolset
    from inis_pydantic_ai.lifecycle import inis_lifecycle_toolset

    agent = Agent(
        "openai:gpt-4o",
        toolsets=[
            inis_toolset(session_id=session.session_id),
            inis_lifecycle_toolset(session_id=session.session_id),
        ],
    )
"""

from __future__ import annotations

from typing import Any

from inis import AsyncClient, InisError
from pydantic_ai.toolsets import FunctionToolset

from inis_pydantic_ai.toolset import InisToolset, _SessionHolder


def inis_lifecycle_toolset(
    *,
    session_id: str,
    api_key: str | None = None,
    base_url: str | None = None,
) -> InisToolset:
    """Opt-in toolset giving the model pause/resume/checkpoint/restore/
    expose/capture-artifacts control over one existing session.

    Attaches to `session_id` only — never creates or destroys the session
    (same contract as `inis_toolset()`). Every tool here is administration,
    not execution: adding this toolset hands the model the ability to pause
    the sandbox, roll it back to a checkpoint, or open a public preview URL
    for it. Do this deliberately, not by default. Call `aclose()` on the
    returned toolset when the conversation ends, same as `inis_toolset()`.
    """
    client = AsyncClient(token=api_key, base_url=base_url)
    holder = _SessionHolder(client, session_id)
    toolset = InisToolset(holder)

    @toolset.tool_plain
    async def expose_port(port: int) -> str:
        """Expose a guest port and return the HTTPS preview URL."""
        preview = await holder.get().expose(port)
        return preview.preview_url

    @toolset.tool_plain
    async def pause() -> str:
        """Pause the session to a snapshot.

        Useful to avoid idle timeouts during long model reasoning steps.
        The session can be resumed later with resume().
        """
        info = await holder.get().pause()
        return f"session_id: {info.session_id} state: {info.state}"

    @toolset.tool_plain
    async def resume() -> str:
        """Resume a paused session."""
        info = await holder.get().resume()
        return f"session_id: {info.session_id} state: {info.state}"

    @toolset.tool_plain
    async def checkpoint(
        name: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> str:
        """Take a named, retained snapshot of the current session state.

        The session keeps running after the checkpoint is captured.

        Args:
            name:   Optional checkpoint name (lowercase alphanumerics and hyphens).
            labels: Optional key/value metadata.

        Returns:
            Checkpoint ID, e.g. "chk_abc123".
        """
        ckpt = await holder.get().checkpoint(name=name, labels=labels)
        return ckpt.checkpoint_id

    @toolset.tool_plain
    async def restore(checkpoint_id: str) -> str:
        """Roll the session back to a checkpoint in place.

        Args:
            checkpoint_id: ID or name of the checkpoint to restore from.

        Returns:
            "session_id: <id> state: live"
        """
        info = await holder.get().restore(checkpoint_id)
        return f"session_id: {info.session_id} state: {info.state}"

    @toolset.tool_plain
    async def capture_artifacts(paths: list[str]) -> str:
        """Capture files or glob patterns from /workspace to durable EU storage.

        Returns an artifact ID immediately; the upload runs in the background.

        Args:
            paths: Paths or glob patterns under /workspace to capture.

        Returns:
            Artifact ID, e.g. "art_abc123".
        """
        artifact = await holder.get().capture_artifacts(paths)
        return artifact.id

    return toolset


class _EphemeralToolset(FunctionToolset[Any]):
    """Returned by `inis_ephemeral_toolset()`; `aclose()` releases the local
    HTTP transport (this toolset holds no session to attach or destroy)."""

    def __init__(self, client: AsyncClient) -> None:
        super().__init__()
        self._client = client

    async def aclose(self) -> None:
        await self._client.close()


def inis_ephemeral_toolset(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> _EphemeralToolset:
    """Opt-in toolset giving the model the ability to spin up and tear down
    its own throwaway sandboxes, one per call.

    Unlike `inis_toolset()`, this does not attach to an existing session at
    all — `execute_once` creates a brand-new sandbox for each call and
    destroys it immediately after. Creation and deletion are exactly the
    kind of thing that stays host-controlled or explicitly opt-in: handing
    a model unbounded, per-call sandbox creation is a real resource/cost
    control decision, not a default.
    """
    client = AsyncClient(token=api_key, base_url=base_url)
    toolset = _EphemeralToolset(client)

    @toolset.tool_plain
    async def execute_once(code: str, language: str = "python") -> str:
        """Run code in a throwaway sandbox — no session is retained.

        The sandbox is created, the code runs, and the sandbox is immediately
        destroyed. Use this for isolated one-shot execution that must not share
        state with any other tool call.

        Args:
            code:     Source code or shell command to execute.
            language: Runtime language — python, node, or bun (default: python).

        Raises InisError if the process exits non-zero.
        """
        result = await client.execute(language=language, code=code)
        if result.exit_code != 0:
            raise InisError(
                f"{language} exited {result.exit_code}: {result.stderr or result.stdout}"
            )
        if result.stderr:
            return f"{result.stdout}\n[stderr]\n{result.stderr}".strip()
        return result.stdout

    return toolset
