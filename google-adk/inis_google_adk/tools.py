"""Explicit, non-experimental fallback: plain `google.adk.tools.function_tool.
FunctionTool` wrappers around an inis.run session, for callers who don't want
to depend on ADK's `@experimental` `BaseEnvironment`/`EnvironmentToolset`
surface at all (see `environment.py`'s module docstring for exactly what's
experimental and why). `google.adk.tools.function_tool.FunctionTool` itself
carries no `@experimental` decorator in installed source -- verified, not
assumed -- so this path is the stable one per the issue's "provide an
explicit toolset fallback while the environment API remains experimental"
requirement.

Mirrors `inis_pydantic_ai.toolset.inis_toolset()`'s shape: the smallest
execution/file surface a model needs, nothing that creates, destroys,
pauses, or otherwise administers sessions. Attach-only -- pass an existing
`inis.AsyncSession`'s ID; this module never creates or destroys one.
"""

from __future__ import annotations

import secrets

from google.adk.tools.function_tool import FunctionTool

from inis import AsyncSession, InisError


def inis_adk_tools(session: AsyncSession) -> list[FunctionTool]:
    """Return plain ADK `FunctionTool`s (`execute`, `read_file`, `write_file`,
    `list_files`) bound to an already-created/attached inis.run session.

    Unlike `InisEnvironment`/`EnvironmentToolset`, these are not experimental
    upstream and do not require ADK's environment surface at all -- pass the
    result straight to `Agent(tools=[...])`. This adapter never creates or
    destroys `session`; the host owns that, one session per agent thread,
    same as everywhere else in this package.
    """
    if not session.session_id:
        raise ValueError("inis_adk_tools() requires an already-created/attached session")

    async def execute(command: str) -> str:
        """Run a shell command in the inis.run session and return its output.

        Args:
          command: The shell command to run. Chain dependent commands with &&.
        """
        result = await session.exec(command)
        output = result.stdout
        if result.stderr:
            output = f"{output}\n[stderr]\n{result.stderr}" if output else result.stderr
        if result.timed_out:
            output = f"{output}\n[inis: command timed out]" if output else "[inis: command timed out]"
        if result.exit_code != 0:
            output = f"{output}\n[inis: exited {result.exit_code}]"
        if result.truncated:
            # ExecResult.truncated is real (sdk/python/inis/client.py) but
            # this function returns a single string with no separate field
            # to carry it -- append a visible marker instead of silently
            # dropping the fact that what's above is a partial view, the
            # same choice InisEnvironment.execute() makes for the identical
            # gap (see environment.py). Nonce is fresh per call: a guest
            # with full control of its own output could otherwise read this
            # marker's exact wording out of this open-source file and print
            # a fake one.
            nonce = secrets.token_hex(8)
            marker = f"[inis: output truncated by the sandbox's own capture limit (nonce={nonce})]"
            output = f"{output}\n{marker}" if output else marker
        return output

    async def read_file(path: str) -> str:
        """Read a file from the inis.run session's filesystem.

        Args:
          path: Absolute or /workspace-relative path to the file.
        """
        try:
            return await session.read_file(path)
        except InisError as e:
            return f"[inis: read failed: {e}]"

    async def write_file(path: str, content: str) -> str:
        """Write a file to the inis.run session's filesystem.

        Args:
          path: Absolute or /workspace-relative path to the file.
          content: The full text content to write.
        """
        await session.write_file(path, content)
        return f"wrote {len(content)} bytes to {path}"

    async def list_files(path: str = "/workspace") -> str:
        """List files in a directory in the inis.run session (defaults to /workspace).

        Args:
          path: Absolute directory path to list inside the session.
        """
        entries = await session.list_files(path)
        return "\n".join(entries)

    return [
        FunctionTool(execute),
        FunctionTool(read_file),
        FunctionTool(write_file),
        FunctionTool(list_files),
    ]
