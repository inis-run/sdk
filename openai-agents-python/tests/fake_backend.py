"""A fake inis.run HTTP backend for deterministic tests.

Backs `httpx.MockTransport` with a real local subprocess + a real temp
directory instead of hand-simulated shell semantics. This lets
`InisSandboxSession`'s runtime-helper install, symlink-safe path
resolution, and tar-based persist/hydrate run through *real* `sh`/`tar`/
`base64`/`mkdir` behaviour rather than a pattern-matched guess at what
those commands would do — the only thing faked is that the "session" lives
on this machine instead of inis.run's infrastructure. Everything upstream
of this (the real `inis.AsyncSession`/`AsyncClient` HTTP client, and the
real `InisSandboxSession`/`InisSandboxClient` adapter code) runs for real.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import tempfile
from pathlib import Path

import httpx


class LocalProcessInisBackend:
    def __init__(self, session_id: str, *, workspace_root: str = "/workspace") -> None:
        self.session_id = session_id
        self.state = "live"
        self.destroyed = False
        self.exec_log: list[list[str]] = []
        self.force_truncated = False
        self._tmp = Path(tempfile.mkdtemp(prefix="inis-openai-agents-test-"))
        self.workspace_root = workspace_root
        (self._tmp / workspace_root.lstrip("/")).mkdir(parents=True, exist_ok=True)

    def cleanup(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _under_workspace_root(self, sandbox_path: str) -> bool:
        root_prefix = self.workspace_root.rstrip("/") + "/"
        return sandbox_path == self.workspace_root or sandbox_path.startswith(root_prefix)

    def _real_path(self, sandbox_path: str) -> Path:
        # Only workspace-rooted paths (e.g. "/workspace/x") are redirected
        # under our real temp root, matching `_run`'s rewrite policy below
        # so a file the `/files` endpoints write and a file `exec` later
        # reads agree on the same real location. Anything else (chiefly the
        # runtime helper's own /tmp install/scratch paths) resolves to its
        # real, unrewritten path — a harmless side effect for a test
        # double, and required so exec-side tar/base64 commands and
        # `write_file`-based scratch files land in the same place.
        if self._under_workspace_root(sandbox_path):
            return self._tmp / sandbox_path.lstrip("/")
        return Path(sandbox_path)

    async def _run(self, command: list[str], timeout_ms: int | None) -> dict:
        # Two shapes show up here: plain argv items that ARE a workspace
        # path outright (the runtime helper's own path-resolution/install
        # commands, run with shell=False), and one-string shell scripts
        # that merely *mention* the workspace root inline (this adapter's
        # own tar/base64 persist+hydrate commands, run as "bash -lc
        # <script>"). A substring replace handles both: an exact-match
        # argv item is a substring of itself, and it also rewrites the
        # workspace root wherever it appears inside a larger script string.
        real_workspace = str(self._tmp / self.workspace_root.lstrip("/"))
        rewritten = [part.replace(self.workspace_root, real_workspace) for part in command]
        proc = await asyncio.create_subprocess_exec(
            *rewritten,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            timeout_s = (timeout_ms / 1000) if timeout_ms else 30
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            timed_out = False
            exit_code = proc.returncode or 0
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            stdout, stderr, timed_out, exit_code = b"", b"", True, -1
        return {
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "exit_code": exit_code,
            "timed_out": timed_out,
        }

    async def handler(self, request: httpx.Request) -> httpx.Response:
        # httpx.MockTransport awaits an async handler directly (see
        # `handle_async_request`), so this runs on the same event loop as
        # the real `AsyncSession`/`AsyncClient` HTTP call it is standing in
        # for — no nested-loop juggling needed.
        path = request.url.path
        method = request.method
        prefix = f"/v1/sessions/{self.session_id}"

        if path == prefix and method == "GET":
            return httpx.Response(200, json={"session_id": self.session_id, "state": self.state})

        if path == prefix and method == "DELETE":
            self.destroyed = True
            return httpx.Response(204)

        if path == f"{prefix}/exec" and method == "POST":
            body = json.loads(request.content)
            command = body["command"]
            # Only record the user-visible shell command in `exec_log` —
            # runtime-helper install/probe commands (symlink-safe path
            # resolution machinery inherited from the ABC) are real but not
            # what these tests assert on.
            if command[:2] in (["sh", "-lc"], ["bash", "-lc"]):
                self.exec_log.append(command)
            result = await self._run(command, body.get("timeout_ms"))
            result["truncated"] = self.force_truncated
            return httpx.Response(200, json=result)

        if path == f"{prefix}/files" and method == "GET":
            sandbox_path = request.url.params["path"]
            op = request.url.params.get("op", "read")
            # Same real-API restriction as the PUT handler above (confirmed
            # live: reading e.g. /tmp/outside.txt returns this same 400).
            if not self._under_workspace_root(sandbox_path):
                return httpx.Response(
                    400, json={"error": "path must be under /workspace", "code": "invalid_path"}
                )
            real = self._real_path(sandbox_path)
            if op == "list":
                if not real.is_dir():
                    return httpx.Response(404, json={"error": "not a directory", "code": "not_found"})
                return httpx.Response(200, json={"entries": sorted(p.name for p in real.iterdir())})
            if not real.is_file():
                return httpx.Response(404, json={"error": "file not found", "code": "not_found"})
            data = real.read_bytes()
            if request.url.params.get("encoding") == "base64":
                return httpx.Response(200, json={"content": base64.b64encode(data).decode("ascii")})
            return httpx.Response(200, json={"content": data.decode("utf-8", errors="replace")})

        if path == f"{prefix}/files" and method == "PUT":
            body = json.loads(request.content)
            # Match the real api.inis.run behaviour, confirmed live: the
            # /files endpoints reject any path outside /workspace, even
            # though a bare exec() is unrestricted. This fake used to skip
            # that check entirely, which is exactly how
            # `hydrate_workspace()`'s old `/tmp/...` scratch path shipped
            # broken against the real API despite passing every test here.
            if not self._under_workspace_root(body["path"]):
                return httpx.Response(
                    400, json={"error": "path must be under /workspace", "code": "invalid_path"}
                )
            real = self._real_path(body["path"])
            real.parent.mkdir(parents=True, exist_ok=True)
            content = body["content"]
            if body.get("encoding") == "base64":
                real.write_bytes(base64.b64decode(content))
            else:
                real.write_text(content)
            return httpx.Response(200, json={})

        if path == f"{prefix}/files" and method == "DELETE":
            sandbox_path = request.url.params["path"]
            real = self._real_path(sandbox_path)
            if real.is_dir():
                shutil.rmtree(real, ignore_errors=True)
            else:
                real.unlink(missing_ok=True)
            return httpx.Response(204)

        if path == f"{prefix}/files/mkdir" and method == "POST":
            body = json.loads(request.content)
            self._real_path(body["path"]).mkdir(parents=True, exist_ok=True)
            return httpx.Response(200, json={})

        if path == f"{prefix}/expose" and method == "POST":
            body = json.loads(request.content)
            port = body["port"]
            return httpx.Response(
                200,
                json={
                    "session_id": self.session_id,
                    "port": port,
                    "preview_url": f"https://{self.session_id}-{port}.preview.inis.test",
                },
            )

        return httpx.Response(500, json={"error": f"unhandled {method} {path}"})


def attach_fake_session(session_id: str, *, workspace_root: str = "/workspace"):
    """Returns (AsyncSession, LocalProcessInisBackend) wired together."""
    from inis import AsyncSession

    backend = LocalProcessInisBackend(session_id, workspace_root=workspace_root)
    session = AsyncSession.attach(session_id, base_url="https://fake.inis.test", token="test-token")
    session._client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
    return session, backend
