/**
 * Framework-level tests: the real Mastra `Workspace` + `createWorkspaceTools()`
 * path (the actual mechanism Mastra injects into an agent's tool set), driving
 * the real `InisSandbox`/`InisProcessManager` adapter code against
 * `FakeSession`. Only the transport inside `FakeSession` is fake; `Workspace`,
 * `createWorkspaceTools()`, `InisSandbox`, and `InisProcessManager` all run
 * for real.
 *
 * This calls each generated tool's `.execute()` directly rather than driving
 * a full `Agent` + language model loop. Disclosed rather than presented as
 * equivalent to an end-to-end agent test: a Mastra tool object IS just
 * `{ execute(input) }` under the hood (confirmed by reading
 * `@mastra/core/dist/tools/tool.d.ts`) — a scripted/fake model's only real
 * effect on this path is choosing which tool to call with which arguments,
 * which is exactly what each test below does explicitly. The exact wording
 * asserted below (e.g. "Process X has been killed.") was confirmed by
 * running the same tool against the upstream `LocalSandbox` first, not
 * guessed from source.
 */
import { describe, expect, it } from "vitest";
import { createWorkspaceTools, Workspace } from "@mastra/core/workspace";
import { InisSandbox } from "./sandbox.js";
import { asSession, FakeSession } from "./fake-session.js";

describe("real Workspace + createWorkspaceTools() (real framework path, fake Session)", () => {
  it("foreground mastra_workspace_execute_command reaches the real InisSandbox default executeCommand (spawn+wait)", async () => {
    const raw = new FakeSession("sess_tools_fg");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    const workspace = new Workspace({ sandbox });
    const tools = await createWorkspaceTools(workspace);

    const result = await tools.mastra_workspace_execute_command.execute({ command: "echo hello-tool" });

    expect(String(result)).toContain("hello-tool");
    // Proves it went through spawn(), not some other path: a real named
    // process was created server-side for this call.
    expect(raw.getStartProcessLog().some((p) => p.command.join(" ").includes("echo hello-tool"))).toBe(true);
  });

  it("background execute_command + kill_process reach the real InisProcessManager spawn()/kill()", async () => {
    const raw = new FakeSession("sess_tools_bg");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    const workspace = new Workspace({ sandbox });
    const tools = await createWorkspaceTools(workspace);

    const spawnResult = await tools.mastra_workspace_execute_command.execute({
      command: "long-running-marker",
      background: true,
    });
    const match = /PID:\s*(\S+)\)/.exec(String(spawnResult));
    expect(match).not.toBeNull();
    const pid = match![1];

    const killResult = await tools.mastra_workspace_kill_process.execute({ pid });
    expect(String(killResult)).toContain("has been killed");
  });

  it("killing an unknown/already-finished pid reports not found rather than throwing", async () => {
    const raw = new FakeSession("sess_tools_bg2");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    const workspace = new Workspace({ sandbox });
    const tools = await createWorkspaceTools(workspace);

    const killResult = await tools.mastra_workspace_kill_process.execute({ pid: "totally-unknown-pid" });
    expect(String(killResult)).toContain("was not found or had already exited");
  });

  it("get_process_output with wait:true returns the real captured stdout and exit code", async () => {
    const raw = new FakeSession("sess_tools_wait");
    const sandbox = new InisSandbox({ session: asSession(raw) });
    const workspace = new Workspace({ sandbox });
    const tools = await createWorkspaceTools(workspace);

    const spawnResult = await tools.mastra_workspace_execute_command.execute({
      command: "echo waited-output",
      background: true,
    });
    const pid = /PID:\s*(\S+)\)/.exec(String(spawnResult))![1];

    const output = await tools.mastra_workspace_get_process_output.execute({ pid, wait: true });
    expect(String(output)).toContain("waited-output");
    expect(String(output)).toContain("Exit code: 0");
  });

  it("two concurrent Workspaces over two attached sandboxes stay isolated end to end", async () => {
    const rawA = new FakeSession("sess_tools_iso_a");
    const rawB = new FakeSession("sess_tools_iso_b");
    const workspaceA = new Workspace({ sandbox: new InisSandbox({ session: asSession(rawA) }) });
    const workspaceB = new Workspace({ sandbox: new InisSandbox({ session: asSession(rawB) }) });
    const [toolsA, toolsB] = await Promise.all([
      createWorkspaceTools(workspaceA),
      createWorkspaceTools(workspaceB),
    ]);

    const [resultA, resultB] = await Promise.all([
      toolsA.mastra_workspace_execute_command.execute({ command: "echo marker-A" }),
      toolsB.mastra_workspace_execute_command.execute({ command: "echo marker-B" }),
    ]);

    expect(String(resultA)).toContain("marker-A");
    expect(String(resultA)).not.toContain("marker-B");
    expect(String(resultB)).toContain("marker-B");
    expect(String(resultB)).not.toContain("marker-A");
    expect(rawA.getStartProcessLog().some((p) => p.command.join(" ").includes("marker-B"))).toBe(false);
    expect(rawB.getStartProcessLog().some((p) => p.command.join(" ").includes("marker-A"))).toBe(false);
  });
});
