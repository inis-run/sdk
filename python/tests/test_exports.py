"""Package-root re-exports — types consumers import from ``inis`` directly."""

from __future__ import annotations

from inis import RequestEndpointInfo, SessionUpdateInfo


def test_request_endpoint_info_exported_from_package_root() -> None:
    assert RequestEndpointInfo.__name__ == "RequestEndpointInfo"


def test_session_update_info_exported_from_package_root() -> None:
    assert SessionUpdateInfo.__name__ == "SessionUpdateInfo"
