"""Tests for `InisSandboxBackend` against a fake local-subprocess inis.run
backend (see `fake_backend.py`) — no production secret, no network.

`TestInisSandboxBackendStandard` subclasses the upstream conformance suite
(`langchain_tests.integration_tests.SandboxIntegrationTests`, package
`langchain-tests`) exactly the way its own docstring and the "Contributing
a sandbox integration" guide (docs.langchain.com/oss/python/contributing/
integrations-langchain) describe — this is the real, framework-blessed
test path for a new sandbox backend, not a bespoke replacement for it.

Everything below `TestInisSandboxBackendStandard` covers what that generic
suite can't know about this specific backend: owned-vs-attached lifetime
(owned sessions must never leak, attached sessions must never be destroyed), structured-error mapping, timeout/truncation
markers, and that two backends attached to two different session ids never
share state.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from langchain_tests.integration_tests import SandboxIntegrationTests

from inis import ConnectionSpec
from langchain_inis import InisSandboxBackend

from .fake_backend import attach_fake_session, make_fake_inis_sandbox_backend


def _session_id() -> str:
    return f"ses_test_{uuid.uuid4().hex[:12]}"


# A real, writable, deterministic absolute directory on this machine — NOT
# the literal string "/workspace". See `fake_backend.LocalProcessInisBackend`'s
# class docstring: `BaseSandbox`'s own derived shell scripts (ls/read/grep/
# glob/write-preflight) embed the exact path a caller gave them, often
# base64-encoded, so a fake backend must make that path genuinely real
# rather than rewrite a virtual "/workspace" prefix inside those scripts.
_CONFORMANCE_WORKSPACE_ROOT = Path(tempfile.mkdtemp(prefix="langchain-inis-conformance-"))

# Second genuine three-way upstream contradiction (see
# `test_write_existing_file_fails` below for the first): `deepagents`
# 0.7.9 rewrote `BaseSandbox`'s remote glob script (changelog + diff read
# against 0.7.1, the version pinned when this adapter shipped) to fix two
# real bugs in the pre-0.7.9 sandbox-specific glob path, neither of which
# this adapter's own code controls -- `InisSandboxBackend` never overrides
# `glob`/`aglob`; both come from `BaseSandbox` verbatim:
#
# 1. Pre-0.7.9 returned matches relative to the search root. deepagents'
#    own `_absolutize_glob_path` docstring (installed
#    `deepagents/backends/sandbox.py`) explains why that was a bug, not a
#    style choice: "`glob`'s tool contract is absolute paths, and
#    `_check_fs_permission` only matches absolute patterns -- a relative
#    path silently bypasses every `deny` rule." `FilesystemBackend.glob`
#    (deepagents' in-process backend) and this very suite's own
#    `test_ls_lists_files` (`self.sandbox_path(..., root_dir=...)`,
#    i.e. absolute) already expected absolute -- sandbox glob was the
#    outlier, and 0.7.9 brings it in line.
# 2. Pre-0.7.9 sandbox glob included directories in its matches
#    (`test_glob_with_directories` below expects `is_dir` entries).
#    0.7.9's remote script filters to regular files only, "mirroring
#    FilesystemBackend.glob's is_file() filter" (same docstring) -- again
#    bringing sandbox glob's behavior in line with every other backend's.
#
# Both fixes are deliberate, upstream, and correct (the first closes an
# actual permission-check bypass); this adapter has nothing to fix. But
# `langchain-tests` 1.1.9 -- already at the ceiling of this package's own
# `>=1.1,<2` pin, so it did not move -- still encodes the pre-0.7.9
# sandbox-specific contract in six of its glob assertions. strict=True is
# verified to bite (monkeypatching the pre-0.7.9 relative/directories-
# included behavior back in flips these to XPASS). Worth reporting to
# `langchain-tests` upstream so the suite catches up with deepagents.
_GLOB_ABSOLUTE_AND_FILES_ONLY_XFAIL_REASON = (
    "deepagents>=0.7.9 (see pyproject.toml) made BaseSandbox.glob() return "
    "absolute paths (closing a real _check_fs_permission deny-rule bypass) "
    "and files-only (matching FilesystemBackend.glob), matching this "
    "suite's own absolute-path ls assertions -- but langchain-tests==1.1.9 "
    "(already at this package's pin ceiling; it did not move) still "
    "asserts the pre-0.7.9 sandbox-specific contract (relative paths / "
    "directories included). Not an adapter bug: InisSandboxBackend never "
    "overrides glob/aglob. See the block comment above this constant and "
    "inis.run#1850."
)


class TestInisSandboxBackendStandard(SandboxIntegrationTests):
    """Upstream `SandboxIntegrationTests` conformance suite, real adapter
    path (`InisSandboxBackend`), fake inis.run HTTP transport."""

    @property
    def sandbox_root_dir(self) -> str:
        # The suite's own default (`/tmp/test_sandbox_ops/`) would 400 if
        # this fake enforced the real inis.run file API's actual
        # `/workspace`-only restriction against the literal string
        # "/workspace" — this fake instead enforces that restriction
        # against `_CONFORMANCE_WORKSPACE_ROOT` (a real directory), so test
        # paths must be built under that same root.
        return f"{_CONFORMANCE_WORKSPACE_ROOT}/test_sandbox_ops/"

    @pytest.fixture(scope="class")
    @classmethod
    def sandbox(cls) -> Iterator[InisSandboxBackend]:
        backend, fake = make_fake_inis_sandbox_backend(
            _session_id(), workspace_root=str(_CONFORMANCE_WORKSPACE_ROOT)
        )
        try:
            yield backend
        finally:
            fake.cleanup()
            shutil.rmtree(_CONFORMANCE_WORKSPACE_ROOT, ignore_errors=True)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Genuine three-way upstream contradiction in deepagents==0.7.1 + "
            "langchain-tests==1.1.9, not something specific to this adapter: "
            "(1) BaseSandbox.write()'s docstring says 'creating or overwriting "
            "it if it already exists' (deepagents/backends/sandbox.py) and its "
            "code matches — _write_preflight only mkdir -p's the parent, never "
            "checks whether the target file itself exists; InisSandboxBackend "
            "does not override write(), so it inherits this documented "
            "overwrite behavior verbatim. (2) LangSmithSandbox.write() (the "
            "one in-tree third-party reference) calls that same "
            "_write_preflight and also has no existence check, despite its "
            "own docstring claiming it 'preserves the same existence check ... "
            "as BaseSandbox.write()' -- a claim BaseSandbox.write()'s actual "
            "source does not support. (3) This suite's own "
            "test_write_existing_file_fails asserts the opposite of both: "
            "that write() must error on an existing file. strict=True is "
            "verified to bite (monkeypatching a real existence check into "
            "BaseSandbox.write flips this to XPASS). Reported to the upstream "
            "project is tracked separately; update this reason with the issue "
            "URL once one exists."
        ),
    )
    def test_write_existing_file_fails(
        self, sandbox_backend: InisSandboxBackend, sandbox_test_root: str
    ) -> None:
        super().test_write_existing_file_fails(sandbox_backend, sandbox_test_root)

    @pytest.mark.xfail(strict=True, reason=_GLOB_ABSOLUTE_AND_FILES_ONLY_XFAIL_REASON)
    def test_glob(
        self, sandbox_backend: InisSandboxBackend, sandbox_test_root: str
    ) -> None:
        super().test_glob(sandbox_backend, sandbox_test_root)

    @pytest.mark.xfail(strict=True, reason=_GLOB_ABSOLUTE_AND_FILES_ONLY_XFAIL_REASON)
    def test_glob_basic_pattern(
        self, sandbox_backend: InisSandboxBackend, sandbox_test_root: str
    ) -> None:
        super().test_glob_basic_pattern(sandbox_backend, sandbox_test_root)

    @pytest.mark.xfail(strict=True, reason=_GLOB_ABSOLUTE_AND_FILES_ONLY_XFAIL_REASON)
    def test_glob_with_directories(
        self, sandbox_backend: InisSandboxBackend, sandbox_test_root: str
    ) -> None:
        super().test_glob_with_directories(sandbox_backend, sandbox_test_root)

    @pytest.mark.xfail(strict=True, reason=_GLOB_ABSOLUTE_AND_FILES_ONLY_XFAIL_REASON)
    def test_glob_hidden_files_explicitly(
        self, sandbox_backend: InisSandboxBackend, sandbox_test_root: str
    ) -> None:
        super().test_glob_hidden_files_explicitly(sandbox_backend, sandbox_test_root)

    @pytest.mark.xfail(strict=True, reason=_GLOB_ABSOLUTE_AND_FILES_ONLY_XFAIL_REASON)
    def test_glob_with_character_class(
        self, sandbox_backend: InisSandboxBackend, sandbox_test_root: str
    ) -> None:
        super().test_glob_with_character_class(sandbox_backend, sandbox_test_root)

    @pytest.mark.xfail(strict=True, reason=_GLOB_ABSOLUTE_AND_FILES_ONLY_XFAIL_REASON)
    def test_glob_with_question_mark(
        self, sandbox_backend: InisSandboxBackend, sandbox_test_root: str
    ) -> None:
        super().test_glob_with_question_mark(sandbox_backend, sandbox_test_root)


# -- owned vs. attached lifetime ---------------------------------------------


def test_attach_never_destroys_on_close() -> None:
    """`.attach()` + `close()`/`aclose()` must never destroy the session —
    only `.destroy()` may. Regression guard for the exact bug class a
    supported integration must never produce: destroying an owned run must
    never destroy a caller-owned attached session."""
    session, fake = attach_fake_session(_session_id())
    backend = InisSandboxBackend(session)
    try:
        backend.execute("echo hello")
        backend.close()
        assert fake.destroyed is False, "close() must not destroy an attached session"
    finally:
        fake.cleanup()


def test_destroy_marks_session_destroyed() -> None:
    """`.destroy()` (the owned path's cleanup call) does hit DELETE."""
    session, fake = attach_fake_session(_session_id())
    backend = InisSandboxBackend(session)
    try:
        backend.destroy()
        assert fake.destroyed is True
    finally:
        fake.cleanup()


def test_destroy_runs_on_failure_path() -> None:
    """Owned lifecycle cleanup must run on the exception path, not just the
    happy path."""
    session, fake = attach_fake_session(_session_id())
    backend = InisSandboxBackend(session)
    try:
        with pytest.raises(RuntimeError):
            try:
                backend.execute("echo before-failure")
                raise RuntimeError("simulated agent-loop failure mid-conversation")
            finally:
                backend.destroy()
        assert fake.destroyed is True, "finally-block destroy() must run even when the try body raised"
    finally:
        fake.cleanup()


def test_async_destroy_marks_session_destroyed() -> None:
    async def run() -> None:
        backend, fake = make_fake_inis_sandbox_backend(_session_id())
        try:
            await backend.adestroy()
            assert fake.destroyed is True
        finally:
            fake.cleanup()

    asyncio.run(run())


# -- exec: errors, timeout, truncation ----------------------------------------


class _RaisingSession:
    """Minimal stand-in whose `.exec()` raises, to exercise
    `InisSandboxBackend.execute()`'s `InisError` -> `ExecuteResponse`
    mapping without needing the fake HTTP transport to fabricate a 5xx."""

    session_id = "ses_raising"

    def __init__(self, error: Exception) -> None:
        self._error = error

    def exec(self, *args: object, **kwargs: object) -> object:
        raise self._error


def test_execute_maps_inis_error_instead_of_raising() -> None:
    from inis import InisError

    backend = InisSandboxBackend.__new__(InisSandboxBackend)
    backend._session = _RaisingSession(InisError("boom", code="internal_error", status=500))  # type: ignore[assignment]
    backend._async_session = None
    backend.enable_capture_offload = False

    result = backend.execute("false")
    assert result.exit_code is None
    assert "internal_error" in result.output


def test_execute_timeout_marks_output() -> None:
    session, fake = attach_fake_session(_session_id())
    backend = InisSandboxBackend(session)
    try:
        result = backend.execute("sleep 5", timeout=1)
        assert result.exit_code is None
        assert "timed out" in result.output
    finally:
        fake.cleanup()


def test_execute_truncated_flag_propagates() -> None:
    session, fake = attach_fake_session(_session_id())
    fake.force_truncated = True
    backend = InisSandboxBackend(session)
    try:
        result = backend.execute("echo hi")
        assert result.truncated is True
    finally:
        fake.cleanup()


def test_aexecute_matches_execute_for_same_command() -> None:
    async def run() -> None:
        backend, fake = make_fake_inis_sandbox_backend(_session_id())
        try:
            sync_result = backend.execute("echo from-sync")
            async_result = await backend.aexecute("echo from-async")
            assert sync_result.output.strip() == "from-sync"
            assert async_result.output.strip() == "from-async"
            assert sync_result.exit_code == async_result.exit_code == 0
        finally:
            fake.cleanup()

    asyncio.run(run())


# -- file error mapping --------------------------------------------------------


def test_download_relative_path_maps_to_invalid_path() -> None:
    backend, fake = make_fake_inis_sandbox_backend(_session_id())
    try:
        responses = backend.download_files(["relative/path.txt"])
        assert responses[0].error == "invalid_path"
        assert responses[0].content is None
    finally:
        fake.cleanup()


def test_download_missing_file_maps_to_file_not_found() -> None:
    backend, fake = make_fake_inis_sandbox_backend(_session_id())
    try:
        responses = backend.download_files([f"{fake.workspace_root}/does-not-exist.txt"])
        assert responses[0].error == "file_not_found"
    finally:
        fake.cleanup()


def test_upload_relative_path_maps_to_invalid_path() -> None:
    backend, fake = make_fake_inis_sandbox_backend(_session_id())
    try:
        responses = backend.upload_files([("relative/path.txt", b"x")])
        assert responses[0].error == "invalid_path"
    finally:
        fake.cleanup()


# -- two-conversation isolation -----------------------------------------------


def test_two_backends_do_not_share_state() -> None:
    """Two `InisSandboxBackend`s attached to two different session ids must
    never see each other's filesystem/exec state — the concrete failure
    mode a module-global session would produce. See PR description for the
    "deliberately share state, watch it fail, revert" run this guards
    against (a shared-fixture variant of this test observably fails when
    both backends are pointed at the same fake session id / same instance).
    """
    backend_a, fake_a = make_fake_inis_sandbox_backend(_session_id())
    backend_b, fake_b = make_fake_inis_sandbox_backend(_session_id())
    try:
        path_a = f"{fake_a.workspace_root}/nonce.txt"
        path_b = f"{fake_b.workspace_root}/nonce.txt"
        backend_a.upload_files([(path_a, b"nonce-A")])
        backend_b.upload_files([(path_b, b"nonce-B")])

        a_read = backend_a.download_files([path_a])[0]
        b_read = backend_b.download_files([path_b])[0]

        assert a_read.content == b"nonce-A"
        assert b_read.content == b"nonce-B"
        assert a_read.content != b_read.content
        assert backend_a.id != backend_b.id
    finally:
        fake_a.cleanup()
        fake_b.cleanup()


def test_two_backends_concurrent_execute_do_not_cross() -> None:
    """Async twin of the isolation test above, run genuinely concurrently
    via `asyncio.gather` (not just sequentially) — the shape real
    concurrent LangGraph threads would exercise."""

    async def run() -> None:
        backend_a, fake_a = make_fake_inis_sandbox_backend(_session_id())
        backend_b, fake_b = make_fake_inis_sandbox_backend(_session_id())
        try:
            who_a = f"{fake_a.workspace_root}/who.txt"
            who_b = f"{fake_b.workspace_root}/who.txt"
            result_a, result_b = await asyncio.gather(
                backend_a.aexecute(f"echo A > {who_a} && cat {who_a}"),
                backend_b.aexecute(f"echo B > {who_b} && cat {who_b}"),
            )
            assert result_a.output.strip() == "A"
            assert result_b.output.strip() == "B"
        finally:
            fake_a.cleanup()
            fake_b.cleanup()

    asyncio.run(run())


# -- Connections (create-time credential brokering) ---------------------------


def test_create_threads_connections_to_session_create(monkeypatch: pytest.MonkeyPatch) -> None:
    """`InisSandboxBackend.create(connections=...)` must reach the POST
    /v1/sessions payload the same way `egress_default`/`egress_allow` already
    do -- this is the create()-time wire contract, not a live connection."""
    captured: dict[str, object] = {}

    def fake_request(self: httpx.Client, method: str, url: object, **kwargs: object) -> httpx.Response:
        path = str(url).split("?", 1)[0]
        if method == "POST" and path == "https://fake.inis.test/v1/sessions":
            captured["json"] = kwargs.get("json")
            return httpx.Response(200, json={"session_id": "sess_conn_test", "state": "live"})
        if method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(httpx.Client, "request", fake_request)

    spec = ConnectionSpec(
        name="stripe",
        origin="https://api.stripe.com",
        secret="sk_test_123",
        methods=["GET", "POST"],
        paths=["/v1/"],
    )
    backend = InisSandboxBackend.create(
        api_key="test-token",
        base_url="https://fake.inis.test",
        egress_default=None,  # isolate this test to the connections payload
        connections=[spec],
    )
    try:
        assert captured["json"] == {
            "connections": [
                {
                    "name": "stripe",
                    "origin": "https://api.stripe.com",
                    "authentication": {"type": "bearer", "secret": "sk_test_123"},
                    "allow": {"methods": ["GET", "POST"], "paths": ["/v1/"]},
                }
            ]
        }
    finally:
        backend.destroy()


def test_create_accepts_plain_dict_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """`connections` also accepts already wire-shaped dicts, passed through
    as-is, matching the core SDK's own `Session(connections=...)` contract."""
    captured: dict[str, object] = {}

    def fake_request(self: httpx.Client, method: str, url: object, **kwargs: object) -> httpx.Response:
        path = str(url).split("?", 1)[0]
        if method == "POST" and path == "https://fake.inis.test/v1/sessions":
            captured["json"] = kwargs.get("json")
            return httpx.Response(200, json={"session_id": "sess_conn_dict_test", "state": "live"})
        if method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(httpx.Client, "request", fake_request)

    raw = {"name": "acme", "origin": "https://api.acme.test", "allow": {"methods": [], "paths": []}}
    backend = InisSandboxBackend.create(
        api_key="test-token",
        base_url="https://fake.inis.test",
        egress_default=None,  # isolate this test to the connections payload
        connections=[raw],
    )
    try:
        assert captured["json"] == {"connections": [raw]}
    finally:
        backend.destroy()
