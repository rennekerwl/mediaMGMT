"""Jackett capabilities, retries, Torznab parsing, and secret-safety tests."""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from media_scope.exceptions import (
    JackettApiError,
    JackettAuthenticationError,
    JackettConfigurationError,
    JackettNetworkError,
    JackettResponseError,
)
from media_scope.jackett_client import (
    JackettClient,
    magnet_btih,
    normalize_infohash,
    normalize_jackett_url,
    parse_torznab_results,
    sanitize_result_url,
)
from media_scope.search_models import IndexerCapabilities, TorznabCategory

FIXTURES = Path(__file__).parent / "fixtures" / "jackett_responses"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleeps: list[float] | None = None,
) -> JackettClient:
    recorded = sleeps if sleeps is not None else []
    return JackettClient(
        "http://127.0.0.1:9117/",
        "secret-key",
        transport=httpx.MockTransport(handler),
        sleep=recorded.append,
    )


def test_url_normalization_preserves_proxy_path_and_rejects_unsafe_values() -> None:
    assert normalize_jackett_url(" http://localhost:9117/jackett/ ") == (
        "http://localhost:9117/jackett"
    )
    for value in ("", "ftp://localhost", "http://user:pass@localhost", "http://x/?a=1"):
        with pytest.raises(JackettConfigurationError):
            normalize_jackett_url(value)


def test_result_url_removes_api_credentials_but_preserves_other_parameters() -> None:
    value = sanitize_result_url(
        "https://jackett.test/dl?APIKEY=secret&path=one&PASSKEY=also-secret"
    )
    assert value == "https://jackett.test/dl?path=one"
    magnet = "magnet:?xt=urn:btih:abcd&tr=https%3A%2F%2Ftracker"
    assert sanitize_result_url(magnet) == magnet


def test_infohash_and_magnet_normalization() -> None:
    raw = bytes.fromhex("ab" * 20)
    base32_value = base64.b32encode(raw).decode()
    assert normalize_infohash("AB" * 20) == "ab" * 20
    assert normalize_infohash(base32_value) == "ab" * 20
    assert normalize_infohash("not-a-valid-infohash") is None
    assert magnet_btih(f"magnet:?xt=urn:btih:{base32_value}") == "ab" * 20


def test_aggregate_indexer_id_is_rejected() -> None:
    with client_for(lambda _request: httpx.Response(200)) as client:
        with pytest.raises(JackettConfigurationError):
            client.get_capabilities("all")


def test_discovery_returns_only_configured_indexers_with_capabilities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["apikey"] == "secret-key"
        assert request.url.params["t"] == "indexers"
        assert request.url.params["configured"] == "true"
        return httpx.Response(200, content=fixture("indexers.xml"))

    with client_for(handler) as client:
        values = client.discover_indexers()
    assert [value.id for value in values] == ["alpha"]
    assert values[0].name == "Alpha Indexer"
    assert values[0].search_mode == "tvsearch"
    assert values[0].supports_tv_category


def test_explicit_capabilities_parse_namespaced_structure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=fixture("caps.xml"))

    with client_for(handler) as client:
        value = client.get_capabilities("alpha")
    assert value.tvsearch_available
    assert value.search_available
    assert [category.id for category in value.categories] == [5000, 5040, 5070]


def test_search_uses_tvsearch_tv_category_and_cache_bypass() -> None:
    indexer = IndexerCapabilities(
        "alpha",
        "Alpha",
        True,
        True,
        (TorznabCategory(5000, "TV"),),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["t"] == "tvsearch"
        assert request.url.params["q"] == "The Good Place Complete Series"
        assert request.url.params["cat"] == "5000"
        assert request.url.params["cache"] == "false"
        return httpx.Response(200, content=fixture("results.xml"))

    with client_for(handler) as client:
        values = client.search(
            indexer,
            "The Good Place Complete Series",
            fresh=True,
            sequence_start=10,
        )
    assert len(values) == 2
    assert values[0].sequence == 10
    assert values[0].seeders == 20
    assert values[0].leechers == 2
    assert values[0].infohash == "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
    assert values[0].size_bytes == 1234567890
    assert {category.id for category in values[0].categories} == {5000, 5040}
    assert "apikey" not in (values[0].download_url or "").casefold()
    assert "super-secret" not in (values[0].guid or "")
    assert values[1].seeders is None


def test_missing_optional_fields_and_link_only_result_do_not_crash() -> None:
    xml = b"""<rss><channel><item>
      <title>Example.Show.Complete.Series</title>
      <guid>opaque-guid</guid>
      <link>https://jackett.test/dl?id=1&amp;apikey=secret</link>
    </item></channel></rss>"""
    indexer = IndexerCapabilities("alpha", "Alpha", False, True, ())
    values = parse_torznab_results(xml, indexer=indexer, query="query")
    assert len(values) == 1
    assert values[0].seeders is None
    assert values[0].infohash is None
    assert values[0].download_url == "https://jackett.test/dl?id=1"


def test_malformed_xml_and_entity_declarations_are_rejected() -> None:
    indexer = IndexerCapabilities("alpha", "Alpha", False, True, ())
    with pytest.raises(JackettResponseError):
        parse_torznab_results(b"<rss>", indexer=indexer, query="query")
    with pytest.raises(JackettResponseError):
        parse_torznab_results(
            b'<!DOCTYPE rss [<!ENTITY x "bad">]><rss />',
            indexer=indexer,
            query="query",
        )


def test_429_retries_with_bounded_retry_after_then_succeeds() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, content=fixture("caps.xml"))

    with client_for(handler, sleeps=sleeps) as client:
        assert client.get_capabilities("alpha").search_available
    assert calls == 3
    assert sleeps == [30.0, 30.0]


def test_temporary_5xx_retries_and_ordinary_4xx_does_not() -> None:
    calls = 0

    def temporary(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(503) if calls < 4 else httpx.Response(200, content=fixture("caps.xml"))
        )

    with client_for(temporary) as client:
        assert client.get_capabilities("alpha").search_available
    assert calls == 4

    calls = 0

    def ordinary(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    with client_for(ordinary) as client, pytest.raises(JackettApiError):
        client.get_capabilities("alpha")
    assert calls == 1


def test_authentication_and_exhausted_timeout_map_to_safe_errors() -> None:
    def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with client_for(unauthorized) as client, pytest.raises(JackettAuthenticationError):
        client.get_capabilities("alpha")

    calls = 0

    def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("secret URL should not escape", request=request)

    with client_for(timeout) as client, pytest.raises(JackettNetworkError) as caught:
        client.get_capabilities("alpha")
    assert calls == 4
    assert "secret" not in str(caught.value)
