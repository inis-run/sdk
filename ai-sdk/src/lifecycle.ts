/**
 * Opt-in tool bundles for session/sandbox administration.
 *
 * inisTools() (in tools.ts) deliberately excludes anything that creates,
 * destroys, pauses, exposes, or otherwise administers a session or sandbox
 * — the model-facing default exposes only the bounded execution/file
 * surface it needs, while credentials and lifecycle administration remain
 * host-controlled or explicitly opt-in. The two functions here are that
 * opt-in: import from "@inis-run/ai-sdk/lifecycle" and spread into `tools`
 * alongside inisTools() only when your app deliberately wants the model to
 * have this control.
 *
 *   import { inisTools } from "@inis-run/ai-sdk";
 *   import { inisLifecycleTools } from "@inis-run/ai-sdk/lifecycle";
 *
 *   const result = streamText({
 *     model,
 *     tools: { ...inisTools({ sessionId }), ...inisLifecycleTools({ sessionId }) },
 *     stopWhen: stepCountIs(5),
 *   });
 */

import { tool } from "ai";
import { InisError, Session } from "@inis-run/sdk";
import { z } from "zod";

import type { InisClientOptions, InisToolsOptions } from "./tools.js";
import { _makeClient } from "./tools.js";

/**
 * Opt-in toolset giving the model pause/resume/checkpoint/restore/expose/
 * capture-artifacts control over one existing session.
 *
 * Attaches to `sessionId` only — never creates or destroys the session
 * (same contract as inisTools()). Every tool here is administration, not
 * execution: adding this hands the model the ability to pause the sandbox,
 * roll it back to a checkpoint, or open a public preview URL for it. Do
 * this deliberately, not by default.
 */
export function inisLifecycleTools(opts: InisToolsOptions) {
  if (!opts.sessionId) {
    throw new InisError("inisLifecycleTools() requires sessionId — see inisTools().");
  }
  const client = _makeClient(opts);
  const session = Session.attach(client, opts.sessionId);

  return {
    expose_port: tool({
      description: "Expose a guest port and return the HTTPS preview URL.",
      inputSchema: z.object({
        port: z.number().int().min(1).max(65535).describe("Guest port to expose"),
      }),
      execute: async ({ port }) => {
        const preview = await session.expose(port);
        return preview.previewUrl;
      },
    }),

    pause: tool({
      description:
        "Pause the current sandbox session to a snapshot. " +
        "The session can be resumed later. Returns the updated session state.",
      inputSchema: z.object({}),
      execute: async () => {
        const info = await session.pause();
        return `session_id: ${info.sessionId} state: ${info.state}`;
      },
    }),

    resume: tool({
      description:
        "Resume the current sandbox session from its paused snapshot. " +
        "Returns the updated session state.",
      inputSchema: z.object({}),
      execute: async () => {
        const info = await session.resume();
        return `session_id: ${info.sessionId} state: ${info.state}`;
      },
    }),

    checkpoint: tool({
      description:
        "Take a named, retained snapshot of the current session state. " +
        "The session keeps running after the checkpoint is captured. " +
        "Returns the checkpoint ID, which can be passed to restore later.",
      inputSchema: z.object({
        name: z
          .string()
          .optional()
          .describe("Optional checkpoint name (lowercase alphanumerics and hyphens)."),
        labels: z
          .record(z.string())
          .optional()
          .describe("Optional key/value metadata to attach to the checkpoint"),
      }),
      execute: async ({ name, labels }) => {
        const info = await session.checkpoint({ name, labels });
        return info.checkpointId;
      },
    }),

    restore: tool({
      description:
        "Roll the sandbox session back to a previously captured checkpoint in place. " +
        "The running VM is stopped and restarted from the checkpoint state. " +
        "Use this to reset to a clean baseline after running untrusted code.",
      inputSchema: z.object({
        checkpoint_id: z.string().describe("ID or name of the checkpoint to restore from."),
      }),
      execute: async ({ checkpoint_id }) => {
        const info = await session.restore(checkpoint_id);
        return `session_id: ${info.sessionId} state: ${info.state}`;
      },
    }),

    capture_artifacts: tool({
      description:
        "Capture files or glob patterns from /workspace to durable EU object storage. " +
        "Returns an artifact ID immediately; the upload runs in the background.",
      inputSchema: z.object({
        paths: z
          .array(z.string())
          .min(1)
          .describe(
            "Paths or glob patterns under /workspace to capture. " +
              'Examples: ["/workspace/output/**", "/workspace/report.pdf"].',
          ),
      }),
      execute: async ({ paths }) => {
        const artifact = await session.captureArtifacts(paths);
        return artifact.id;
      },
    }),
  };
}

/**
 * Opt-in toolset giving the model the ability to spin up and tear down its
 * own throwaway sandboxes, one per call.
 *
 * Unlike inisTools(), this does not attach to an existing session at all —
 * execute_once creates a brand-new sandbox for each call and destroys it
 * immediately after. Creation and deletion are exactly the kind of thing
 * that stays host-controlled or explicitly opt-in: handing a
 * model unbounded, per-call sandbox creation is a real resource/cost
 * control decision, not a default.
 */
export function inisEphemeralTools(opts: InisClientOptions = {}) {
  const client = _makeClient(opts);

  return {
    execute_once: tool({
      description:
        "Run code in a throwaway sandbox — no session is retained. " +
        "The sandbox is created, the code runs, and it is immediately destroyed. " +
        "Use for isolated one-shot execution that must not share state with any other tool call.",
      inputSchema: z.object({
        code: z.string().describe("Source code or shell command to execute"),
        language: z
          .enum(["python", "node", "bun"])
          .optional()
          .describe("Runtime language (default: python)"),
      }),
      execute: async ({ code, language }) => {
        const result = await client.execute({
          language: (language ?? "python") as "python" | "node" | "bun",
          code,
        });
        if (result.exitCode !== 0) {
          throw new InisError(
            `${language ?? "python"} exited ${result.exitCode}: ${result.stderr || result.stdout}`,
          );
        }
        if (result.stderr) {
          return `${result.stdout}\n[stderr]\n${result.stderr}`.trim();
        }
        return result.stdout;
      },
    }),
  };
}
