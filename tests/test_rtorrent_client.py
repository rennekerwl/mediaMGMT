"""Mocked HTTP/XML-RPC tests for the rTorrent client boundary."""

from __future__ import annotations

import base64
import xmlrpc.client
from pathlib import Path

import httpx
import pytest

from media_scope.exceptions import (
    MagnetSubmissionUnsupportedError,
    RtorrentAuthenticationError,
    RtorrentConfigurationError,
    RtorrentRpcError,
)
from media_scope.rtorrent_client import RtorrentClient, sanitize_rpc_endpoint

REQUIRED = {
    "system.client_version",
    "system.library_version",
    "system.api_version",
    "d.name",
    "d.is_meta",
    "d.directory.set",
    "d.custom.set",
    "d.stop",
    "d.erase",
    "load.start_verbose",
    "d.peers_connected",
    "d.peers_complete",
    "d.message",
}


def xml_response(value: object) -> bytes:
    return xmlrpc.client.dumps((value,), methodresponse=True, allow_none=True).encode()


def rpc_transport(
    methods: set[str] = REQUIRED,
    *,
    captured: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        params, method = xmlrpc.client.loads(request.content)
        if method == "system.listMethods":
            return httpx.Response(200, content=xml_response(sorted(methods)))
        versions = {
            "system.client_version": "0.9.8",
            "system.library_version": "0.13.8",
            "system.api_version": "9",
        }
        if method in versions:
            return httpx.Response(200, content=xml_response(versions[method]))
        if method == "d.name":
            return httpx.Response(200, content=xml_response("Example"))
        if method == "d.is_meta":
            return httpx.Response(200, content=xml_response(0))
        if method in {"d.peers_connected", "d.peers_complete"}:
            return httpx.Response(200, content=xml_response(2))
        if method == "d.message":
            return httpx.Response(200, content=xml_response(""))
        return httpx.Response(200, content=xml_response(0))

    return httpx.MockTransport(handler)


def test_missing_rpc_url_is_configuration_error() -> None:
    with pytest.raises(RtorrentConfigurationError):
        RtorrentClient("")


def test_successful_connection_discovers_versions_and_methods() -> None:
    with RtorrentClient("http://localhost/custom/RPC", transport=rpc_transport()) as client:
        capabilities = client.discover_capabilities()
    assert capabilities.client_version == "0.9.8"
    assert capabilities.library_version == "0.13.8"
    assert capabilities.api_version == "9"
    assert capabilities.load_method == "load.start_verbose"
    assert capabilities.metadata_detection_method == "d.is_meta"


def test_basic_authentication_is_sent_without_leaking_password() -> None:
    captured: list[httpx.Request] = []
    with RtorrentClient(
        "https://localhost/RPC",
        username="alice",
        password="secret-value",
        transport=rpc_transport(captured=captured),
    ) as client:
        client.discover_capabilities()
    expected = base64.b64encode(b"alice:secret-value").decode()
    assert captured[0].headers["authorization"] == f"Basic {expected}"
    assert "secret-value" not in sanitize_rpc_endpoint("https://alice:secret-value@host/RPC")


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_rejection_is_distinct(status: int) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(status))
    with RtorrentClient("http://localhost/RPC", transport=transport) as client:
        with pytest.raises(RtorrentAuthenticationError):
            client.discover_capabilities()


def test_timeout_is_sanitized_rpc_error() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("contains-sensitive-transport-detail", request=request)

    with RtorrentClient("http://localhost/RPC", transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(RtorrentRpcError) as captured:
            client.discover_capabilities()
    assert "sensitive" not in str(captured.value)


def test_malformed_xmlrpc_response_is_rejected() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"<broken"))
    with RtorrentClient("http://localhost/RPC", transport=transport) as client:
        with pytest.raises(RtorrentRpcError):
            client.discover_capabilities()


def test_unsupported_magnet_submission_is_reported() -> None:
    methods = REQUIRED - {"load.start_verbose"}
    with RtorrentClient("http://localhost/RPC", transport=rpc_transport(methods)) as client:
        with pytest.raises(MagnetSubmissionUnsupportedError) as captured:
            client.discover_capabilities()
    assert captured.value.error_code == "MAGNET_SUBMISSION_UNSUPPORTED"


def test_compatibility_metadata_fallback_is_selected() -> None:
    methods = (REQUIRED - {"d.is_meta"}) | {"d.size_files", "d.size_bytes"}
    with RtorrentClient("http://localhost/RPC", transport=rpc_transport(methods)) as client:
        capabilities = client.discover_capabilities()
    assert capabilities.metadata_detection_method == "compatibility_fallback"


def test_compatibility_fallback_rejects_rtorrent_meta_placeholder() -> None:
    methods = (REQUIRED - {"d.is_meta"}) | {"d.size_files", "d.size_bytes"}

    def handler(request: httpx.Request) -> httpx.Response:
        _params, method = xmlrpc.client.loads(request.content)
        if method == "system.listMethods":
            value: object = sorted(methods)
        elif method == "system.client_version":
            value = "0.9.6"
        elif method == "system.library_version":
            value = "0.13.6"
        elif method == "system.api_version":
            value = "9"
        elif method == "d.name":
            value = f"{'A' * 40}.meta"
        elif method in {"d.size_files", "d.size_bytes"}:
            value = 1
        elif method in {"d.peers_connected", "d.peers_complete"}:
            value = 0
        elif method == "d.message":
            value = ""
        else:
            value = 0
        return httpx.Response(200, content=xml_response(value))

    with RtorrentClient(
        "http://localhost/RPC",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.discover_capabilities()
        status = client.status("a" * 40)
    assert status.metadata_retrieved is False
    assert status.detection_method == "compatibility_fallback"


def test_normal_load_compatibility_path_starts_torrent_after_loading(tmp_path: Path) -> None:
    captured: list[httpx.Request] = []
    methods = (REQUIRED - {"load.start_verbose"}) | {"load.normal_verbose", "d.start"}
    with RtorrentClient(
        "http://localhost/RPC",
        transport=rpc_transport(methods, captured=captured),
    ) as client:
        capabilities = client.discover_capabilities()
        selected = client.submit_magnet(
            f"magnet:?xt=urn:btih:{'a' * 40}",
            "a" * 40,
            tmp_path,
        )
    called = [xmlrpc.client.loads(request.content)[1] for request in captured]
    assert capabilities.load_starts is False
    assert selected == "load.normal_verbose"
    assert called[-2:] == ["load.normal_verbose", "d.start"]


def test_endpoint_sanitization_removes_query_fragment_and_userinfo() -> None:
    value = sanitize_rpc_endpoint("https://user:pass@example.test:443/path?token=x#fragment")
    assert value == "https://example.test:443/path"
