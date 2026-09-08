"""End-to-end tests for preview selection through the real LLM recommendation flow."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from media_scope.openrouter_client import OpenRouterClient
from media_scope.recommend_cli import main

SHEET_ROWS = [
    ["Title", "Year", "Rating", "Notes", "TMDb ID"],
    ["Loved One", 2010, 5, "Loved the quiet character work.", 10],
    ["Liked Two", 2011, 4, "Enjoyed the dry humor.", 20],
    ["Disliked", 2012, 1, "Too much spectacle and noise.", 30],
]


def _candidate(tmdb_id: int, title: str, *, genre_id: int = 18) -> dict[str, Any]:
    return {
        "id": tmdb_id,
        "title": title,
        "adult": False,
        "video": False,
        "genre_ids": [genre_id],
        "release_date": "2020-06-01",
        "overview": f"Recommendation overview for {title}.",
        "popularity": float(tmdb_id),
        "vote_average": 7.5,
        "vote_count": 1000,
    }


class FakeSheetClient:
    def read_movie_rows(self) -> list[list[object]]:
        return SHEET_ROWS


class FakeTmdbClient:
    def __init__(self, token: str) -> None:
        assert token == "tmdb-test-token"
        self.recommendation_calls: list[int] = []
        self.detail_calls: list[int] = []

    def __enter__(self) -> FakeTmdbClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def search_movies(self, _title: str, _year: int | None = None) -> list[dict[str, Any]]:
        raise AssertionError("all fixture ratings already have TMDb IDs")

    def get_movie_recommendations(self, tmdb_id: int) -> list[dict[str, Any]]:
        self.recommendation_calls.append(tmdb_id)
        return {
            10: [
                _candidate(101, "First Candidate"),
                _candidate(102, "Second Candidate", genre_id=35),
            ],
            20: [
                _candidate(102, "Duplicate Candidate", genre_id=35),
                _candidate(103, "Third Candidate", genre_id=53),
            ],
            30: [],
        }[tmdb_id]

    def discover_movies(
        self,
        *,
        page: int,
        released_through: date,
        min_vote_average: float,
        min_vote_count: int,
    ) -> list[dict[str, Any]]:
        assert released_through == date(2030, 1, 1)
        assert min_vote_average == 7.0
        assert min_vote_count == 500
        if page == 1:
            return [_candidate(104, "Discovery Candidate", genre_id=10749)]
        return []

    def get_movie(self, tmdb_id: int) -> dict[str, Any]:
        self.detail_calls.append(tmdb_id)
        return {
            "id": tmdb_id,
            "overview": f"Trusted TMDb overview for {tmdb_id}.",
            "genres": [{"id": 18, "name": "Drama"}],
            "popularity": float(tmdb_id) + 0.5,
            "vote_average": 8.0,
            "vote_count": 1200,
        }


def _configure_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMDB_BEARER_TOKEN", "tmdb-test-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/llm-model")
    monkeypatch.delenv("MOVIES_DIRECTORY", raising=False)
    monkeypatch.delenv("RECOMMENDATIONS_DIRECTORY", raising=False)
    monkeypatch.delenv("RECOMMENDATION_ENGINE", raising=False)
    monkeypatch.setattr("media_scope.recommend_cli.load_dotenv", lambda: False)


def _selector_factory(
    handler: Any,
) -> Any:
    def factory(api_key: str, model: str) -> OpenRouterClient:
        assert api_key == "openrouter-test-key"
        assert model == "test/llm-model"
        return OpenRouterClient(
            api_key,
            model,
            transport=httpx.MockTransport(handler),
            sleep=lambda _delay: None,
        )

    return factory


def test_preview_runs_real_llm_flow_and_reconstructs_trusted_picks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _configure_preview(monkeypatch)
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        data = json.loads(body["messages"][1]["content"].split("\n\n", 1)[1])
        assert [item["tmdb_id"] for item in data["candidates"]] == [101, 102, 103, 104]
        history_by_id = {item["tmdb_id"]: item for item in data["taste_history"]}
        assert history_by_id[10]["notes"] == "Loved the quiet character work."
        assert history_by_id[20]["notes"] == "Enjoyed the dry humor."
        assert history_by_id[30]["rating"] == 1
        assert history_by_id[30]["notes"] == "Too much spectacle and noise."
        assert data["requested_count"] == 3
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "recommendations": [
                                        {"tmdb_id": 103, "reason": "Fits the grounded tone."},
                                        {"tmdb_id": 102, "reason": "Matches the humor note."},
                                        {"tmdb_id": 104, "reason": "Adds a promising new angle."},
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    tmdb_holder: list[FakeTmdbClient] = []

    def tmdb_factory(token: str) -> FakeTmdbClient:
        client = FakeTmdbClient(token)
        tmdb_holder.append(client)
        return client

    exit_code = main(
        ["--preview"],
        client_factory=tmdb_factory,
        sheet_client_factory=FakeSheetClient,
        selector_client_factory=_selector_factory(handler),
        today=date(2030, 1, 1),
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload == {
        "schema_version": 1,
        "result": "recommendations_preview",
        "picks": [
            {"tmdb_id": 103, "title": "Third Candidate", "year": 2020},
            {"tmdb_id": 102, "title": "Second Candidate", "year": 2020},
            {"tmdb_id": 104, "title": "Discovery Candidate", "year": 2020},
        ],
        "reasons": {
            "102": "Matches the humor note.",
            "103": "Fits the grounded tone.",
            "104": "Adds a promising new angle.",
        },
        "model": "test/llm-model",
        "candidate_count": 4,
        "method": "llm",
        "fallback": False,
    }
    assert len(requests) == 1
    assert tmdb_holder[0].recommendation_calls == [10, 20]
    assert tmdb_holder[0].detail_calls == [10, 20, 30]
    assert not (tmp_path / "RECOMMENDATIONS.txt").exists()


@pytest.mark.parametrize("status", [400, 401])
def test_preview_uses_deterministic_fallback_after_openrouter_auth_or_context_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    status: int,
) -> None:
    _configure_preview(monkeypatch)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, content=b"provider context details and api key")

    exit_code = main(
        ["--preview"],
        client_factory=FakeTmdbClient,
        sheet_client_factory=FakeSheetClient,
        selector_client_factory=_selector_factory(handler),
        today=date(2030, 1, 1),
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["method"] == "fallback"
    assert payload["fallback"] is True
    assert payload["candidate_count"] == 4
    assert payload["picks"] == [
        {"tmdb_id": 101, "title": "First Candidate", "year": 2020},
        {"tmdb_id": 102, "title": "Second Candidate", "year": 2020},
        {"tmdb_id": 104, "title": "Discovery Candidate", "year": 2020},
    ]
    assert payload["reasons"] == {
        "101": "TMDb recommendations for rated seed TMDb ID 10.",
        "102": "TMDb recommendations for rated seed TMDb ID 10.",
        "104": "TMDb discovery.",
    }
    assert calls == 1
    assert "api key" not in captured.err.casefold()
    assert not (tmp_path / "RECOMMENDATIONS.txt").exists()
