"""Mocked-HTTP tests for inis.client.Client and its sub-APIs: the new
_SessionsAPI methods (get, batch_exec moved off Session), _CheckpointsAPI,
_ArtifactsAPI, and _TemplatesAPI added in the client-surface redesign.
"""

from __future__ import annotations

import pytest

from inis.client import Client, InisError, Session
from tests.conftest import error_response, json_response


def make_client() -> Client:
    return Client(token="tok_abc", base_url="https://api.inis.run")


class TestSessionsAPIGet:
    def test_get_by_id_without_attaching(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_1", "state": "paused"}
            )
        )
        info = make_client().sessions.get("sess_1")
        assert transport.calls[0].method == "GET"
        assert transport.calls[0].path == "https://api.inis.run/v1/sessions/sess_1"
        assert info.state == "paused"


class TestSessionsAPIAttach:
    def test_attach_threads_clients_base_url_token_and_timeout(self):
        client = Client(token="tok_abc", base_url="https://api.inis.run", timeout=42.0)
        session = client.sessions.attach("sess_1")
        assert isinstance(session, Session)
        assert session.session_id == "sess_1"
        assert session._base_url == "https://api.inis.run"
        assert session._token == "tok_abc"
        assert session._timeout == 42.0

    def test_attach_does_not_issue_a_request(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(200, {}))
        make_client().sessions.attach("sess_1")
        assert transport.calls == []


class TestSessionsAPIList:
    def test_list_passes_filters_and_cursor(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "sessions": [{"session_id": "sess_1", "state": "live"}],
                    "next_cursor": "cur_2",
                },
            )
        )
        sessions, next_cursor = make_client().sessions.list(state="live", limit=10, cursor="cur_1")
        call = transport.calls[0]
        assert call.params == {"state": "live", "limit": 10, "cursor": "cur_1"}
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess_1"
        assert next_cursor == "cur_2"

    def test_list_passes_external_id_filter(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"sessions": [{"session_id": "sess_1", "state": "live"}]}
            )
        )
        make_client().sessions.list(external_id="workflow-42")
        assert transport.calls[0].params == {"external_id": "workflow-42"}


class TestSessionsAPIBatchExec:
    def test_batch_exec_wraps_string_command_and_maps_results(self, fake_http):
        transport = fake_http(
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
        results = make_client().sessions.batch_exec(["sess_1", "sess_2"], "echo hi")

        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path == "https://api.inis.run/v1/sessions/batch/exec"
        assert call.json_body["command"] == ["bash", "-lc", "echo hi"]
        assert call.json_body["session_ids"] == ["sess_1", "sess_2"]

        assert results[0].session_id == "sess_1"
        assert results[0].stdout == "ok"
        assert results[1].session_id == "sess_2"
        assert results[1].error == "timed out"

    def test_batch_exec_accepts_argv_list_unmodified(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(200, {"results": []})
        )
        make_client().sessions.batch_exec(["sess_1"], ["python3", "-c", "print(1)"])
        assert transport.calls[0].json_body["command"] == ["python3", "-c", "print(1)"]

    def test_static_batch_exec_still_works_standalone(self, fake_http):
        """Session.batch_exec (static) is retained for backward compatibility
        even though the primary surface moved to Client.sessions.batch_exec."""
        transport = fake_http(
            lambda method, path, params, body: json_response(200, {"results": []})
        )
        Session.batch_exec(
            base_url="https://api.inis.run",
            token="tok_abc",
            session_ids=["sess_1"],
            command="echo hi",
        )
        assert transport.calls[0].json_body["session_ids"] == ["sess_1"]


class TestSessionsAPIFiles:
    def test_read_write_list_by_session_id(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"content": "data", "entries": ["a"]}
            )
        )
        sessions = make_client().sessions
        assert sessions.read_file("sess_1", "/a") == "data"
        assert transport.calls[-1].path == "https://api.inis.run/v1/sessions/sess_1/files"
        assert sessions.list_files("sess_1", "/") == ["a"]
        sessions.write_file("sess_1", "/a", "content")
        assert transport.calls[-1].method == "PUT"
        assert transport.calls[-1].json_body == {"path": "/a", "content": "content", "encoding": "text"}


class TestCheckpointsAPI:
    def test_get(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"checkpoint_id": "cp_1", "size_bytes": 10}
            )
        )
        cp = make_client().checkpoints.get("cp_1")
        assert transport.calls[0].path == "https://api.inis.run/v1/checkpoints/cp_1"
        assert cp.checkpoint_id == "cp_1"

    def test_delete(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(204, {}))
        make_client().checkpoints.delete("cp_1")
        assert transport.calls[0].method == "DELETE"
        assert transport.calls[0].path == "https://api.inis.run/v1/checkpoints/cp_1"

    def test_create_session_branches_from_checkpoint(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(200, {"session_id": "sess_branch"})
        )
        session = make_client().checkpoints.create_session(
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
        assert isinstance(session, Session)
        assert session.session_id == "sess_branch"


class TestArtifactsAPI:
    def test_get_maps_files(self, fake_http):
        fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "id": "art_1",
                    "status": "ready",
                    "files": [{"path": "out.txt", "url": "https://x", "size_bytes": 5}],
                },
            )
        )
        artifact = make_client().artifacts.get("art_1")
        assert artifact.status == "ready"
        assert artifact.files[0].path == "out.txt"

    def test_extend(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(200, {"id": "art_1", "status": "ready"})
        )
        make_client().artifacts.extend("art_1", 7)
        assert transport.calls[0].path == "https://api.inis.run/v1/artifacts/art_1/extend"
        assert transport.calls[0].json_body == {"ttl_days": 7}

    def test_delete(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(204, {}))
        make_client().artifacts.delete("art_1")
        assert transport.calls[0].method == "DELETE"


class TestTemplatesAPI:
    def test_list(self, fake_http):
        fake_http(
            lambda method, path, params, body: json_response(
                200, {"templates": [{"name": "python-3.12", "kind": "official"}]}
            )
        )
        templates = make_client().templates.list()
        assert len(templates) == 1
        assert templates[0].name == "python-3.12"
        assert templates[0].kind == "official"

    def test_delete(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(204, {}))
        make_client().templates.delete("my-template")
        assert transport.calls[0].path == "https://api.inis.run/v1/templates/my-template"
        assert transport.calls[0].method == "DELETE"


class TestRegistriesAPI:
    def test_add_sends_fields_and_never_leaks_secret_in_the_return_value(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                201,
                {
                    "id": "cred_1",
                    "name": "my-ghcr",
                    "registry_host": "ghcr.io",
                    "username": "user",
                    "secret_last4": "7890",
                    "created_at": "2026-07-24T00:00:00Z",
                },
            )
        )
        cred = make_client().registries.add(
            "my-ghcr",
            registry_host="ghcr.io",
            username="user",
            secret="ghp_should-only-appear-in-this-one-request-body-1234567890",
        )
        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path == "https://api.inis.run/v1/registry-credentials"
        assert call.json_body == {
            "name": "my-ghcr",
            "registry_host": "ghcr.io",
            "username": "user",
            "secret": "ghp_should-only-appear-in-this-one-request-body-1234567890",
        }
        assert cred.name == "my-ghcr"
        assert cred.registry_host == "ghcr.io"
        assert cred.username == "user"
        assert cred.secret_last4 == "7890"
        assert not hasattr(cred, "secret")

    def test_list(self, fake_http):
        fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "credentials": [
                        {"id": "cred_1", "name": "my-ghcr", "registry_host": "ghcr.io", "username": "user"}
                    ]
                },
            )
        )
        creds = make_client().registries.list()
        assert len(creds) == 1
        assert creds[0].name == "my-ghcr"
        assert creds[0].registry_host == "ghcr.io"

    def test_delete(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(204, {}))
        make_client().registries.delete("my-ghcr")
        assert transport.calls[0].path == "https://api.inis.run/v1/registry-credentials/my-ghcr"
        assert transport.calls[0].method == "DELETE"


class TestConnectorsAPI:
    def test_add_sends_fields_and_never_leaks_secret_in_the_return_value(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                201,
                {
                    "id": "conn_1",
                    "name": "stripe",
                    "target_base_url": "https://api.stripe.com",
                    "auth_shape": "bearer",
                    "secret_last4": "7890",
                    "created_at": "2026-07-24T00:00:00Z",
                },
            )
        )
        conn = make_client().connectors.add(
            "stripe",
            target_base_url="https://api.stripe.com",
            secret="sk_live_should-only-appear-in-this-one-request-body-1234567890",
        )
        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path == "https://api.inis.run/v1/connectors"
        assert call.json_body == {
            "name": "stripe",
            "target_base_url": "https://api.stripe.com",
            "auth_shape": "bearer",
            "secret": "sk_live_should-only-appear-in-this-one-request-body-1234567890",
        }
        assert conn.name == "stripe"
        assert conn.target_base_url == "https://api.stripe.com"
        assert conn.auth_shape == "bearer"
        assert conn.secret_last4 == "7890"
        assert not hasattr(conn, "secret")

    def test_add_header_shape_sends_header_name(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                201,
                {
                    "id": "conn_2",
                    "name": "acme",
                    "target_base_url": "https://api.acme.example",
                    "auth_shape": "header",
                    "header_name": "X-Api-Key",
                },
            )
        )
        make_client().connectors.add(
            "acme",
            target_base_url="https://api.acme.example",
            auth_shape="header",
            header_name="X-Api-Key",
            secret="acme-secret",
        )
        assert transport.calls[0].json_body["header_name"] == "X-Api-Key"
        assert transport.calls[0].json_body["auth_shape"] == "header"

    def test_list(self, fake_http):
        fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "connectors": [
                        {"id": "conn_1", "name": "stripe", "target_base_url": "https://api.stripe.com", "auth_shape": "bearer"}
                    ]
                },
            )
        )
        conns = make_client().connectors.list()
        assert len(conns) == 1
        assert conns[0].name == "stripe"
        assert conns[0].target_base_url == "https://api.stripe.com"

    def test_delete(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(204, {}))
        make_client().connectors.delete("stripe")
        assert transport.calls[0].path == "https://api.inis.run/v1/connectors/stripe"
        assert transport.calls[0].method == "DELETE"


class TestDomainsAPI:
    def test_add_sends_domain_and_maps_verify_instructions(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                201,
                {
                    "id": "dom_1",
                    "domain": "app.customer.com",
                    "verified": False,
                    "verify_txt_name": "_inis-verify.app.customer.com",
                    "verify_txt_value": "tok_abc123",
                    "created_at": "2026-07-24T00:00:00Z",
                },
            )
        )
        d = make_client().domains.add("app.customer.com")
        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path == "https://api.inis.run/v1/org/domains"
        assert call.json_body == {"domain": "app.customer.com"}
        assert d.id == "dom_1"
        assert d.verified is False
        assert d.verify_txt_name == "_inis-verify.app.customer.com"
        assert d.verify_txt_value == "tok_abc123"

    def test_list(self, fake_http):
        fake_http(
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
                            "session_id": "ses_1",
                            "port": 8000,
                        }
                    ]
                },
            )
        )
        domains = make_client().domains.list()
        assert len(domains) == 1
        assert domains[0].verified is True
        assert domains[0].session_id == "ses_1"
        assert domains[0].port == 8000

    def test_delete(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(204, {}))
        make_client().domains.delete("dom_1")
        assert transport.calls[0].method == "DELETE"
        assert transport.calls[0].path == "https://api.inis.run/v1/org/domains/dom_1"

    def test_verify(self, fake_http):
        transport = fake_http(
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
        d = make_client().domains.verify("dom_1")
        assert transport.calls[0].method == "POST"
        assert transport.calls[0].path == "https://api.inis.run/v1/org/domains/dom_1/verify"
        assert d.verified is True

    def test_verify_failure_raises_inis_error(self, fake_http):
        fake_http(lambda method, path, params, body: error_response(409, "verification failed"))
        with pytest.raises(InisError):
            make_client().domains.verify("dom_1")

    def test_route_sends_session_and_port(self, fake_http):
        transport = fake_http(
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
        d = make_client().domains.route("dom_1", "ses_1", 8000)
        call = transport.calls[0]
        assert call.method == "PUT"
        assert call.path == "https://api.inis.run/v1/org/domains/dom_1/route"
        assert call.json_body == {"session_id": "ses_1", "port": 8000}
        assert d.session_id == "ses_1"
        assert d.port == 8000

    def test_route_before_verified_raises_inis_error(self, fake_http):
        fake_http(lambda method, path, params, body: error_response(409, "domain must be verified"))
        with pytest.raises(InisError):
            make_client().domains.route("dom_1", "ses_1", 8000)

    def test_unroute_clears_binding(self, fake_http):
        transport = fake_http(
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
        d = make_client().domains.unroute("dom_1")
        call = transport.calls[0]
        assert call.method == "DELETE"
        assert call.path == "https://api.inis.run/v1/org/domains/dom_1/route"
        assert d.session_id is None
        assert d.port is None


class TestVolumesAPI:
    """Standalone volume CRUD. Lazy creation is retired -- a volume
    must be created explicitly before its id can be attached anywhere, and
    an unknown/cross-tenant id (get/delete) or an over-cap size (create)
    must surface as a clean InisError, not a raw HTTP failure."""

    def test_create_with_no_size_omits_size_gb(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                201,
                {"id": "vol_1", "size_gb": 1, "created_at": "2026-08-07T00:00:00Z", "attached": False},
            )
        )
        v = make_client().volumes.create()
        call = transport.calls[0]
        assert call.method == "POST"
        assert call.path == "https://api.inis.run/v1/volumes"
        assert call.json_body == {}
        assert v.id == "vol_1"
        assert v.size_gb == 1
        assert v.attached is False
        assert v.session_id is None

    def test_create_with_explicit_size_sends_size_gb(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                201,
                {"id": "vol_2", "size_gb": 5, "created_at": "2026-08-07T00:00:00Z", "attached": False},
            )
        )
        v = make_client().volumes.create(size_gb=5)
        assert transport.calls[0].json_body == {"size_gb": 5}
        assert v.size_gb == 5

    def test_create_over_cap_raises_inis_error(self, fake_http):
        fake_http(
            lambda method, path, params, body: error_response(
                400, "size_gb exceeds maximum (10 GB)", code="bad_request"
            )
        )
        with pytest.raises(InisError) as exc_info:
            make_client().volumes.create(size_gb=999)
        assert exc_info.value.code == "bad_request"
        assert exc_info.value.status == 400

    def test_list_empty(self, fake_http):
        fake_http(lambda method, path, params, body: json_response(200, {"volumes": []}))
        assert make_client().volumes.list() == []

    def test_list_populated(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "volumes": [
                        {"id": "vol_1", "size_gb": 1, "created_at": "2026-08-01T00:00:00Z", "attached": False},
                        {
                            "id": "vol_2",
                            "size_gb": 5,
                            "created_at": "2026-08-02T00:00:00Z",
                            "attached": True,
                            "session_id": "ses_xyz",
                        },
                    ]
                },
            )
        )
        volumes = make_client().volumes.list()
        assert transport.calls[0].method == "GET"
        assert transport.calls[0].path == "https://api.inis.run/v1/volumes"
        assert [v.id for v in volumes] == ["vol_1", "vol_2"]
        assert volumes[1].attached is True
        assert volumes[1].session_id == "ses_xyz"

    def test_get_returns_volume(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200,
                {"id": "vol_1", "size_gb": 2, "created_at": "2026-08-01T00:00:00Z", "attached": False},
            )
        )
        v = make_client().volumes.get("vol_1")
        assert transport.calls[0].path == "https://api.inis.run/v1/volumes/vol_1"
        assert v.id == "vol_1"
        assert v.size_gb == 2

    def test_get_never_saved_has_no_last_saved_at(self, fake_http):
        """A volume that has never completed a push must map to
        last_saved_at=None -- never a zeroed/synthesized timestamp. That
        None is the one signal that distinguishes "no durable copy yet"
        from "saved a long time ago"."""
        fake_http(
            lambda method, path, params, body: json_response(
                200,
                {"id": "vol_1", "size_gb": 2, "created_at": "2026-08-01T00:00:00Z", "attached": False},
            )
        )
        v = make_client().volumes.get("vol_1")
        assert v.last_saved_at is None

    def test_get_saved_returns_last_saved_at(self, fake_http):
        fake_http(
            lambda method, path, params, body: json_response(
                200,
                {
                    "id": "vol_1",
                    "size_gb": 2,
                    "created_at": "2026-08-01T00:00:00Z",
                    "attached": False,
                    "last_saved_at": "2026-08-07T10:00:00Z",
                },
            )
        )
        v = make_client().volumes.get("vol_1")
        assert v.last_saved_at == "2026-08-07T10:00:00Z"

    def test_get_unknown_id_raises_clean_not_found(self, fake_http):
        fake_http(lambda method, path, params, body: error_response(404, "volume not found", code="not_found"))
        with pytest.raises(InisError) as exc_info:
            make_client().volumes.get("vol_nonexistent")
        assert exc_info.value.code == "not_found"
        assert exc_info.value.status == 404

    def test_delete_unattached(self, fake_http):
        transport = fake_http(lambda method, path, params, body: json_response(204, {}))
        make_client().volumes.delete("vol_1")
        assert transport.calls[0].method == "DELETE"
        assert transport.calls[0].path == "https://api.inis.run/v1/volumes/vol_1"

    def test_delete_unknown_id_raises_clean_not_found(self, fake_http):
        fake_http(lambda method, path, params, body: error_response(404, "volume not found", code="not_found"))
        with pytest.raises(InisError) as exc_info:
            make_client().volumes.delete("vol_nonexistent")
        assert exc_info.value.code == "not_found"

    def test_delete_while_attached_raises_distinguishable_conflict(self, fake_http):
        fake_http(
            lambda method, path, params, body: error_response(
                409, "volume is attached to a live or paused session", code="conflict"
            )
        )
        with pytest.raises(InisError) as exc_info:
            make_client().volumes.delete("vol_attached")
        assert exc_info.value.code == "conflict"
        assert exc_info.value.status == 409

    def test_resize_grows(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200,
                {"id": "vol_1", "size_gb": 5, "created_at": "2026-08-01T00:00:00Z", "attached": False},
            )
        )
        v = make_client().volumes.resize("vol_1", 5)
        call = transport.calls[0]
        assert call.method == "PATCH"
        assert call.path == "https://api.inis.run/v1/volumes/vol_1"
        assert call.json_body == {"size_gb": 5}
        assert v.size_gb == 5

    def test_resize_shrink_raises_bad_request(self, fake_http):
        """Grow-only: a same-size or smaller request is refused outright,
        not silently accepted as a no-op or attempted as a shrink."""
        fake_http(
            lambda method, path, params, body: error_response(
                400,
                "size_gb must be greater than the current size (5 GB) — volumes only grow, they never shrink",
                code="bad_request",
            )
        )
        with pytest.raises(InisError) as exc_info:
            make_client().volumes.resize("vol_1", 3)
        assert exc_info.value.code == "bad_request"
        assert exc_info.value.status == 400

    def test_resize_while_attached_raises_distinguishable_conflict(self, fake_http):
        fake_http(
            lambda method, path, params, body: error_response(
                409, "volume is attached to session ses_abc; detach it before resizing", code="conflict"
            )
        )
        with pytest.raises(InisError) as exc_info:
            make_client().volumes.resize("vol_attached", 5)
        assert exc_info.value.code == "conflict"
        assert exc_info.value.status == 409

    def test_resize_over_quota_raises_conflict(self, fake_http):
        fake_http(
            lambda method, path, params, body: error_response(
                409,
                "volume storage quota exceeded: 8 GB used, 4 GB more requested, cap is 10 GB",
                code="conflict",
            )
        )
        with pytest.raises(InisError) as exc_info:
            make_client().volumes.resize("vol_1", 10)
        assert exc_info.value.code == "conflict"
        assert exc_info.value.status == 409


class TestSessionCreateOptions:
    def test_on_pty_detach_and_paused_ttl_forwarded(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        session = make_client().sessions.create(
            on_pty_detach="destroy", paused_ttl_ms=60_000, no_sudo=True, template="python-3.12"
        )
        call = transport.calls[0]
        assert call.json_body["on_pty_detach"] == "destroy"
        assert call.json_body["paused_ttl_ms"] == 60_000
        assert call.json_body["no_sudo"] is True
        assert call.json_body["template"] == "python-3.12"
        assert session.session_id == "sess_new"

    def test_external_id_forwarded_in_body_and_response(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live", "external_id": "workflow-42"}
            )
        )
        session = make_client().sessions.create(external_id="workflow-42")
        call = transport.calls[0]
        assert call.json_body["external_id"] == "workflow-42"
        assert session.session_id == "sess_new"

    def test_external_id_omitted_when_unset(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        make_client().sessions.create()
        assert "external_id" not in transport.calls[0].json_body

    def test_idempotency_key_sent_as_header_not_body(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        make_client().sessions.create(idempotency_key="retry-key-1")
        call = transport.calls[0]
        assert call.headers is not None
        assert call.headers.get("Idempotency-Key") == "retry-key-1"
        # Never leaks into the JSON body -- it's a header-only primitive.
        assert "idempotency_key" not in call.json_body

    def test_idempotency_key_header_omitted_when_unset(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        make_client().sessions.create()
        headers = transport.calls[0].headers or {}
        assert "Idempotency-Key" not in headers

    def test_egress_mode_sent_as_mode_field(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        make_client().sessions.create(egress_default="deny", egress_allow=["*.openai.com"])
        assert transport.calls[0].json_body["egress"] == {
            "mode": "deny",
            "allow": ["*.openai.com"],
        }

    def test_env_and_secrets_forwarded(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        make_client().sessions.create(
            env={"MODE": "prod"},
            secrets={"API_KEY": "sk-should-only-appear-in-this-one-request-body"},
        )
        call = transport.calls[0]
        assert call.json_body["env"] == {"MODE": "prod"}
        assert call.json_body["secrets"] == {
            "API_KEY": "sk-should-only-appear-in-this-one-request-body"
        }

    def test_env_and_secrets_omitted_when_unset(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        make_client().sessions.create()
        call = transport.calls[0]
        assert "env" not in call.json_body
        assert "secrets" not in call.json_body


class TestSessionCreateAsyncPolling:
    """POST /v1/sessions acknowledges immediately with state="creating"
    instead of blocking until the sandbox is ready. Session.create() keeps
    its historical blocking contract by polling GET internally — these tests
    cover that poll loop directly (the already-live fast path, progressing
    through phases to live, surfacing a failure, the on_status callback, and
    the wait=False escape hatch).
    """

    @pytest.fixture(autouse=True)
    def _fast_poll(self, monkeypatch):
        # Real poll cadence (0.3s) would make these tests slow for no
        # benefit; the interval value itself isn't what's under test.
        monkeypatch.setattr(Session, "_CREATE_POLL_INTERVAL_S", 0.001)

    def test_already_live_no_polling(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "live"}
            )
        )
        session = make_client().sessions.create()
        assert session.session_id == "sess_new"
        assert len(transport.calls) == 1  # just the POST, no GET polling

    def test_polls_through_creating_to_live(self, fake_http):
        responses = [
            {"session_id": "sess_new", "state": "creating", "status_detail": "preparing_template"},
            {"session_id": "sess_new", "state": "creating", "status_detail": "starting"},
            {"session_id": "sess_new", "state": "live"},
        ]
        calls = {"n": 0}

        def handler(method, path, params, body):
            resp = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return json_response(200, resp)

        transport = fake_http(handler)
        session = make_client().sessions.create()
        assert session.session_id == "sess_new"
        # 1 POST + 2 GET polls (creating, creating) before the 3rd call
        # (live) satisfies the loop.
        assert len(transport.calls) == 3
        assert transport.calls[0].method == "POST"
        assert transport.calls[1].method == "GET"
        assert transport.calls[2].method == "GET"

    def test_failed_raises_with_reason(self, fake_http):
        responses = [
            {"session_id": "sess_new", "state": "creating", "status_detail": "preparing_template"},
            {"session_id": "sess_new", "state": "failed", "status_reason": "template not found"},
        ]
        calls = {"n": 0}

        def handler(method, path, params, body):
            resp = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return json_response(200, resp)

        fake_http(handler)
        with pytest.raises(InisError) as excinfo:
            make_client().sessions.create()
        assert "template not found" in str(excinfo.value)

    def test_on_status_callback_receives_phases(self, fake_http):
        responses = [
            {"session_id": "sess_new", "state": "creating", "status_detail": "preparing_template"},
            {"session_id": "sess_new", "state": "creating", "status_detail": "starting"},
            {"session_id": "sess_new", "state": "live"},
        ]
        calls = {"n": 0}

        def handler(method, path, params, body):
            resp = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return json_response(200, resp)

        fake_http(handler)
        seen: list[str] = []
        make_client().sessions.create(on_status=seen.append)
        assert seen == ["preparing_template", "starting"]

    def test_wait_false_skips_polling(self, fake_http):
        transport = fake_http(
            lambda method, path, params, body: json_response(
                200, {"session_id": "sess_new", "state": "creating", "status_detail": "preparing_template"}
            )
        )
        session = make_client().sessions.create(wait=False)
        assert session.session_id == "sess_new"
        assert len(transport.calls) == 1  # no GET polling with wait=False


class TestClientErrorMapping:
    def test_get_raises_inis_error(self, fake_http):
        fake_http(lambda method, path, params, body: error_response(401, "unauthorized"))
        with pytest.raises(InisError) as excinfo:
            make_client().sessions.get("sess_1")
        assert "401" in str(excinfo.value)
        assert "unauthorized" in str(excinfo.value)

    def test_delete_raises_inis_error(self, fake_http):
        fake_http(lambda method, path, params, body: error_response(403, "forbidden"))
        with pytest.raises(InisError):
            make_client().templates.delete("x")
