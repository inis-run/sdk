"""Mocked-HTTP tests for inis.async_client.AsyncClient and its sub-APIs — the
async twin of tests/test_client.py. Same coverage (sessions.get/attach/list/
batch_exec/files, checkpoints, artifacts, templates, create-time options,
error mapping), exercised with await against httpx.AsyncClient.
"""

from __future__ import annotations

import pytest

from inis.async_client import AsyncClient, AsyncSession
from inis.client import InisError
from tests.conftest import error_response, json_response


def make_client() -> AsyncClient:
    return AsyncClient(token="tok_abc", base_url="https://api.inis.run")


class TestSessionsAPIGet:
    async def test_get_by_id_without_attaching(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_1", "state": "paused"}
            )
        )
        info = await make_client().sessions.get("sess_1")
        assert transport.calls[0].method == "GET"
        assert transport.calls[0].path == "https://api.inis.run/v1/sessions/sess_1"
        assert info.state == "paused"


class TestSessionsAPIAttach:
    async def test_attach_threads_clients_base_url_token_and_timeout(self):
        client = AsyncClient(token="tok_abc", base_url="https://api.inis.run", timeout=42.0)
        session = await client.sessions.attach("sess_1")
        assert isinstance(session, AsyncSession)
        assert session.session_id == "sess_1"
        assert session._base_url == "https://api.inis.run"
        assert session._token == "tok_abc"
        assert session._timeout == 42.0

    async def test_attach_does_not_issue_a_request(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(200, {}))
        await make_client().sessions.attach("sess_1")
        assert transport.calls == []


class TestSessionsAPIList:
    async def test_list_passes_filters_and_cursor(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {
                    "sessions": [{"session_id": "sess_1", "state": "live"}],
                    "next_cursor": "cur_2",
                },
            )
        )
        sessions, next_cursor = await make_client().sessions.list(
            state="live", limit=10, cursor="cur_1"
        )
        call = transport.calls[0]
        assert call.params == {"state": "live", "limit": 10, "cursor": "cur_1"}
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess_1"
        assert next_cursor == "cur_2"


class TestSessionsAPIBatchExec:
    async def test_batch_exec_wraps_string_command_and_maps_results(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {
                    "results": [
                        {"session_id": "sess_1", "stdout": "ok", "exit_code": 0},
                        {"session_id": "sess_2", "error": "timed out"},
                    ]
                },
            )
        )
        results = await make_client().sessions.batch_exec(["sess_1", "sess_2"], "echo hi")

        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path == "https://api.inis.run/v1/sessions/batch/exec"
        assert call.json_body["command"] == ["bash", "-lc", "echo hi"]
        assert call.json_body["session_ids"] == ["sess_1", "sess_2"]

        assert results[0].session_id == "sess_1"
        assert results[0].stdout == "ok"
        assert results[1].session_id == "sess_2"
        assert results[1].error == "timed out"

    async def test_batch_exec_accepts_argv_list_unmodified(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(200, {"results": []})
        )
        await make_client().sessions.batch_exec(["sess_1"], ["python3", "-c", "print(1)"])
        assert transport.calls[0].json_body["command"] == ["python3", "-c", "print(1)"]


class TestSessionsAPIFiles:
    async def test_read_write_list_by_session_id(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"content": "data", "entries": ["a"]}
            )
        )
        sessions = make_client().sessions
        assert await sessions.read_file("sess_1", "/a") == "data"
        assert transport.calls[-1].path == "https://api.inis.run/v1/sessions/sess_1/files"
        assert await sessions.list_files("sess_1", "/") == ["a"]
        await sessions.write_file("sess_1", "/a", "content")
        assert transport.calls[-1].method == "PUT"
        assert transport.calls[-1].json_body == {
            "path": "/a",
            "content": "content",
            "encoding": "text",
        }


class TestCheckpointsAPI:
    async def test_get(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"checkpoint_id": "cp_1", "size_bytes": 10}
            )
        )
        cp = await make_client().checkpoints.get("cp_1")
        assert transport.calls[0].path == "https://api.inis.run/v1/checkpoints/cp_1"
        assert cp.checkpoint_id == "cp_1"

    async def test_delete(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(204, {}))
        await make_client().checkpoints.delete("cp_1")
        assert transport.calls[0].method == "DELETE"
        assert transport.calls[0].path == "https://api.inis.run/v1/checkpoints/cp_1"

    async def test_create_session_branches_from_checkpoint(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(200, {"session_id": "sess_branch"})
        )
        session = await make_client().checkpoints.create_session(
            "cp_1", name="branch-a", labels={"k": "v"}, max_lifetime_ms=1000, idle_timeout_ms=500
        )
        call = transport.calls[0]
        assert call.path == "https://api.inis.run/v1/checkpoints/cp_1/sessions"
        assert call.json_body == {
            "name": "branch-a",
            "labels": {"k": "v"},
            "max_lifetime_ms": 1000,
            "idle_timeout_ms": 500,
        }
        assert isinstance(session, AsyncSession)
        assert session.session_id == "sess_branch"


class TestArtifactsAPI:
    async def test_get_maps_files(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {
                    "id": "art_1",
                    "status": "ready",
                    "files": [{"path": "out.txt", "url": "https://x", "size_bytes": 5}],
                },
            )
        )
        artifact = await make_client().artifacts.get("art_1")
        assert artifact.status == "ready"
        assert artifact.files[0].path == "out.txt"

    async def test_extend(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(200, {"id": "art_1", "status": "ready"})
        )
        await make_client().artifacts.extend("art_1", 7)
        assert transport.calls[0].path == "https://api.inis.run/v1/artifacts/art_1/extend"
        assert transport.calls[0].json_body == {"ttl_days": 7}

    async def test_delete(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(204, {}))
        await make_client().artifacts.delete("art_1")
        assert transport.calls[0].method == "DELETE"


class TestTemplatesAPI:
    async def test_list(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"templates": [{"name": "python-3.12", "kind": "official"}]}
            )
        )
        templates = await make_client().templates.list()
        assert len(templates) == 1
        assert templates[0].name == "python-3.12"
        assert templates[0].kind == "official"

    async def test_delete(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(204, {}))
        await make_client().templates.delete("my-template")
        assert transport.calls[0].path == "https://api.inis.run/v1/templates/my-template"
        assert transport.calls[0].method == "DELETE"


class TestRegistriesAPI:
    async def test_add(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                201,
                {"id": "cred_1", "name": "my-ghcr", "registry_host": "ghcr.io", "username": "user"},
            )
        )
        cred = await make_client().registries.add(
            "my-ghcr", registry_host="ghcr.io", username="user", secret="super-secret-pat"
        )
        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path == "https://api.inis.run/v1/registry-credentials"
        assert cred.name == "my-ghcr"
        assert not hasattr(cred, "secret")

    async def test_list(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {"credentials": [{"id": "cred_1", "name": "my-ghcr", "registry_host": "ghcr.io", "username": "user"}]},
            )
        )
        creds = await make_client().registries.list()
        assert len(creds) == 1
        assert creds[0].name == "my-ghcr"

    async def test_delete(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(204, {}))
        await make_client().registries.delete("my-ghcr")
        assert transport.calls[0].path == "https://api.inis.run/v1/registry-credentials/my-ghcr"
        assert transport.calls[0].method == "DELETE"


class TestConnectorsAPI:
    async def test_add(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                201,
                {"id": "conn_1", "name": "stripe", "target_base_url": "https://api.stripe.com", "auth_shape": "bearer"},
            )
        )
        conn = await make_client().connectors.add(
            "stripe", target_base_url="https://api.stripe.com", secret="sk_live_super_secret"
        )
        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path == "https://api.inis.run/v1/connectors"
        assert conn.name == "stripe"
        assert not hasattr(conn, "secret")

    async def test_list(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {"connectors": [{"id": "conn_1", "name": "stripe", "target_base_url": "https://api.stripe.com", "auth_shape": "bearer"}]},
            )
        )
        conns = await make_client().connectors.list()
        assert len(conns) == 1
        assert conns[0].name == "stripe"

    async def test_delete(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(204, {}))
        await make_client().connectors.delete("stripe")
        assert transport.calls[0].path == "https://api.inis.run/v1/connectors/stripe"
        assert transport.calls[0].method == "DELETE"


class TestDomainsAPI:
    async def test_add(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                201,
                {
                    "id": "dom_1",
                    "domain": "app.customer.com",
                    "verified": False,
                    "verify_txt_name": "_inis-verify.app.customer.com",
                    "verify_txt_value": "tok_abc123",
                },
            )
        )
        d = await make_client().domains.add("app.customer.com")
        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path == "https://api.inis.run/v1/org/domains"
        assert call.json_body == {"domain": "app.customer.com"}
        assert d.verified is False

    async def test_list(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {
                    "domains": [
                        {
                            "id": "dom_1",
                            "domain": "app.customer.com",
                            "verified": True,
                            "verify_txt_name": "_inis-verify.app.customer.com",
                            "verify_txt_value": "tok_abc123",
                        }
                    ]
                },
            )
        )
        domains = await make_client().domains.list()
        assert len(domains) == 1
        assert domains[0].verified is True

    async def test_delete(self, fake_http_async):
        transport = fake_http_async(lambda method, path, params, body: json_response(204, {}))
        await make_client().domains.delete("dom_1")
        assert transport.calls[0].path == "https://api.inis.run/v1/org/domains/dom_1"
        assert transport.calls[0].method == "DELETE"

    async def test_verify(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {
                    "id": "dom_1",
                    "domain": "app.customer.com",
                    "verified": True,
                    "verify_txt_name": "_inis-verify.app.customer.com",
                    "verify_txt_value": "tok_abc123",
                },
            )
        )
        d = await make_client().domains.verify("dom_1")
        assert transport.calls[0].path == "https://api.inis.run/v1/org/domains/dom_1/verify"
        assert d.verified is True

    async def test_verify_failure_raises_inis_error(self, fake_http_async):
        fake_http_async(lambda method, path, params, body: error_response(409, "verification failed"))
        with pytest.raises(InisError):
            await make_client().domains.verify("dom_1")

    async def test_route(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {
                    "id": "dom_1",
                    "domain": "app.customer.com",
                    "verified": True,
                    "verify_txt_name": "_inis-verify.app.customer.com",
                    "verify_txt_value": "tok_abc123",
                    "session_id": "ses_1",
                    "port": 8000,
                },
            )
        )
        d = await make_client().domains.route("dom_1", "ses_1", 8000)
        call = transport.calls[0]
        assert call.method == "PUT"
        assert call.path == "https://api.inis.run/v1/org/domains/dom_1/route"
        assert call.json_body == {"session_id": "ses_1", "port": 8000}
        assert d.session_id == "ses_1"

    async def test_unroute(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200,
                {
                    "id": "dom_1",
                    "domain": "app.customer.com",
                    "verified": True,
                    "verify_txt_name": "_inis-verify.app.customer.com",
                    "verify_txt_value": "tok_abc123",
                },
            )
        )
        d = await make_client().domains.unroute("dom_1")
        assert transport.calls[0].method == "DELETE"
        assert transport.calls[0].path == "https://api.inis.run/v1/org/domains/dom_1/route"
        assert d.session_id is None


class TestSessionCreateOptions:
    async def test_on_pty_detach_and_paused_ttl_forwarded(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        session = await make_client().sessions.create(
            on_pty_detach="destroy", paused_ttl_ms=60_000, no_sudo=True, template="python-3.12"
        )
        call = transport.calls[0]
        assert call.json_body["on_pty_detach"] == "destroy"
        assert call.json_body["paused_ttl_ms"] == 60_000
        assert call.json_body["no_sudo"] is True
        assert call.json_body["template"] == "python-3.12"
        assert session.session_id == "sess_new"

    async def test_egress_mode_sent_as_mode_field(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        await make_client().sessions.create(egress_default="deny", egress_allow=["*.openai.com"])
        assert transport.calls[0].json_body["egress"] == {
            "mode": "deny",
            "allow": ["*.openai.com"],
        }


class TestExecuteOneShotAndCapacity:
    async def test_execute(self, fake_http_async):
        transport = fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"stdout": "42\n", "exit_code": 0, "install_ms": 100, "phase": "run"}
            )
        )
        result = await make_client().execute(language="python", code="print(42)")
        assert transport.calls[0].path == "https://api.inis.run/v1/sessions/exec"
        assert result.stdout == "42\n"
        assert result.install_ms == 100
        assert result.phase == "run"

    async def test_capacity(self, fake_http_async):
        fake_http_async(
            lambda method, path, params, body: json_response(
                200, {"capacity": {"running": 3, "warm": 1, "limits": {"running": 10, "warm": 5}}}
            )
        )
        cap = await make_client().capacity()
        assert cap.running == 3
        assert cap.limits.running == 10


class TestClientErrorMapping:
    async def test_get_raises_inis_error(self, fake_http_async):
        fake_http_async(lambda method, path, params, body: error_response(401, "unauthorized"))
        with pytest.raises(InisError) as excinfo:
            await make_client().sessions.get("sess_1")
        assert "401" in str(excinfo.value)
        assert "unauthorized" in str(excinfo.value)

    async def test_delete_raises_inis_error(self, fake_http_async):
        fake_http_async(lambda method, path, params, body: error_response(403, "forbidden"))
        with pytest.raises(InisError):
            await make_client().templates.delete("x")
