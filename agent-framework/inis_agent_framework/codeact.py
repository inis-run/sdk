"""Native inis.run CodeAct provider for Microsoft Agent Framework
(`agent_framework.ContextProvider` / `agent_framework.FunctionTool`,
package `agent-framework-core`).

Verified against `agent-framework-core==1.13.0` installed source, not blog
posts or the issue's original assumption. Worth recording explicitly:

1. `agent_framework.ContextProvider` / `FunctionTool` / `Content` / `Agent` /
   `AgentSession` are a **stable, public, documented extension point** --
   `agent-framework-core`'s own PyPI classifier is "5 - Production/Stable",
   and `ContextProvider` ships with a real docstring describing exactly this
   use ("participate in the context engineering pipeline, adding context
   before model invocation"). This is a materially different situation from
   this repo's OpenAI Agents SDK provider (an undocumented internal ABC) and
   closer to the Strands Agents provider (a stable public base class).
2. There is no formal "CodeAct sandbox provider" ABC to subclass, though.
   Unlike, say, LangChain Deep Agents' `SandboxBackendProtocol`, Microsoft's
   own two first-party CodeAct backends -- `agent_framework.monty`
   (`MontyCodeActProvider` / `MontyExecuteCodeTool`, package
   `agent-framework-monty`, an in-process Rust interpreter) and
   `agent_framework.hyperlight` (`HyperlightCodeActProvider`, a
   hypervisor-backed sandbox, not on PyPI at the time this was written) --
   are each just an ordinary `ContextProvider` + `FunctionTool` pair, with
   `MontyCodeActProvider`'s own docstring literally saying it "Mirrors
   agent_framework_hyperlight.HyperlightCodeActProvider ... for the subset
   of capabilities that apply". `InisCodeActProvider` below follows the same
   convention -- it is not a subclass of either Monty or Hyperlight's
   classes, since inis.run's execution backend (a real hardware-isolated VM
   session, not an in-process interpreter or a local hypervisor) has a
   different shape than both.
3. Both first-party backends -- and the "beta" CodeAct convention itself --
   are classified "4 - Beta" upstream (`agent-framework-monty`'s own
   `pyproject.toml`/README says so verbatim). `agent-framework-core` itself
   is stable; the CodeAct provider *pattern* built on top of it is young and
   could still change shape. Pin `inis-agent-framework` and re-verify before
   assuming forward compatibility, same as this package's own dependency pin
   on `agent-framework-core<2` (see pyproject.toml).
4. **Monty's own `execute_code` is NOT stateful across calls** -- every
   `MontyExecuteCodeTool._run_code` call builds a brand-new
   `InlineCodeBridge` with a fresh interpreter namespace (verified by
   reading `agent_framework_monty/_execute_code_tool.py` directly: no
   globals dict is cached anywhere across calls). `InisCodeActProvider`
   below is genuinely different here, not just differently-implemented:
   `execute_code` runs through `inis.AsyncSession.run_code()`, which is
   backed by a real persistent Python process inside the inis.run session
   (see `inis.interpreter_common`/`inis._kernel_script` in the core SDK) --
   variables set in one `execute_code` call are still there in the next one,
   across separate tool calls *and* across separate `agent.run()`
   invocations against the same session, until `restart_context()` is
   called or the session itself is torn down. This is the "preserve
   interpreter/session state across CodeAct steps" requirement from the
   issue this package implements, and it is a real capability gap in the
   other direction from what shipped first: Monty starts fresh every call.

Two ways to put an inis.run session behind an Agent Framework `Agent`
-------------------------------------------------------------------------

**1. Attached -- one inis.run session for the whole conversation
(recommended).** Create or attach to a session yourself; this provider never
creates or destroys it, and reconnects to the same persistent interpreter
context by session ID across process restarts:

    from inis import AsyncClient
    from agent_framework import Agent
    from inis_agent_framework import InisCodeActProvider

    client = AsyncClient()
    session = await client.sessions.create(egress_default="deny")
    codeact = InisCodeActProvider.wrap(session)

    agent = Agent(client=chat_client, name="assistant", context_providers=[codeact])
    await agent.run("Use execute_code to compute 6 * 7.")
    await session.destroy()  # this app owns the session; the provider never will

Reconnect a *later* process to the same persistent interpreter context by
session ID alone:

    codeact = InisCodeActProvider.attach("ses_abc123")  # new AsyncSession.attach() under the hood
    agent = Agent(client=chat_client, context_providers=[codeact])
    await agent.run("What was x again?")  # sees state from the earlier process

**2. Owned -- the provider creates and destroys the session for you**, for a
single bounded task:

    from inis_agent_framework import InisCodeActProvider

    codeact = await InisCodeActProvider.create(egress_default="deny")
    try:
        agent = Agent(client=chat_client, context_providers=[codeact])
        await agent.run("...")
    finally:
        await codeact.aclose()  # destroys the session

Explicitly not implemented (declared, not faked)
-------------------------------------------------------------------------
- **`call_tool` bridging.** Monty's own `execute_code` instructions advertise
  `await call_tool('name', **kwargs)` as an untyped fallback for calling
  host-registered tools from inside the sandbox -- this works for Monty
  because Monty's interpreter runs **in the same host process**, so
  `call_tool` is a direct in-process function call into a `tool_map` dict
  (see `agent_framework_monty/_execute_code_tool.py::_make_tool_callback`).
  inis.run's `execute_code` instead runs remote code inside a real
  hardware-isolated VM, communicating with the host over the kernel's
  file-based request/result protocol -- there is no channel in that protocol
  for the guest to pause mid-cell, ask the host to run an arbitrary
  function, and get the result back. Building one by, say, exposing a
  generic "call any host tool by name" RPC over that same channel would be
  exactly the kind of insecure partial bridge the issue this package
  implements explicitly warns against: it would let any code the sandboxed
  interpreter runs (including code an attacker smuggled into a prompt) invoke
  host-side tools with attacker-chosen arguments, with no scoping narrower
  than "every tool this agent has". `InisExecuteCodeTool` therefore does not
  accept a `tools=` parameter at all in v1 -- there is nothing to bridge, and
  no silent no-op pretending otherwise. Keep host-only tools (secrets,
  billing, anything with side effects the model shouldn't drive from inside
  the sandbox) as ordinary `Agent(tools=[...])` entries outside
  `context_providers`, per the issue's "keep host tools outside the sandbox"
  requirement -- that path is unaffected by this decision.
- **`workspace_root` / `file_mounts`.** Monty and Hyperlight both expose
  these as a way to bind a *host* directory into the sandbox, because their
  sandbox and the host share (or can share) a filesystem. inis.run's session
  runs on separate hardware from the host process entirely -- there is no
  host directory to bind-mount. The equivalent primitive is the inis SDK's
  own `session.read_file()`/`session.write_file()`, which the host calls
  directly against the *same* session this provider wraps; see the README.
- **.NET.** This package is Python-only, matching `agent-framework-core`'s
  Python package. There is no inis.run .NET SDK; this is not a gap in this
  package's scope, it is a settled absence upstream in this repo (see
  CLAUDE.md's project instructions) -- Microsoft's own reference CodeAct
  providers (Monty, Hyperlight) also ship Python-first, so this does not
  reflect a smaller surface than what those provide today.
- **True remote cancellation of an in-flight `execute_code` call.** Not for
  want of a cancellation primitive -- inis.run has a real one
  (`SIGTERM`->`SIGKILL`) for named background processes, and the interpreter
  kernel *is* one (`inis-kernel`, see `restart_context()`). The gap is
  contract shape: `run_code()` is a timeout-only call with no abort handle
  for `FunctionTool.invoke` to route through -- the same shape gap the
  OpenAI Agents SDK, Pydantic AI, and Vercel AI SDK adapters in this repo
  have for their own `exec()`-shaped contracts. A `timeout_ms` still ends
  the call and, per `run_code()`'s own contract, leaves the interpreter
  context intact for the next call rather than tearing it down.
"""

from __future__ import annotations

import json
from typing import Any

from agent_framework import (
    AgentSession,
    Content,
    ContextProvider,
    FunctionTool,
    SessionContext,
)

from inis import AsyncClient, AsyncSession, InisError
from inis.interpreter_common import InterpreterResult

EXECUTE_CODE_TOOL_NAME = "execute_code"

_EXECUTE_CODE_DESCRIPTION = (
    "Execute Python code in a persistent interpreter running inside an "
    "isolated inis.run session (a real hardware-isolated VM, not this "
    "process). Variables, imports, and function definitions persist across "
    "every call for this session -- across separate execute_code calls and "
    "separate conversation turns -- until the session is restarted or torn "
    "down. Surface results via print(...) (captured as text) or by ending "
    "with an expression whose value becomes the returned result (text/"
    "image/table/json, matching a Jupyter cell). There is no call_tool "
    "bridge to host-registered tools in this sandbox -- host tools stay "
    "outside execute_code."
)

_EXECUTE_CODE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "_ExecuteCodeInput",
    "properties": {
        "code": {
            "type": "string",
            "title": "Code",
            "description": "Python code to run in the session's persistent interpreter.",
        },
    },
    "required": ["code"],
}


def _table_content(result: InterpreterResult) -> Content:
    payload = {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": bool(result.truncated),
    }
    return Content.from_text(json.dumps(payload, ensure_ascii=False))


def _result_to_content(result: InterpreterResult) -> Content:
    """Map one `InterpreterResult` (see `inis.interpreter_common`'s type
    docstring: text/image/table/json/error) onto one `agent_framework.Content`.
    """
    if result.type == "text":
        return Content.from_text(result.text or "")
    if result.type == "image":
        return Content.from_data(
            data=result.data or b"",
            media_type=f"image/{result.format or 'png'}",
        )
    if result.type == "table":
        return _table_content(result)
    if result.type == "json":
        try:
            serialized = json.dumps(result.json, ensure_ascii=False)
        except (TypeError, ValueError):
            serialized = repr(result.json)
        return Content.from_text(serialized)
    # "error" (including run_code()'s own timeout_result -- see module
    # docstring: the interpreter context stays usable after either kind).
    return Content.from_error(
        message=result.ename or "Execution error",
        error_details=f"{result.ename}: {result.evalue}\n{result.traceback or ''}".strip(),
    )


class InisExecuteCodeTool(FunctionTool):
    """`execute_code` FunctionTool backed by one inis.run session's
    persistent interpreter (`AsyncSession.run_code()`).

    Does not accept a `tools=`/call_tool bridge parameter -- see the module
    docstring's "Explicitly not implemented" section for why building one
    would be an insecure partial bridge, not a missing convenience.
    """

    def __init__(self, session: AsyncSession, *, timeout_ms: int | None = None) -> None:
        super().__init__(
            name=EXECUTE_CODE_TOOL_NAME,
            description=_EXECUTE_CODE_DESCRIPTION,
            approval_mode="never_require",
            func=self._run_code,
            input_model=_EXECUTE_CODE_INPUT_SCHEMA,
        )
        self._session = session
        self._timeout_ms = timeout_ms

    async def _run_code(self, *, code: str) -> list[Content]:
        try:
            results = await self._session.run_code(code, timeout_ms=self._timeout_ms)
        except InisError as e:
            return [
                Content.from_error(
                    message="inis.run interpreter request failed",
                    error_details=f"{e.code}: {e}",
                )
            ]
        if not results:
            return [Content.from_text("Code executed successfully without output.")]
        return [_result_to_content(r) for r in results]


class InisCodeActProvider(ContextProvider):
    """`ContextProvider` injecting an inis.run-backed `execute_code` tool and
    CodeAct instructions, mirroring `MontyCodeActProvider` /
    `HyperlightCodeActProvider`'s shape (see module docstring for what is
    deliberately different: no `tools=`/call_tool bridge, no
    `workspace_root`/`file_mounts`, and a genuinely persistent interpreter
    context instead of a fresh one per call).

    Construct via `InisCodeActProvider.wrap()` (attach an existing session),
    `.attach()` (reconnect by session ID alone), or `.create()` (own a new
    session's lifecycle) -- not directly.
    """

    DEFAULT_SOURCE_ID = "inis_codeact"

    def __init__(
        self,
        session: AsyncSession,
        *,
        source_id: str = DEFAULT_SOURCE_ID,
        timeout_ms: int | None = None,
        _owned_client: AsyncClient | None = None,
    ) -> None:
        super().__init__(source_id)
        if not session.session_id:
            raise ValueError(
                "InisCodeActProvider requires an already-created/attached "
                "session (session.session_id is not set)"
            )
        self._session = session
        self._tool = InisExecuteCodeTool(session, timeout_ms=timeout_ms)
        # Only set when this instance was built by .create() and also
        # created its own AsyncClient (not passed one) -- see aclose().
        self._owned_client = _owned_client

    @classmethod
    def wrap(
        cls,
        session: AsyncSession,
        *,
        source_id: str = DEFAULT_SOURCE_ID,
        timeout_ms: int | None = None,
    ) -> InisCodeActProvider:
        """Wrap an inis.run session the host already created or attached to.

        This provider never creates or destroys the underlying session --
        call `await session.destroy()` (the `AsyncSession`, not this
        provider) yourself when the conversation is over. `aclose()` on a
        wrapped provider is a no-op.
        """
        return cls(session, source_id=source_id, timeout_ms=timeout_ms)

    @classmethod
    def attach(
        cls,
        session_id: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        source_id: str = DEFAULT_SOURCE_ID,
        timeout_ms: int | None = None,
    ) -> InisCodeActProvider:
        """Reconnect to an existing inis.run session by ID alone -- the
        session's persistent interpreter context (variables, imports) is
        still there, exactly as `run_code()`'s own contract promises,
        whether this is a new process or a later turn in the same one.
        Never destroys the session; see `wrap()`.
        """
        session = AsyncSession.attach(session_id, base_url=base_url, token=api_key)
        return cls(session, source_id=source_id, timeout_ms=timeout_ms)

    @classmethod
    async def create(
        cls,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        source_id: str = DEFAULT_SOURCE_ID,
        timeout_ms: int | None = None,
        **session_kwargs: Any,
    ) -> InisCodeActProvider:
        """Create a new inis.run session and own its lifecycle.

        `session_kwargs` is forwarded to `AsyncClient.sessions.create(...)`
        (`template`, `size`, `egress_default`, `egress_allow`, `name`,
        `labels`, `idle_timeout_ms`, `max_lifetime_ms`, `connections`, ...;
        `connections` binds this session to one or more external API origins
        with a credential the platform injects only at request time). Call
        `await provider.aclose()` when the conversation is over -- nothing
        in Agent Framework calls it automatically (`ContextProvider` has no
        lifecycle hooks of its own beyond `before_run`/`after_run`).
        """
        client = AsyncClient(token=api_key, base_url=base_url)
        try:
            session = await client.sessions.create(**session_kwargs)
        except Exception:
            await client.close()
            raise
        return cls(session, source_id=source_id, timeout_ms=timeout_ms, _owned_client=client)

    async def aclose(self) -> None:
        """Destroy the session and close the owned client -- owned mode
        (`.create()`) only. A no-op for `.wrap()`/`.attach()`, matching
        every other adapter in this repo's attach/own split."""
        if self._owned_client is None:
            return
        try:
            await self._session.destroy()
        except InisError:
            pass  # best-effort: already gone is fine
        await self._owned_client.close()

    async def restart_context(self) -> None:
        """Discard the persistent interpreter context: kills the running
        kernel process (if any) and starts a fresh one on the next
        `execute_code` call. Does not affect the underlying inis.run
        session's filesystem or lifecycle -- only its Python interpreter
        state."""
        await self._session.restart_context()

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession | None,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """Inject CodeAct instructions and the `execute_code` tool before
        each run."""
        state["session_id"] = self._session.session_id
        context.extend_instructions(
            self.source_id,
            (
                f"You have one primary tool: `{EXECUTE_CODE_TOOL_NAME}`. It runs Python "
                f"in a persistent interpreter inside an isolated inis.run session "
                f"(session id: {self._session.session_id}). Variables set in one call "
                f"are still there in the next -- including in a later conversation "
                f"turn -- so prefer incremental `execute_code` calls over "
                f"recomputing state from scratch.\n\n"
                f"Surface results via print(...) or a final expression. Uncaught "
                f"exceptions are reported as errors but do not reset the interpreter "
                f"-- your variables are still there afterward.\n\n"
                f"There is no host-tool bridge inside `{EXECUTE_CODE_TOOL_NAME}`: it "
                f"cannot call any tool registered directly on the agent. Filesystem "
                f"access is a real, persistent /workspace inside the session -- not "
                f"this process's filesystem. Network egress is deny-by-default unless "
                f"the host configured this session otherwise."
            ),
        )
        context.extend_tools(self.source_id, [self._tool])
