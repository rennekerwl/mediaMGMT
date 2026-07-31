"""Magnet validation, Jackett-reference resolution, and metainfo tests."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from media_scope.jackett_client import JackettClient
from media_scope.magnet_resolver import (
    MagnetResolution,
    MagnetResolutionError,
    MagnetResolutionFailure,
    MagnetResolver,
    construct_magnet_uri,
    parse_torrent_metainfo,
    validate_magnet_uri,
)
from media_scope.release_classifier import normalize_release_title
from media_scope.search_models import RawRelease, SearchScope, TorznabCategory
from media_scope.search_service import deduplicate_and_classify

FIXTURES = Path(__file__).parent / "fixtures" / "jackett_responses"
HASH_A = "ab" * 20
HASH_B = "cd" * 20
SCOPE = SearchScope(1, "Example Show", None, 2017, "Ended", (1, 2, 3, 4, 5))
TV = (TorznabCategory(5000, "TV"),)


def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleeps: list[float] | None = None,
) -> JackettClient:
    return JackettClient(
        "http://127.0.0.1:9117",
        "secret-key",
        transport=httpx.MockTransport(handler),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def canonical(
    *,
    infohash: str | None = None,
    torznab_magnet: str | None = None,
    field_magnets: tuple[str, ...] = (),
    references: tuple[str, ...] = (),
):
    release = RawRelease(
        sequence=0,
        indexer_id="alpha",
        indexer_name="Alpha",
        query="Example Show Complete Series",
        original_title="Example Show S01-S05 Complete",
        normalized_title=normalize_release_title("Example Show S01-S05 Complete"),
        guid="stable-guid",
        categories=TV,
        infohash=infohash,
        magnet_uri=torznab_magnet or (field_magnets[0] if field_magnets else None),
        torznab_magnet_uri=torznab_magnet,
        result_field_magnets=field_magnets,
        internal_download_references=references,
    )
    return deduplicate_and_classify([release], SCOPE)[0]


def test_validate_magnet_normalizes_base32_btih_and_rejects_unsafe_values() -> None:
    base32_hash = base64.b32encode(bytes.fromhex(HASH_A)).decode()
    validated = validate_magnet_uri(
        f"MaGnEt:?dn=Example%20Show&xt=urn:btih:{base32_hash}"
        f"&xt=urn:btih:{HASH_B}&tr=https%3A%2F%2Ftracker.example"
    )
    assert validated.infohash == HASH_A
    assert validated.magnet_uri.count("xt=") == 1
    assert f"urn:btih:{HASH_A}" in validated.magnet_uri
    assert "dn=Example%20Show" in validated.magnet_uri

    invalid = (
        "",
        "https://example.test/file.torrent",
        "magnet:?dn=no-hash",
        "magnet:?xt=urn:btih:not-valid",
        f"magnet:?xt=urn:btih:{HASH_A}&apikey=secret",
        f"magnet:?xt=urn:btih:{HASH_A}%0A",
    )
    for value in invalid:
        with pytest.raises(MagnetResolutionError):
            validate_magnet_uri(value)


def test_constructed_magnet_is_tracker_free_and_encodes_display_name() -> None:
    value = construct_magnet_uri(HASH_A.upper(), "Example Show: Complete / Series")
    assert value == (f"magnet:?xt=urn:btih:{HASH_A}&dn=Example%20Show%3A%20Complete%20%2F%20Series")
    assert "&tr=" not in value


def test_resolver_precedence_is_torznab_then_fields_then_infohash() -> None:
    torznab = f"magnet:?xt=urn:btih:{HASH_A}&dn=Torznab"
    result_field = f"magnet:?xt=urn:btih:{HASH_B}&dn=Field"
    with client_for(lambda _request: pytest.fail("HTTP resolution was not expected")) as client:
        resolved = MagnetResolver(client).resolve(
            canonical(
                infohash="ef" * 20,
                torznab_magnet=torznab,
                field_magnets=(result_field,),
            )
        )
    assert isinstance(resolved, MagnetResolution)
    assert resolved.infohash == HASH_A
    assert resolved.source == "torznab_magneturl"

    with client_for(lambda _request: pytest.fail("HTTP resolution was not expected")) as client:
        resolved = MagnetResolver(client).resolve(
            canonical(infohash=HASH_A, field_magnets=(result_field,))
        )
    assert isinstance(resolved, MagnetResolution)
    assert resolved.infohash == HASH_B
    assert resolved.source == "result_field"

    with client_for(lambda _request: pytest.fail("HTTP resolution was not expected")) as client:
        resolved = MagnetResolver(client).resolve(canonical(infohash=HASH_A))
    assert isinstance(resolved, MagnetResolution)
    assert resolved.source == "torznab_infohash"
    assert "&tr=" not in resolved.magnet_uri


@pytest.mark.parametrize(
    ("response", "expected_source"),
    [
        (
            httpx.Response(302, headers={"Location": f"magnet:?xt=urn:btih:{HASH_A}"}),
            "jackett_redirect",
        ),
        (
            httpx.Response(
                200,
                text=f"download: magnet:?xt=urn:btih:{HASH_A}&dn=Example",
                headers={"Content-Type": "text/plain"},
            ),
            "jackett_plain_text",
        ),
        (
            httpx.Response(
                200,
                text=f'<html><a href="magnet:?xt=urn:btih:{HASH_A}&amp;dn=Example">go</a></html>',
                headers={"Content-Type": "text/html"},
            ),
            "jackett_html",
        ),
    ],
)
def test_resolves_redirect_plain_text_and_html(
    response: httpx.Response,
    expected_source: str,
) -> None:
    with client_for(lambda _request: response) as client:
        resolved = MagnetResolver(client).resolve(
            canonical(references=("http://127.0.0.1:9117/dl?id=1&apikey=secret-key",))
        )
    assert isinstance(resolved, MagnetResolution)
    assert resolved.infohash == HASH_A
    assert resolved.source == expected_source
    assert "secret-key" not in resolved.magnet_uri


def test_follows_relative_http_redirect_without_forwarding_jackett_key() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/finish"})
        return httpx.Response(200, text=f"magnet:?xt=urn:btih:{HASH_A}")

    with client_for(handler) as client:
        resolved = MagnetResolver(client).resolve(
            canonical(references=("http://127.0.0.1:9117/start?apikey=secret-key",))
        )
    assert isinstance(resolved, MagnetResolution)
    assert len(requests) == 2
    assert "apikey=" not in requests[1]


def test_redirect_loop_limit_and_unsupported_scheme_fail_safely() -> None:
    def loop_handler(request: httpx.Request) -> httpx.Response:
        target = "/two" if request.url.path == "/one" else "/one"
        return httpx.Response(302, headers={"Location": target})

    with client_for(loop_handler) as client:
        failed = MagnetResolver(client).resolve(
            canonical(references=("http://127.0.0.1:9117/one",))
        )
    assert isinstance(failed, MagnetResolutionFailure)
    assert "JACKETT_REDIRECT_LOOP" in failed.diagnostics

    with client_for(
        lambda _request: httpx.Response(302, headers={"Location": "ftp://example.test/file"})
    ) as client:
        failed = MagnetResolver(client).resolve(
            canonical(references=("http://127.0.0.1:9117/start",))
        )
    assert isinstance(failed, MagnetResolutionFailure)
    assert "UNSUPPORTED_REDIRECT_SCHEME" in failed.diagnostics

    calls = 0

    def endless(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": f"/redirect-{calls}"})

    with client_for(endless) as client:
        failed = MagnetResolver(client, max_redirects=2).resolve(
            canonical(references=("http://127.0.0.1:9117/start",))
        )
    assert isinstance(failed, MagnetResolutionFailure)
    assert "JACKETT_REDIRECT_LIMIT" in failed.diagnostics
    assert calls == 3


def test_torrent_metainfo_hashes_exact_raw_info_bytes_and_rejects_private() -> None:
    public = (FIXTURES / "public.torrent").read_bytes().strip()
    expected_info = b"d4:name12:Example Show6:lengthi123ee"
    parsed = parse_torrent_metainfo(public)
    assert (
        parsed.infohash
        == hashlib.sha1(
            expected_info,
            usedforsecurity=False,
        ).hexdigest()
    )
    assert parsed.display_name == "Example Show"
    assert not parsed.private

    reordered = b"d4:infod6:lengthi123e4:name12:Example Showee"
    reordered_parsed = parse_torrent_metainfo(reordered)
    assert (
        reordered_parsed.infohash
        == hashlib.sha1(
            b"d6:lengthi123e4:name12:Example Showe",
            usedforsecurity=False,
        ).hexdigest()
    )
    assert reordered_parsed.infohash != parsed.infohash

    private = (FIXTURES / "private.torrent").read_bytes().strip()
    assert parse_torrent_metainfo(private).private


def test_torrent_body_produces_tracker_free_magnet_and_private_is_rejected() -> None:
    public = (FIXTURES / "public.torrent").read_bytes().strip()
    with client_for(
        lambda _request: httpx.Response(
            200,
            content=public,
            headers={"Content-Type": "application/x-bittorrent"},
        )
    ) as client:
        resolved = MagnetResolver(client).resolve(
            canonical(references=("http://127.0.0.1:9117/dl/public",))
        )
    assert isinstance(resolved, MagnetResolution)
    assert resolved.source == "jackett_torrent_file"
    assert resolved.magnet_uri.endswith("&dn=Example%20Show")
    assert "&tr=" not in resolved.magnet_uri

    private = (FIXTURES / "private.torrent").read_bytes().strip()
    with client_for(lambda _request: httpx.Response(200, content=private)) as client:
        failed = MagnetResolver(client).resolve(
            canonical(references=("http://127.0.0.1:9117/dl/private",))
        )
    assert isinstance(failed, MagnetResolutionFailure)
    assert failed.code == "PRIVATE_TORRENT_MAGNET_UNSUPPORTED"


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not-bencode",
        b"d4:info",
        b"d3:foo3:bare",
        b"d4:infole",
        b"d4:infod4:name4:testeejunk",
        b"d4:infod4:name4:testee4:infod4:name5:otheree",
    ],
)
def test_invalid_torrent_metainfo_is_rejected(content: bytes) -> None:
    with pytest.raises(MagnetResolutionError):
        parse_torrent_metainfo(content)


def test_fetch_retries_bounded_statuses_and_rejects_oversized_body() -> None:
    calls = 0
    sleeps: list[float] = []

    def retry_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "60"})
        if calls == 2:
            return httpx.Response(503)
        return httpx.Response(200, text=f"magnet:?xt=urn:btih:{HASH_A}")

    with client_for(retry_handler, sleeps=sleeps) as client:
        resolved = MagnetResolver(client).resolve(
            canonical(references=("http://127.0.0.1:9117/dl",))
        )
    assert isinstance(resolved, MagnetResolution)
    assert calls == 3
    assert sleeps == [30.0, 2.0]

    with client_for(
        lambda _request: httpx.Response(200, headers={"Content-Length": "1000"})
    ) as client:
        failed = MagnetResolver(client, max_response_bytes=10).resolve(
            canonical(references=("http://127.0.0.1:9117/dl",))
        )
    assert isinstance(failed, MagnetResolutionFailure)
    assert failed.code == "NO_USABLE_MAGNET"
    assert "JACKETT_INVALID_RESPONSE" in failed.diagnostics


@pytest.mark.parametrize(
    ("status_code", "expected_diagnostic", "expected_calls"),
    [
        (401, "JACKETT_AUTHENTICATION_ERROR", 1),
        (404, "JACKETT_API_ERROR", 1),
        (503, "JACKETT_UNAVAILABLE", 4),
    ],
)
def test_acquisition_http_failures_are_sanitized_and_bounded(
    status_code: int,
    expected_diagnostic: str,
    expected_calls: int,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code)

    with client_for(handler) as client:
        failed = MagnetResolver(client).resolve(
            canonical(references=("http://127.0.0.1:9117/dl?id=1&apikey=secret-key",))
        )
    assert isinstance(failed, MagnetResolutionFailure)
    assert failed.code == "NO_USABLE_MAGNET"
    assert expected_diagnostic in failed.diagnostics
    assert calls == expected_calls
    assert "secret-key" not in repr(failed)


def test_acquisition_network_timeout_uses_bounded_retries_without_leaking_url() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout(
            "request contained secret-key",
            request=request,
        )

    with client_for(handler) as client:
        failed = MagnetResolver(client).resolve(
            canonical(references=("http://127.0.0.1:9117/dl?id=1&apikey=secret-key",))
        )
    assert isinstance(failed, MagnetResolutionFailure)
    assert failed.code == "NO_USABLE_MAGNET"
    assert "JACKETT_UNAVAILABLE" in failed.diagnostics
    assert calls == 4
    assert "secret-key" not in repr(failed)


def test_duplicate_reference_is_fetched_once_and_non_jackett_reference_is_ignored() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="not a magnet or torrent")

    with client_for(handler) as client:
        resolver = MagnetResolver(client)
        result = canonical(
            references=(
                "http://127.0.0.1:9117/dl?id=1&apikey=secret-key",
                "http://127.0.0.1:9117/dl?id=1",
                "https://outside.example/file",
            )
        )
        assert isinstance(resolver.resolve(result), MagnetResolutionFailure)
        assert isinstance(resolver.resolve(result), MagnetResolutionFailure)
    assert calls == 1


def test_no_reference_returns_stable_failure() -> None:
    with client_for(lambda _request: pytest.fail("HTTP resolution was not expected")) as client:
        failed = MagnetResolver(client).resolve(canonical())
    assert isinstance(failed, MagnetResolutionFailure)
    assert failed.code == "NO_USABLE_MAGNET"
    assert failed.diagnostics == ("JACKETT_REFERENCE_MISSING",)
