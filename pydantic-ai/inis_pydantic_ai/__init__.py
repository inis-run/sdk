"""Pydantic AI integration for inis.run sandboxes.

- `inis_toolset()` — lightweight function-tool surface (execute_python,
  execute_shell, read_file, write_file, list_files). Attach-only: pass the
  ID of a session your app already created or attached to.
- `InisSandbox` — a capability (`Agent(capabilities=[...])`) following the
  current pydantic-ai-harness sandbox contract (run_command, read_file,
  write_file, list_directory). Same attach-only rule.
- `inis_lifecycle_toolset()` / `inis_ephemeral_toolset()` (in
  `inis_pydantic_ai.lifecycle`) — opt-in session/sandbox administration
  (pause/resume/checkpoint/restore/expose, one-shot sandboxes). Not exported
  at the top level on purpose: importing from `inis_pydantic_ai.lifecycle`
  is the deliberate step required before a model gets that control.
"""

from inis_pydantic_ai.sandbox import InisSandbox
from inis_pydantic_ai.toolset import InisToolset, inis_toolset

__all__ = ["inis_toolset", "InisToolset", "InisSandbox"]
