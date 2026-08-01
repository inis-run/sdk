"""A local, in-process fake of the inis.run session backend, used to
exercise the REAL example scripts' functions without any network call and
without creating a live inis.run session (the harness this repo runs under
has neither credentials nor permission to do that — see #1237).

This is not a wire-protocol fake. It patches `inis.client.Session`'s public
methods directly, the same technique
`sdk/pydantic-ai/tests/test_toolset.py` uses when it does
`monkeypatch.setattr(AsyncSession, "exec", fake_exec)` — proving the
example scripts' own logic (session-per-thread reuse, bounded tool
surface, concurrent isolation, cleanup-on-error, fork's real return shape)
rather than re-testing the SDK's own HTTP layer, which sdk/python/tests
already covers.

Every fake session is backed by a plain dict: files, named processes, and
lifecycle state. Good enough to prove the harness invariants this issue
cares about; not a substitute for a real sandbox.
"""

from __future__ import annotations

import io
import itertools
import re
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from typing import Any

import pytest
from inis.client import (
    ExecResult,
    ForkResult,
    InisError,
    ProcessInfo,
    Session,
    SessionInfo,
)

_HARD_MAX_FORK_COUNT = 16  # mirrors the server's hard fork-count cap


@dataclass
class FakeSessionState:
    session_id: str
    state: str = "live"
    files: dict[str, str] = field(default_factory=dict)
    processes: dict[str, ProcessInfo] = field(default_factory=dict)
    labels: dict[str, str] | None = None
    destroyed: bool = False
    # Paths a fake "ticker" background process writes to. persistent_tool_
    # loop.py starts a real `while true; do echo $i > path; sleep 1; done`
    # background process and expects that file's value to have advanced by
    # the next turn; this fake doesn't run a real shell loop, so it instead
    # bumps the value by one on each read — enough to prove the example's
    # own "value must have increased" assertion without a real subprocess.
    ticking_paths: set[str] = field(default_factory=set)


class FakeBackend:
    """Registry of fake sessions, plus the counter that mints their IDs."""

    def __init__(self) -> None:
        self.by_id: dict[str, FakeSessionState] = {}
        self._ids = itertools.count(1)

    def new_id(self) -> str:
        return f"ses_fake{next(self._ids):04d}"

    def get(self, session_id: str) -> FakeSessionState:
        rec = self.by_id.get(session_id)
        if rec is None or rec.destroyed:
            raise InisError("404: session not found", code="not_found", status=404, retryable=False)
        return rec

    def create(self, *, labels: dict[str, str] | None = None) -> FakeSessionState:
        session_id = self.new_id()
        rec = FakeSessionState(session_id=session_id, labels=labels)
        self.by_id[session_id] = rec
        return rec


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    """Patch every Session method the example scripts call, backed by an
    in-memory FakeBackend. Session.attach()/Client.sessions.create() are
    left as the REAL SDK code (they never touch the network themselves —
    attach() just stores an ID, and create()'s __enter__ is patched below),
    so this test suite still exercises the real call shape, just not the
    real HTTP transport underneath it."""
    backend = FakeBackend()

    def fake_enter(self: Session) -> Session:
        if self.session_id:
            return self
        rec = backend.create(labels=self._labels)
        self.session_id = rec.session_id
        return self

    def fake_close(self: Session) -> None:
        pass  # no real HTTP transport was ever opened

    def fake_exec(self: Session, command, *, cwd=None, timeout_ms=None) -> ExecResult:
        rec = backend.get(self.session_id)
        if isinstance(command, str):
            command = ["bash", "-lc", command]
        # The example scripts only ever exec a small, recognizable set of
        # shapes against the fake filesystem below — enough to prove their
        # own control flow, not a real shell.
        if command[:1] == ["cat"]:
            path = command[1]
            if path not in rec.files:
                return ExecResult(stdout="", stderr=f"cat: {path}: No such file or directory\n", exit_code=1, duration_ms=1, timed_out=False)
            return ExecResult(stdout=rec.files[path], stderr="", exit_code=0, duration_ms=1, timed_out=False)
        if command[:2] == ["mkdir", "-p"]:
            return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=1, timed_out=False)
        if command[:1] == ["sleep"] and len(command) == 2:
            seconds = float(command[1])
            timed_out = timeout_ms is not None and timeout_ms < seconds * 1000
            return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=int(timeout_ms or seconds * 1000), timed_out=timed_out)
        if command[:2] == ["bash", "-lc"]:
            script = command[2]
            # branch_and_evaluate.py's candidates: "sleep 2; python3
            # /workspace/<name>.py" against a fake file that itself reads
            # /workspace/n.txt and prints a sum. Interpreted directly here
            # rather than actually shelling out.
            if "python3 /workspace/" in script:
                py_path = "/workspace/" + script.split("/workspace/", 1)[1].split()[0]
                code = rec.files.get(py_path, "")
                ns: dict[str, Any] = {"__builtins__": __builtins__, "open": _fake_open(rec)}
                buf = io.StringIO()
                try:
                    with redirect_stdout(buf):
                        exec(code, ns)  # noqa: S102 - test-only, fully local fake sandbox
                    return ExecResult(stdout=buf.getvalue(), stderr="", exit_code=0, duration_ms=1, timed_out=False)
                except Exception as e:  # pragma: no cover - candidate bugs are the point
                    return ExecResult(stdout=buf.getvalue(), stderr=str(e), exit_code=1, duration_ms=1, timed_out=False)
        return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=1, timed_out=False)

    def fake_write_file(self: Session, path: str, content, encoding: str = "text") -> None:
        rec = backend.get(self.session_id)
        rec.files[path] = content if isinstance(content, str) else content.decode()

    def fake_read_file(self: Session, path: str, *, encoding: str = "text"):
        rec = backend.get(self.session_id)
        if path not in rec.files:
            raise InisError(f"404: {path} not found", code="not_found", status=404, retryable=False)
        if path in rec.ticking_paths:
            rec.files[path] = str(int(rec.files[path]) + 1)
        return rec.files[path]

    def fake_list_files(self: Session, path: str = "/workspace") -> list[str]:
        rec = backend.get(self.session_id)
        prefix = path.rstrip("/") + "/"
        return sorted({p[len(prefix):].split("/")[0] for p in rec.files if p.startswith(prefix)})

    def fake_get(self: Session) -> SessionInfo:
        rec = backend.get(self.session_id)
        return SessionInfo(session_id=rec.session_id, state=rec.state, labels=rec.labels)

    def fake_pause(self: Session) -> SessionInfo:
        rec = backend.get(self.session_id)
        rec.state = "paused"
        return SessionInfo(session_id=rec.session_id, state=rec.state, labels=rec.labels)

    def fake_resume(self: Session) -> SessionInfo:
        rec = backend.get(self.session_id)
        rec.state = "live"
        return SessionInfo(session_id=rec.session_id, state=rec.state, labels=rec.labels)

    def fake_destroy(self: Session, *, reason=None) -> None:
        if not self.session_id:
            return
        rec = backend.by_id.get(self.session_id)
        if rec is not None:
            rec.destroyed = True
        self.session_id = None

    def fake_fork(self: Session, count: int = 1) -> ForkResult:
        rec = backend.get(self.session_id)
        if count > _HARD_MAX_FORK_COUNT:
            raise InisError(
                f"400: count exceeds hard cap of {_HARD_MAX_FORK_COUNT}",
                code="bad_request",
                status=400,
                retryable=False,
            )
        children: list[str] = []
        for _ in range(count):
            child = backend.create(labels=rec.labels)
            child.files = dict(rec.files)  # fork seeds the child's filesystem from the parent
            children.append(child.session_id)
        return ForkResult(parent_session_id=rec.session_id, children=children)

    def fake_start_process(self: Session, name: str, command, *, cwd=None, keep_alive: bool = False) -> ProcessInfo:
        rec = backend.get(self.session_id)
        info = ProcessInfo(name=name, state="running", keep_alive=keep_alive)
        rec.processes[name] = info
        script = command if isinstance(command, str) else " ".join(command)
        match = re.search(r">\s*(\S+)", script)
        if match:
            path = match.group(1).rstrip(";")
            rec.files.setdefault(path, "0")
            rec.ticking_paths.add(path)
        return info

    def fake_get_process(self: Session, name: str) -> ProcessInfo:
        rec = backend.get(self.session_id)
        if name not in rec.processes:
            raise InisError(f"404: process {name} not found", code="not_found", status=404, retryable=False)
        return rec.processes[name]

    def fake_kill_process(self: Session, name: str) -> ProcessInfo:
        rec = backend.get(self.session_id)
        info = rec.processes.get(name)
        if info is None:
            raise InisError(f"404: process {name} not found", code="not_found", status=404, retryable=False)
        info.state = "exited"
        return info

    def fake_list_processes(self: Session) -> list[ProcessInfo]:
        rec = backend.get(self.session_id)
        return list(rec.processes.values())

    monkeypatch.setattr(Session, "__enter__", fake_enter)
    monkeypatch.setattr(Session, "close", fake_close)
    monkeypatch.setattr(Session, "exec", fake_exec)
    monkeypatch.setattr(Session, "write_file", fake_write_file)
    monkeypatch.setattr(Session, "read_file", fake_read_file)
    monkeypatch.setattr(Session, "list_files", fake_list_files)
    monkeypatch.setattr(Session, "get", fake_get)
    monkeypatch.setattr(Session, "pause", fake_pause)
    monkeypatch.setattr(Session, "resume", fake_resume)
    monkeypatch.setattr(Session, "destroy", fake_destroy)
    monkeypatch.setattr(Session, "fork", fake_fork)
    monkeypatch.setattr(Session, "start_process", fake_start_process)
    monkeypatch.setattr(Session, "get_process", fake_get_process)
    monkeypatch.setattr(Session, "kill_process", fake_kill_process)
    monkeypatch.setattr(Session, "list_processes", fake_list_processes)

    return backend


def _fake_open(rec: FakeSessionState):
    def open_(path: str, mode: str = "r"):
        return io.StringIO(rec.files.get(path, ""))

    return open_
