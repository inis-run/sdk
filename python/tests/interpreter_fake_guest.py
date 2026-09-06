"""A faithful in-process simulation of the code-interpreter guest side,
shared by tests/test_session_interpreter.py and
tests/test_async_session_interpreter.py.

Rather than faking the wire protocol with canned responses, this drives the
*real* inis._kernel_script.run_cell() against an in-memory "globals" dict
that stands in for the kernel process's memory — so these tests exercise the
actual cell-execution/rich-output logic, not a stand-in for it. It only
fakes the parts a real guest VM would otherwise provide: the filesystem
(a dict of path -> bytes), named-process lifecycle, and exec() dispatch.

Starting a "process" resets the simulated globals — the same discontinuity
a real kernel restart (OOM, manual kill_process, a fresh install) causes.
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
from typing import Any

import httpx

from inis import _kernel_script as kernel
from tests.conftest import error_response, json_response


class FakeInterpreterGuest:
    def __init__(self, session_id: str = "sess_123", base_url: str = "https://api.inis.run"):
        self.session_id = session_id
        self._prefix = f"{base_url}/v1/sessions/{session_id}"
        self.process_state: str | None = None  # None | "running" | "exited"
        self.files: dict[str, bytes] = {}
        self.globals: dict[str, Any] = {"__name__": "__main__"}
        self.exec_scripts: list[str] = []
        kernel.ART_DIR = tempfile.mkdtemp()

    # ── httpx handler ─────────────────────────────────────────────────────
    def handle(self, method: str, path: str, params: dict | None, body: Any) -> httpx.Response:
        assert path.startswith(self._prefix), f"unexpected path {path}"
        suffix = path[len(self._prefix) :]
        if method == "POST" and suffix == "/exec":
            return self._exec(body)
        if method == "PUT" and suffix == "/files":
            return self._write(body)
        if method == "GET" and suffix == "/files":
            return self._read(params or {})
        if method == "POST" and suffix == "/processes":
            self.process_state = "running"
            self.globals = {"__name__": "__main__"}
            return json_response(200, {"name": body["name"], "state": "running", "pid": 111})
        if method == "GET" and suffix == "/processes/inis-kernel":
            if self.process_state is None:
                return error_response(404, "process not found")
            return json_response(200, {"name": "inis-kernel", "state": self.process_state})
        if method == "DELETE" and suffix == "/processes/inis-kernel":
            had = self.process_state
            self.process_state = "exited"
            return json_response(
                200,
                {"name": "inis-kernel", "state": "exited", "exit_code": 143 if had == "running" else None},
            )
        raise AssertionError(f"unhandled request: {method} {suffix} {body}")

    def _write(self, body: dict) -> httpx.Response:
        content = body["content"]
        encoding = body.get("encoding", "text")
        self.files[body["path"]] = base64.b64decode(content) if encoding == "base64" else content.encode()
        return json_response(200, {})

    def _read(self, params: dict) -> httpx.Response:
        path = params["path"]
        if path not in self.files:
            return error_response(404, "not found")
        content = self.files[path]
        if params.get("encoding") == "base64":
            return json_response(200, {"content": base64.b64encode(content).decode()})
        return json_response(200, {"content": content.decode()})

    def _exec_result(self, stdout: str) -> httpx.Response:
        return json_response(
            200, {"stdout": stdout, "stderr": "", "exit_code": 0, "duration_ms": 1, "timed_out": False}
        )

    def _exec(self, body: dict) -> httpx.Response:
        script = body["command"][-1]
        self.exec_scripts.append(script)
        if script.startswith("mkdir -p"):
            return self._exec_result("")

        m = re.search(r"mv '([^']+)' '([^']+)'", script)
        assert m, f"exec script wasn't mkdir or a run_code wait command: {script!r}"
        tmp_path = m.group(1)
        timeout_marker = json.dumps({"__inis_timeout__": True})

        raw = self.files.pop(tmp_path, None)
        if raw is None or self.process_state != "running":
            return self._exec_result(timeout_marker)

        req = json.loads(raw.decode())
        results = kernel.run_cell(req["code"], req["id"], self.globals)
        for r in results:
            if r.get("type") == "image":
                with open(r["path"], "rb") as f:
                    self.files[r["path"]] = f.read()
        return self._exec_result(json.dumps({"id": req["id"], "results": results}))
