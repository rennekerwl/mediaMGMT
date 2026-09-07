"""OpenRouter request, retry, and structured-output validation tests."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from media_scope.openrouter_client import OpenRouterClient, OpenRouterError

CandidateHandler = Callable[[httpx.Request], httpx.Response]


def _candidate(tmdb_id: int, title: str) -> dict[str, object]:
    return {
        "tmdb_id": tmdb_id,
        "title": title,
        "year": 2020,
        "overview": f"Synopsis for {title}",
        "genres": ["Drama"],
        "vote_average": 7.5,
        "vote_count": 1000,
    }


def _client(
    handler: CandidateHandler,
    *,
    sleeps: list[float] | None = None,
) -> OpenRouterClient:
    return OpenRouterClient(
        "test-api-key",
        "openai/test-model",
        transport=httpx.MockTransport(handler),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def _response(selection: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(selection)}}]},
    )


def test_configuration_requires_api_key_and_model() -> None:
    with pytest.raises(ValueError, match="API key"):
        OpenRouterClient("  ", "model")
    with pytest.raises(ValueError, match="model"):
        OpenRouterClient("key", "  ")


def test_select_sends_complete_taste_context_and_structured_schema() -> None:
    candidates = [_candidate(101, "One"), _candidate(202, "Two")]
    taste_history = [
        {"tmdb_id": 1, "rating": 5, "notes": "Loved the quiet character work."},
        {"tmdb_id": 2, "rating": 1, "notes": "Too much spectacle."},
        {"tmdb_id": 3, "rating": 3, "notes": "Fine, no strong feeling."},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == OpenRouterClient.BASE_URL
        assert request.headers["Authorization"] == "Bearer test-api-key"
        assert request.headers["Accept"] == "application/json"
        body = json.loads(request.content)
        assert body["model"] == "openai/test-model"
        assert body["provider"] == {"require_parameters": True}
        schema = body["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["recommendations"]["minItems"] == 2
        assert schema["properties"]["recommendations"]["maxItems"] == 2
        assert schema["properties"]["recommendations"]["items"]["required"] == [
            "tmdb_id",
            "reason",
        ]
        user_content = body["messages"][1]["content"]
        serialized = user_content.split("\n\n", 1)[1]
        assert json.loads(serialized) == {
            "candidates": candidates,
            "taste_history": taste_history,
            "requested_count": 2,
        }
        system_content = body["messages"][0]["content"].casefold()
        for phrase in (
            "negative evidence",
            "notes",
            "genre quotas",
            "seed recommendation lists",
            "metadata",
            "instructions",
        ):
            assert phrase in system_content
        return _response(
            {
                "recommendations": [
                    {"tmdb_id": 202, "reason": "The notes suggest this tone fits."},
                    {"tmdb_id": 101, "reason": "It matches the positive character evidence."},
                ]
            }
        )

    with _client(handler) as client:
        assert client.select(candidates=candidates, taste_history=taste_history, count=2) == [
            {"tmdb_id": 202, "reason": "The notes suggest this tone fits."},
            {"tmdb_id": 101, "reason": "It matches the positive character evidence."},
        ]


@pytest.mark.parametrize(
    "selection",
    [
        {"recommendations": [{"tmdb_id": 101, "reason": "one"}]},
        {
            "recommendations": [
                {"tmdb_id": 999, "reason": "unknown"},
                {"tmdb_id": 202, "reason": "known"},
            ]
        },
        {
            "recommendations": [
                {"tmdb_id": 101, "reason": "first"},
                {"tmdb_id": 101, "reason": "duplicate"},
            ]
        },
        {
            "recommendations": [
                {"tmdb_id": 101, "reason": ""},
                {"tmdb_id": 202, "reason": "known"},
            ]
        },
        {
            "recommendations": [
                {"tmdb_id": True, "reason": "boolean ID"},
                {"tmdb_id": 202, "reason": "known"},
            ]
        },
        {
            "recommendations": [
                {"tmdb_id": 101, "reason": "one", "extra": "field"},
                {"tmdb_id": 202, "reason": "known"},
            ]
        },
        {
            "recommendations": [
                {"tmdb_id": 101, "reason": "one"},
                {"tmdb_id": 202, "reason": "known"},
            ],
            "extra": "field",
        },
    ],
)
def test_select_rejects_invalid_structured_output(selection: object) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _response(selection)

    with _client(handler) as client, pytest.raises(OpenRouterError):
        client.select(
            candidates=[_candidate(101, "One"), _candidate(202, "Two")],
            taste_history=[],
            count=2,
        )


def test_select_rejects_malformed_content_without_exposing_response_body() -> None:
    secret_body = '{"secret":"do-not-log"}'

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": secret_body[:-1]}}]},
        )

    with _client(handler) as client, pytest.raises(OpenRouterError) as caught:
        client.select(candidates=[_candidate(101, "One")], taste_history=[], count=1)
    assert "do-not-log" not in str(caught.value)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_http_failure_retries_once(status: int) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status)
        return _response({"recommendations": [{"tmdb_id": 101, "reason": "fits"}]})

    with _client(handler, sleeps=sleeps) as client:
        result = client.select(candidates=[_candidate(101, "One")], taste_history=[], count=1)
    assert result == [{"tmdb_id": 101, "reason": "fits"}]
    assert calls == 2
    assert sleeps == [1.0]


def test_exhausted_transient_http_failure_retries_only_once() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, content=b"provider internals")

    with _client(handler, sleeps=sleeps) as client, pytest.raises(OpenRouterError) as caught:
        client.select(candidates=[_candidate(101, "One")], taste_history=[], count=1)
    assert calls == 2
    assert sleeps == [1.0]
    assert "provider internals" not in str(caught.value)


def test_transport_failure_retries_once_then_raises_sanitized_error() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("key=test-api-key", request=request)

    with _client(handler, sleeps=sleeps) as client, pytest.raises(OpenRouterError) as caught:
        client.select(candidates=[_candidate(101, "One")], taste_history=[], count=1)
    assert calls == 2
    assert sleeps == [1.0]
    assert "test-api-key" not in str(caught.value)


def test_non_retryable_http_failure_does_not_retry_or_expose_body() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, content=b"api-key=test-api-key")

    with _client(handler) as client, pytest.raises(OpenRouterError) as caught:
        client.select(candidates=[_candidate(101, "One")], taste_history=[], count=1)
    assert calls == 1
    assert "test-api-key" not in str(caught.value)


def test_select_validates_candidate_membership_before_network_call() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response({"recommendations": [{"tmdb_id": 101, "reason": "fits"}]})

    with _client(handler) as client, pytest.raises(OpenRouterError, match="Candidate"):
        client.select(candidates=[{"tmdb_id": "101"}], taste_history=[], count=1)  # type: ignore[list-item]
    assert calls == 0

    with _client(handler) as client, pytest.raises(ValueError, match="exceed"):
        client.select(candidates=[_candidate(101, "One")], taste_history=[], count=2)
    assert calls == 0
