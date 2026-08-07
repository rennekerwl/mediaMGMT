"""TMDb client headers, retries, and error mapping tests."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from media_scope.client import TmdbClient
from media_scope.exceptions import (
    AuthenticationError,
    InvalidResponseError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    TmdbApiError,
)


def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleeps: list[float] | None = None,
) -> TmdbClient:
    recorded_sleeps = sleeps if sleeps is not None else []
    return TmdbClient(
        "secret-token",
        transport=httpx.MockTransport(handler),
        sleep=recorded_sleeps.append,
    )


def test_headers_and_movie_search_year_parameter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert request.headers["Accept"] == "application/json"
        assert request.url.params["query"] == "The Thing"
        assert request.url.params["primary_release_year"] == "1982"
        return httpx.Response(200, json={"results": []})

    with client_for(handler) as client:
        assert client.search_movies("The Thing", 1982) == []


def test_tv_search_uses_first_air_date_year_parameter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/search/tv")
        assert request.url.params["first_air_date_year"] == "2016"
        assert "primary_release_year" not in request.url.params
        return httpx.Response(200, json={"results": []})

    with client_for(handler) as client:
        assert client.search_tv("The Good Place", 2016) == []


def test_movie_recommendations_use_first_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/movie/1091/recommendations")
        assert request.url.params["page"] == "1"
        return httpx.Response(200, json={"results": [{"id": 2}]})

    with client_for(handler) as client:
        assert client.get_movie_recommendations(1091) == [{"id": 2}]


def test_malformed_movie_recommendations_raise_invalid_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": "not-a-list"})

    with client_for(handler) as client, pytest.raises(InvalidResponseError):
        client.get_movie_recommendations(1091)


@pytest.mark.parametrize(
    ("status", "exception_type"),
    [(401, AuthenticationError), (404, NotFoundError), (400, TmdbApiError)],
)
def test_non_retryable_http_errors(status: int, exception_type: type[Exception]) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"status_message": "failure"})

    with client_for(handler) as client, pytest.raises(exception_type):
        client.get_movie(1)
    assert calls == 1


def test_429_retries_then_succeeds() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"id": 1})

    with client_for(handler, sleeps=sleeps) as client:
        assert client.get_movie(1) == {"id": 1}
    assert calls == 3
    assert sleeps == [1.0, 1.0]


def test_exhausted_429_raises_rate_limit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with client_for(handler) as client, pytest.raises(RateLimitError):
        client.get_movie(1)


def test_5xx_retries_then_succeeds() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 4:
            return httpx.Response(503)
        return httpx.Response(200, json={"id": 1})

    with client_for(handler) as client:
        assert client.get_movie(1) == {"id": 1}
    assert calls == 4


def test_network_timeout_retries_then_raises() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    with client_for(handler) as client, pytest.raises(NetworkError):
        client.get_movie(1)
    assert calls == 4


def test_malformed_json_raises_invalid_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not-json")

    with client_for(handler) as client, pytest.raises(InvalidResponseError):
        client.get_movie(1)


def test_non_object_json_raises_invalid_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with client_for(handler) as client, pytest.raises(InvalidResponseError):
        client.get_movie(1)
