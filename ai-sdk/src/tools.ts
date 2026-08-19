import { tool } from "ai";
import { Client, InisError, Session, type ConnectionCreate } from "@inis-run/sdk";
import { z } from "zod";

export interface InisClientOptions {
  /** API key; defaults to $INIS_API_KEY */
  apiKey?: string;
  /** API base URL; defaults to $INIS_BASE_URL or https://api.inis.run */
  baseUrl?: string;
  timeoutMs?: number;
}

export interface InisToolsOptions extends InisClientOptions {
  /**
   * ID of a session your app already created or attached to. Required:
   * inisTools() only ever attaches, never creates or discovers a session on
   * its own: one agent conversation maps to one session, with no global
   * mutable session state. Never destroyed by this call — see
   * `createOwnedInisTools()` if you want the tools package to own creating
   * (and destroying) the session for you.
   */
  sessionId: string;
}

type InisToolSet = ReturnType<typeof buildExecutionTools>;

/** The bounded execution/file surface shared by inisTools() and
 * createOwnedInisTools() — built directly against an already-open Session,
 * so callers that already have one (the owned path) don't re-attach. */
function buildExecutionTools(session: Session) {
  return {
    execute_python: tool({
      description: "Run Python code in the sandbox and return stdout.",
      inputSchema: z.object({
        code: z.string().describe("Python source code to execute"),
      }),
      execute: async ({ code }) => {
        const result = await session.exec(["python3", "-c", code]);
        if (result.exitCode !== 0) {
          throw new InisError(
            `python exited ${result.exitCode}: ${result.stderr || result.stdout}`,
          );
        }
        if (result.stderr) {
          return `${result.stdout}\n[stderr]\n${result.stderr}`.trim();
        }
        return result.stdout;
      },
    }),

    execute_shell: tool({
      description: "Run a shell command in the sandbox and return stdout.",
      inputSchema: z.object({
        command: z.string().describe("Shell command to run"),
      }),
      execute: async ({ command }) => {
        const result = await session.exec(command);
        if (result.exitCode !== 0) {
          throw new InisError(
            `command failed (exit ${result.exitCode}): ${result.stderr || result.stdout}`,
          );
        }
        return result.stdout;
      },
    }),

    read_file: tool({
      description: "Read a file from /workspace in the sandbox.",
      inputSchema: z.object({
        path: z.string().describe("Absolute or workspace-relative file path"),
      }),
      execute: async ({ path }) => session.readFile(path),
    }),

    write_file: tool({
      description: "Write a file under /workspace in the sandbox.",
      inputSchema: z.object({
        path: z.string().describe("Absolute or workspace-relative file path"),
        content: z.string().describe("File contents to write"),
      }),
      execute: async ({ path, content }) => {
        await session.writeFile(path, content);
        return `wrote ${content.length} bytes to ${path}`;
      },
    }),

    list_files: tool({
      description:
        "List the files and directories inside a path in the sandbox. " +
        "Defaults to /workspace. Returns a newline-separated list of entry names.",
      inputSchema: z.object({
        path: z
          .string()
          .default("/workspace")
          .describe("Absolute directory path to list inside the sandbox (defaults to /workspace)"),
      }),
      execute: async ({ path }) => {
        const entries = await session.listFiles(path);
        return entries.join("\n");
      },
    }),
  };
}

function makeClient(opts: InisClientOptions): Client {
  return new Client({ token: opts.apiKey, baseUrl: opts.baseUrl, timeoutMs: opts.timeoutMs });
}

/**
 * The smallest execution/file surface a model needs: execute_python,
 * execute_shell, read_file, write_file, list_files. Attach-only — pass the
 * ID of a session your app already owns or is attached to. Nothing here
 * creates, destroys, pauses, resumes, or exposes a session — the
 * model-facing default exposes only the bounded execution/file surface it
 * needs, while credentials and lifecycle administration remain
 * host-controlled or explicitly opt-in. For session/sandbox administration
 * or one-shot sandboxes, see `inisLifecycleTools()` / `inisEphemeralTools()` in
 * `@inis-run/ai-sdk/lifecycle` — deliberate opt-ins, not exported here.
 */
export function inisTools(opts: InisToolsOptions): InisToolSet {
  if (!opts.sessionId) {
    throw new InisError(
      "inisTools() requires sessionId: attach to a session your app already " +
        "created or is attached to. Use createOwnedInisTools() if you want " +
        "this call to create (and later destroy) the session for you.",
    );
  }
  const client = makeClient(opts);
  const session = Session.attach(client, opts.sessionId);
  return buildExecutionTools(session);
}

export interface OwnedInisTools {
  /** The same bounded execution/file tools inisTools() returns. */
  tools: InisToolSet;
  /** ID of the session this call created, for reconnecting on a later request. */
  sessionId: string;
  /** Destroys the session this call created. Call exactly once, when the
   * conversation is over — success or failure. */
  cleanup: () => Promise<void>;
}

/**
 * Deliberate owned-session factory: creates a NEW session, returns its
 * tools alongside the session ID and a cleanup handle that destroys it.
 * Unlike inisTools(), this call does create (and, via `cleanup`, destroy) a
 * session — that's the entire point, so it's a distinct, explicitly-named
 * function rather than an implicit "sessionId omitted -> auto-create" path
 * (the previous shape here claimed a null sessionRef auto-created when the
 * code actually threw).
 *
 * One call here = one owned session for one conversation. Do not share the
 * returned `tools`/`sessionId` across concurrent conversations — construct
 * a new one per conversation. There is no global mutable session state.
 */
export async function createOwnedInisTools(
  opts: InisClientOptions & {
    egress?: "allow" | "deny";
    /**
     * Inline Connections to create with the session: session-scoped
     * credential leases bound to specific external API origins. See
     * `ConnectionCreate` in `@inis-run/sdk` for the full shape.
     */
    connections?: ConnectionCreate[];
  } = {},
): Promise<OwnedInisTools> {
  const client = makeClient(opts);
  const createOpts: { egress?: { mode: "allow" | "deny" }; connections?: ConnectionCreate[] } = {};
  if (opts.egress) createOpts.egress = { mode: opts.egress };
  if (opts.connections) createOpts.connections = opts.connections;
  const session = await client.sessions.create(
    Object.keys(createOpts).length > 0 ? createOpts : undefined,
  );
  return {
    tools: buildExecutionTools(session),
    sessionId: session.sessionId,
    cleanup: () => session.destroy(),
  };
}

export { buildExecutionTools as _buildExecutionTools, makeClient as _makeClient };
