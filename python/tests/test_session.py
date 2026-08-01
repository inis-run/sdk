"""Mocked-HTTP tests for inis.client.Session: request construction, response
mapping, and error handling for the methods added in the client-surface
redesign (get, pause, resume, fork, archive_retry, read_file, list_files,
write_file, destroy(reason=...)).
"""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest

from inis.client import (
    DestroyReason,
    ExecStreamEvent,
    InisError,
    OnPtyDetachPolicy,
    Session,
)
from tests.conftest import error_response, json_response


def make_session(**overrides) -> Session:
    kwargs = dict(base_url="https://api.inis.run", token="tok_abc")
    kwargs.update(overrides)
    session = Session(**kwargs)
    session.session_id = "sess_123"
    return session


class TestAttach:
    def test_attach_with_explicit_base_url_and_token(self):
        session = Session.attach("sess_123", base_url="https://api.inis.run", token="tok_abc")
        assert session.session_id == "sess_123"
        assert session._base_url == "https://api.inis.run"
        assert session._token == "tok_abc"

    def test_attach_falls_back_to_env_when_omitted(self, monkeypatch):
        monkeypatch.setenv("INIS_API_KEY", "tok_from_env")
        monkeypatch.setenv("INIS_BASE_URL", "https://eu1.inis.run")
        session = Session.attach("sess_123")
        assert session.session_id == "sess_123"
        assert session._base_url == "https://eu1.inis.run"
        assert session._token == "tok_from_env"

    def test_attach_raises_without_token_or_env(self, monkeypatch):
        monkeypatch.delenv("INIS_API_KEY", raising=False)
        with pytest.raises(InisError, match="INIS_API_KEY is required"):
            Session.attach("sess_123")


class TestGet:
    def test_request_and_mapping(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "session_id": "sess_123",
                    "state": "live",
                    "on_pty_detach": "pause",
                    "egress": {"mode": "deny", "allow": ["api.openai.com"]},
                },
            )
        )
        session = make_session()

        info = session.get()

        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call.method == "GET"
        assert call.path == "https://api.inis.run/v1/sessions/sess_123"
        assert call.headers["Authorization"] == "Bearer tok_abc"
        assert info.session_id == "sess_123"
        assert info.state == "live"
        assert info.on_pty_detach == "pause"
        assert info.egress.mode == "deny"
        assert info.egress.allow == ["api.openai.com"]

    def test_egress_default_field_backward_compat(self, fake_http):
        fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_123", "state": "live", "egress": {"default": "allow"}}
            )
        )
        info = make_session().get()
        assert info.egress.mode == "allow"

    def test_info_is_alias_for_get(self, fake_http):
        fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_123", "state": "live"}
            )
        )
        info = make_session().info()
        assert info.session_id == "sess_123"

    def test_env_mapped_and_secrets_redacted_to_names_only(self, fake_http):
        # Models the server's actual redaction contract: env (non-sensitive)
        # comes back in full, secrets come back as names only — this response
        # fixture never even includes a secret VALUE, because the real API
        # never sends one.
        fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "session_id": "sess_123",
                    "state": "live",
                    "env": {"MODE": "prod"},
                    "secret_names": ["API_KEY", "DB_PASSWORD"],
                },
            )
        )
        info = make_session().get()
        assert info.env == {"MODE": "prod"}
        assert info.secret_names == ["API_KEY", "DB_PASSWORD"]


class TestExec:
    """`truncated` was missing from api/openapi.yaml entirely and so never
    reached ExecResult -- these
    pin the field through Session.exec()'s response mapping now that it's
    fixed, both when the guest sets it and the (much more common) unset/
    absent-key case a complete response actually sends.
    """

    def test_maps_truncated_when_set(self, fake_http):
        fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "stdout": "partial output",
                    "stderr": "",
                    "exit_code": 0,
                    "duration_ms": 12,
                    "timed_out": False,
                    "truncated": True,
                },
            )
        )
        result = make_session().exec("cat bigfile")
        assert result.truncated is True

    def test_truncated_defaults_false_when_absent(self, fake_http):
        fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "stdout": "ok",
                    "stderr": "",
                    "exit_code": 0,
                    "duration_ms": 5,
                    "timed_out": False,
                },
            )
        )
        result = make_session().exec("echo ok")
        assert result.truncated is False

    def test_maps_stdout_encoding_when_api_flags_non_utf8(self, fake_http):
        # stdout_encoding is set by the API when a stream's output was
        # not valid UTF-8 and had to be base64-encoded rather than risk
        # json.Marshal silently corrupting it. Pins the field through
        # Session.exec()'s response mapping.
        fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "stdout": "QkVGT1JFLf8tQUZURVI=",
                    "stderr": "",
                    "exit_code": 0,
                    "duration_ms": 5,
                    "timed_out": False,
                    "stdout_encoding": "base64",
                },
            )
        )
        result = make_session().exec("cat binfile")
        assert result.stdout_encoding == "base64"
        assert result.stderr_encoding is None
        assert result.stdout == "QkVGT1JFLf8tQUZURVI="

    def test_stdout_encoding_is_none_for_plain_text_response(self, fake_http):
        fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "stdout": "ok",
                    "stderr": "",
                    "exit_code": 0,
                    "duration_ms": 5,
                    "timed_out": False,
                },
            )
        )
        result = make_session().exec("echo ok")
        assert result.stdout_encoding is None
        assert result.stderr_encoding is None


class TestPauseResume:
    def test_pause(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_123", "state": "paused"}
            )
        )
        info = make_session().pause()
        assert transport.calls[0].method == "POST"
        assert transport.calls[0].path == "https://api.inis.run/v1/sessions/sess_123/pause"
        assert info.state == "paused"

    def test_resume(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_123", "state": "live"}
            )
        )
        info = make_session().resume()
        assert transport.calls[0].method == "POST"
        assert transport.calls[0].path == "https://api.inis.run/v1/sessions/sess_123/resume"
        assert info.state == "live"


class TestFork:
    def test_fork_sends_count_and_maps_children(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"parent_session_id": "sess_123", "children": ["sess_a", "sess_b"]}
            )
        )
        result = make_session().fork(count=2)
        assert transport.calls[0].json_body == {"count": 2}
        assert result.parent_session_id == "sess_123"
        assert result.children == ["sess_a", "sess_b"]

    def test_fork_default_count_is_one(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"parent_session_id": "sess_123", "children": ["sess_a"]}
            )
        )
        make_session().fork()
        assert transport.calls[0].json_body == {"count": 1}


class TestArchiveRetry:
    def test_maps_archive_status_and_posts_to_archive_retry_path(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200,
                {"status": "complete", "cold_uri": "s3://bucket/x", "uploaded_at": "2026-07-14T00:00:00Z"},
            )
        )
        status = make_session().archive_retry()
        assert transport.calls[0].method == "POST"
        # Must match the server route registration
        # (POST /v1/sessions/{id}/archive/retry) exactly — a stray hyphen
        # here 404s against the real API.
        assert transport.calls[0].path == "https://api.inis.run/v1/sessions/sess_123/archive/retry"
        assert status.status == "complete"
        assert status.cold_uri == "s3://bucket/x"
        assert status.uploaded_at == "2026-07-14T00:00:00Z"


class TestFiles:
    def test_read_file(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(200, {"content": "hello"})
        )
        content = make_session().read_file("/workspace/x.txt")
        assert transport.calls[0].method == "GET"
        assert transport.calls[0].params == {"path": "/workspace/x.txt", "op": "read"}
        assert content == "hello"

    def test_list_files_default_path(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(200, {"entries": ["a", "b"]})
        )
        entries = make_session().list_files()
        assert transport.calls[0].params == {"path": "/workspace", "op": "list"}
        assert entries == ["a", "b"]

    def test_write_file(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(200, {}))
        make_session().write_file("/workspace/x.txt", "content", encoding="base64")
        call = transport.calls[0]
        assert call.method == "PUT"
        assert call.json_body == {
            "path": "/workspace/x.txt",
            "content": "content",
            "encoding": "base64",
        }

    def test_read_file_base64_roundtrip(self, fake_http):
        import base64

        raw = b"\x00\x01\xff\xfehi"
        encoded = base64.b64encode(raw).decode("ascii")
        transport = fake_http(
            lambda method, path, params, body: json_response(200, {"content": encoded})
        )
        content = make_session().read_file("/workspace/bin.dat", encoding="base64")
        assert transport.calls[0].params == {
            "path": "/workspace/bin.dat",
            "op": "read",
            "encoding": "base64",
        }
        assert content == raw

    def test_write_file_bytes_auto_base64_encodes(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(200, {}))
        make_session().write_file("/workspace/bin.dat", b"\x00\x01\xff")
        call = transport.calls[0]
        assert call.json_body["encoding"] == "base64"
        import base64

        assert base64.b64decode(call.json_body["content"]) == b"\x00\x01\xff"

    def test_write_files_batch(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "results": [
                        {"path": "/workspace/a.txt", "ok": True},
                        {"path": "/workspace/b.txt", "ok": False, "error": "boom"},
                    ]
                },
            )
        )
        results = make_session().write_files(
            [
                {"path": "/workspace/a.txt", "content": "aaa"},
                {"path": "/workspace/b.txt", "content": "bbb"},
            ]
        )
        call = transport.calls[0]
        assert call.method == "PUT"
        assert call.path.endswith("/files/batch")
        assert len(results) == 2
        assert results[0]["ok"] is True
        assert results[1]["ok"] is False

    def test_find_files(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"paths": ["/workspace/a.go"], "truncated": False}
            )
        )
        result = make_session().find_files("*.go", max_results=10)
        assert transport.calls[0].params == {
            "path": "/workspace",
            "op": "find",
            "pattern": "*.go",
            "max_results": 10,
        }
        assert result == {"paths": ["/workspace/a.go"], "truncated": False}

    def test_grep_files(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "matches": [{"path": "/workspace/a.go", "line_number": 3, "line": "needle"}],
                    "files_searched": 1,
                    "truncated": False,
                },
            )
        )
        result = make_session().grep_files(
            "needle",
            file_pattern="*.go",
            case_sensitive=True,
            context_lines=2,
            max_results=5,
            exclude_dirs=["node_modules", ".git"],
        )
        assert transport.calls[0].params == {
            "path": "/workspace",
            "op": "grep",
            "pattern": "needle",
            "file_pattern": "*.go",
            "case_sensitive": "true",
            "context_lines": 2,
            "max_results": 5,
            "exclude_dirs": "node_modules,.git",
        }
        assert result["files_searched"] == 1
        assert result["matches"][0]["line"] == "needle"

    def test_files_namespace_delegates(self, fake_http):
        """FilesAPI (session.files.*) is a compat wrapper around the direct methods."""
        transport = fake_http(
            lambda method, path, params, body: json_response(200, {"content": "hi", "entries": []})
        )
        session = make_session()
        session.files.read("/a")
        assert transport.calls[-1].params == {"path": "/a", "op": "read"}
        session.files.list("/b")
        assert transport.calls[-1].params == {"path": "/b", "op": "list"}

    def test_mkdir(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(200, {"path": "/workspace/a/b"}))
        make_session().mkdir("/workspace/a/b")
        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path.endswith("/files/mkdir")
        assert call.json_body == {"path": "/workspace/a/b"}

    def test_remove_default_non_recursive(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(200, {}))
        make_session().remove("/workspace/a.txt")
        call = transport.calls[0]
        assert call.method == "DELETE"
        assert call.params == {"path": "/workspace/a.txt"}

    def test_remove_recursive(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(200, {}))
        make_session().remove("/workspace/d", recursive=True)
        call = transport.calls[0]
        assert call.params == {"path": "/workspace/d", "recursive": "true"}

    def test_remove_nonexistent_path_raises(self, fake_http):
        fake_http(lambda method, path, params, body: error_response(404, "no such file or directory"))
        with pytest.raises(InisError) as excinfo:
            make_session().remove("/workspace/missing.txt")
        assert "404" in str(excinfo.value)

    def test_rename(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"path": "/workspace/a.txt", "dest_path": "/workspace/b.txt"}
            )
        )
        make_session().rename("/workspace/a.txt", "/workspace/b.txt")
        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path.endswith("/files/rename")
        assert call.json_body == {"path": "/workspace/a.txt", "dest_path": "/workspace/b.txt"}


class TestFileStreaming:
    """upload_file/download_file: streamed binary transfer not subject
    to write_file/read_file's size caps. upload_file rides
    httpx.Client.request (fake_http can see it); download_file uses
    httpx.Client.stream() like exec_stream/stream_process_logs, so it needs
    the real MockTransport seam (see sse_session's doc above) rather than
    fake_http.
    """

    def test_upload_file_streams_from_disk(self, fake_http, tmp_path):
        local = tmp_path / "payload.bin"
        raw = bytes(range(256)) * 40  # 10240 bytes, well above a tiny buffer
        local.write_bytes(raw)

        captured_content = {}

        def handler(method, path, params, body):
            return json_response(
                200, {"path": "/workspace/payload.bin", "bytes": len(raw), "sha256": "deadbeef"}
            )

        transport = fake_http(handler)
        result = make_session().upload_file(str(local), "/workspace/payload.bin")

        call = transport.calls[0]
        assert call.method == "PUT"
        assert call.path.endswith("/files/stream")
        assert call.params == {"path": "/workspace/payload.bin"}
        assert result["bytes"] == len(raw)
        assert result["sha256"] == "deadbeef"
        _ = captured_content

    def test_upload_file_error_raises(self, fake_http, tmp_path):
        local = tmp_path / "ro.bin"
        local.write_bytes(b"data")
        fake_http(lambda method, path, params, body: error_response(400, "permission denied"))
        with pytest.raises(InisError) as excinfo:
            make_session().upload_file(str(local), "/workspace/ro/ro.bin")
        assert "400" in str(excinfo.value)

    def test_download_file_writes_to_disk_and_hashes(self, tmp_path):
        raw = bytes(range(256)) * 40

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=raw, headers={"content-type": "application/octet-stream"})

        session = make_session()
        session._client = httpx.Client(transport=httpx.MockTransport(handler))

        dest = tmp_path / "downloaded.bin"
        result = session.download_file("/workspace/payload.bin", str(dest))

        assert dest.read_bytes() == raw
        assert result["bytes"] == len(raw)
        expected_sha256 = hashlib.sha256(raw).hexdigest()
        assert result["sha256"] == expected_sha256

    def test_download_file_nonexistent_remote_raises(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "no such file or directory"})

        session = make_session()
        session._client = httpx.Client(transport=httpx.MockTransport(handler))

        dest = tmp_path / "downloaded.bin"
        with pytest.raises(InisError) as excinfo:
            session.download_file("/workspace/missing.bin", str(dest))
        assert "404" in str(excinfo.value)
        assert not dest.exists()


class TestDestroy:
    def test_destroy_without_reason(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(200, {}))
        session = make_session()
        session.destroy()
        call = transport.calls[0]
        assert call.method == "DELETE"
        assert call.params is None
        assert session.session_id is None

    def test_destroy_with_string_reason(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(200, {}))
        make_session().destroy(reason="shell_exit")
        assert transport.calls[0].params == {"reason": "shell_exit"}

    def test_destroy_with_enum_reason(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(200, {}))
        make_session().destroy(reason=DestroyReason.CLIENT_DESTROY)
        assert transport.calls[0].params == {"reason": "client_destroy"}

    def test_destroy_noop_without_session_id(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(200, {}))
        session = make_session()
        session.session_id = None
        session.destroy()
        assert transport.calls == []


class TestOnPtyDetachEnum:
    def test_enum_value_sent_on_create(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        session = Session(
            base_url="https://api.inis.run",
            token="tok",
            on_pty_detach=OnPtyDetachPolicy.DESTROY,
        )
        session.__enter__()
        assert transport.calls[0].json_body["on_pty_detach"] == "destroy"

    def test_plain_string_also_accepted(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        session = Session(base_url="https://api.inis.run", token="tok", on_pty_detach="pause")
        session.__enter__()
        assert transport.calls[0].json_body["on_pty_detach"] == "pause"


class TestEnvAndSecrets:
    def test_env_and_secrets_sent_on_create(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        session = Session(
            base_url="https://api.inis.run",
            token="tok",
            env={"FLAG": "1"},
            secrets={"API_KEY": "sk-canary-value"},
        )
        session.__enter__()
        body = transport.calls[0].json_body
        assert body["env"] == {"FLAG": "1"}
        assert body["secrets"] == {"API_KEY": "sk-canary-value"}

    def test_omitted_when_unset(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        session = Session(base_url="https://api.inis.run", token="tok")
        session.__enter__()
        body = transport.calls[0].json_body
        assert "env" not in body
        assert "secrets" not in body


class TestErrorMapping:
    def test_raises_inis_error_with_status_and_detail(self, fake_http):
        fake_http(lambda method, path, params, body: error_response(404, "session not found"))
        session = make_session()
        with pytest.raises(InisError) as excinfo:
            session.get()
        assert "404" in str(excinfo.value)
        assert "session not found" in str(excinfo.value)

    def test_raises_inis_error_on_5xx(self, fake_http):
        fake_http(lambda method, path, params, body: error_response(500, "internal error"))
        with pytest.raises(InisError):
            make_session().pause()

    def test_non_json_error_body_falls_back_to_text(self, fake_http):
        import httpx

        fake_http(lambda method, path, params, body: httpx.Response(502, text="bad gateway"))
        with pytest.raises(InisError) as excinfo:
            make_session().resume()
        assert "bad gateway" in str(excinfo.value)


class TestCheckpointFlow:
    def test_checkpoint_create_list_restore(self, fake_http):
        responses = {
            ("POST", "https://api.inis.run/v1/sessions/sess_123/checkpoints"): json_response(
                200, {"checkpoint_id": "cp_1", "session_id": "sess_123", "size_bytes": 42}
            ),
            ("GET", "https://api.inis.run/v1/sessions/sess_123/checkpoints"): json_response(
                200, {"checkpoints": [{"checkpoint_id": "cp_1"}]}
            ),
            ("POST", "https://api.inis.run/v1/sessions/sess_123/restore"): json_response(
                200, {"session_id": "sess_123", "state": "live"}
            ),
        }

        def handler(method, path, params, body):
            return responses[(method, path)]

        fake_http(handler)
        session = make_session()

        cp = session.checkpoint(name="golden", labels={"env": "ci"})
        assert cp.checkpoint_id == "cp_1"
        assert cp.size_bytes == 42

        cps = session.checkpoints()
        assert len(cps) == 1 and cps[0].checkpoint_id == "cp_1"

        info = session.restore("cp_1")
        assert info.state == "live"


class TestProcesses:
    def test_start_process_request_and_mapping(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "name": "server",
                    "state": "running",
                    "pid": 4242,
                    "command": ["python3", "-m", "http.server", "8000"],
                    "keep_alive": True,
                    "started_at": "2026-07-17T00:00:00Z",
                },
            )
        )
        session = make_session()

        info = session.start_process(
            "server", ["python3", "-m", "http.server", "8000"], keep_alive=True
        )

        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path == "https://api.inis.run/v1/sessions/sess_123/processes"
        assert call.json_body == {
            "name": "server",
            "command": ["python3", "-m", "http.server", "8000"],
            "keep_alive": True,
        }
        assert info.name == "server"
        assert info.state == "running"
        assert info.pid == 4242
        assert info.keep_alive is True

    def test_start_process_string_command_wrapped_in_shell(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"name": "greet", "state": "running"}
            )
        )
        session = make_session()

        session.start_process("greet", "echo hi")

        assert transport.calls[0].json_body["command"] == ["bash", "-lc", "echo hi"]
        assert transport.calls[0].json_body["keep_alive"] is False

    def test_list_processes(self, fake_http):
        fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "processes": [
                        {"name": "server", "state": "running"},
                        {"name": "done", "state": "exited", "exit_code": 0},
                    ]
                },
            )
        )
        session = make_session()

        procs = session.list_processes()
        assert [p.name for p in procs] == ["server", "done"]
        assert procs[1].state == "exited"
        assert procs[1].exit_code == 0

    def test_get_process(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"name": "server", "state": "running", "pid": 99}
            )
        )
        session = make_session()

        info = session.get_process("server")

        assert transport.calls[0].method == "GET"
        assert transport.calls[0].path == "https://api.inis.run/v1/sessions/sess_123/processes/server"
        assert info.pid == 99

    def test_kill_process(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"name": "server", "state": "exited", "exit_code": 143}
            )
        )
        session = make_session()

        info = session.kill_process("server")

        assert transport.calls[0].method == "DELETE"
        assert transport.calls[0].path == "https://api.inis.run/v1/sessions/sess_123/processes/server"
        assert info.state == "exited"
        assert info.exit_code == 143

    def test_get_process_logs(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"name": "server", "stdout": "hello\n", "stderr": ""}
            )
        )
        session = make_session()

        logs = session.get_process_logs("server")

        assert transport.calls[0].method == "GET"
        assert (
            transport.calls[0].path
            == "https://api.inis.run/v1/sessions/sess_123/processes/server/logs"
        )
        assert logs.stdout == "hello\n"
        assert logs.stderr == ""

    def test_process_not_found_raises(self, fake_http):
        fake_http(lambda method, path, params, body: error_response(404, 'process "x" not found'))
        session = make_session()
        with pytest.raises(InisError) as excinfo:
            session.get_process("x")
        assert "404" in str(excinfo.value)


def sse_session(sse_text: str, *, status_code: int = 200) -> Session:
    """Build a Session whose HTTP client's TRANSPORT (not just its
    request() method — the fake_http fixture only patches that, which
    exec_stream/stream_process_logs bypass entirely via httpx.Client.stream())
    is a real httpx.MockTransport returning sse_text as the body. This is
    what makes SSE streaming methods testable at all: httpx.MockTransport is
    the officially supported low-level seam that both .request() and
    .stream() funnel through.
    """
    session = make_session()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=sse_text, headers={"content-type": "text/event-stream"})

    session._client = httpx.Client(transport=httpx.MockTransport(handler))
    return session


class TestExecStreamSSE:
    """SSE streaming tests for exec_stream() — mirrors the TypeScript SDK's
    equivalent client.test.ts coverage (added because fake_http can't see
    .stream() calls, so exec_stream had no live-HTTP unit test at all before
    this coverage was added; the streamProcessLogs() sibling method has the
    same historical gap, not newly introduced here).
    """

    def test_decodes_stdout_stderr_and_exit(self):
        def b64(s: str) -> str:
            return base64.b64encode(s.encode("utf-8")).decode("ascii")

        out1 = b64("out1\n")
        err1 = b64("err1\n")
        sse = (
            f"event: stdout\ndata: {out1}\n\n"
            f"event: stderr\ndata: {err1}\n\n"
            'event: exit\ndata: {"exit_code":0,"timed_out":false,"duration_ms":42}\n\n'
        )
        session = sse_session(sse)

        events = list(session.exec_stream(["echo", "hi"]))

        assert [(e.stream, e.data) for e in events[:2]] == [("stdout", "out1\n"), ("stderr", "err1\n")]
        exit_event = events[2]
        assert exit_event.stream == "exit"
        assert exit_event.exit_code == 0
        assert exit_event.timed_out is False
        assert exit_event.duration_ms == 42

    def test_stops_at_error_frame(self):
        sse = "event: error\ndata: guest agent unreachable\n\n"
        session = sse_session(sse)

        events = list(session.exec_stream("echo hi"))

        assert events == [ExecStreamEvent(stream="error", data="guest agent unreachable")]

    def test_raises_inis_error_on_non_ok_response(self):
        session = sse_session("", status_code=503)
        with pytest.raises(InisError) as excinfo:
            list(session.exec_stream("echo hi"))
        assert "503" in str(excinfo.value)

    def test_reassembles_multibyte_char_split_across_chunk_boundary(self):
        # "🎉" is U+1F389, a 4-byte UTF-8
        # sequence (F0 9F 8E 89, encoded from "hello 🎉 world"). Split it so
        # two bytes of the emoji land in each of two separate SSE frames —
        # mirroring io.Copy's 32KB read boundary landing mid-character on the
        # guest side — and confirm the incremental decoder reassembles the
        # character instead of replacing each half with U+FFFD.
        raw = "hello \U0001F389 world".encode("utf-8")
        split_at = 6 + 2  # "hello " (6 bytes) + first 2 bytes of the emoji
        chunk1 = base64.b64encode(raw[:split_at]).decode("ascii")
        chunk2 = base64.b64encode(raw[split_at:]).decode("ascii")
        sse = (
            f"event: stdout\ndata: {chunk1}\n\n"
            f"event: stdout\ndata: {chunk2}\n\n"
            'event: exit\ndata: {"exit_code":0,"timed_out":false,"duration_ms":1}\n\n'
        )
        session = sse_session(sse)

        events = list(session.exec_stream("echo hi"))

        combined = "".join(e.data for e in events if e.stream == "stdout")
        assert combined == "hello \U0001F389 world"
        assert "�" not in combined


class TestStreamProcessLogsSSE:
    """Same coverage as TestExecStreamSSE for the sibling streaming method
    (identical chunk-boundary bug, fixed the same way — see
    _IncrementalUtf8Decoder's doc)."""

    def test_reassembles_multibyte_char_split_across_chunk_boundary(self):
        raw = "hello \U0001F389 world".encode("utf-8")
        split_at = 6 + 2
        chunk1 = base64.b64encode(raw[:split_at]).decode("ascii")
        chunk2 = base64.b64encode(raw[split_at:]).decode("ascii")
        sse = f"event: stdout\ndata: {chunk1}\n\n" f"event: stdout\ndata: {chunk2}\n\n" "event: eof\ndata: \n\n"
        session = sse_session(sse)

        events = list(session.stream_process_logs("server"))

        combined = "".join(e.data for e in events if e.event == "stdout")
        assert combined == "hello \U0001F389 world"
        assert "�" not in combined
