"""One-shot movie recommendation CLI tests."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from media_scope.movie_search import MovieSearchInputError, load_recommendations
from media_scope.recommend_cli import main
from media_scope.recommendations import Recommendation, RecommendationInputError

SHEET_ROWS = [
    ["Title", "Year", "Rating", "Notes", "TMDb ID"],
    ["Seed", "", 5, "", ""],
]


class FakeSheetClient:
    def __init__(self, rows: list[list[object]] | None = None) -> None:
        self.rows = rows or SHEET_ROWS

    def read_movie_rows(self) -> list[list[object]]:
        return self.rows


class CliRecommendationClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def __enter__(self) -> CliRecommendationClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def search_movies(self, _title: str, _year: int | None = None) -> list[dict[str, Any]]:
        return [{"id": 1, "popularity": 10}]

    def get_movie_recommendations(self, _tmdb_id: int) -> list[dict[str, Any]]:
        return [
            candidate(10, "First", 30, genre_ids=[28, 12]),
            candidate(11, "Second", 20, genre_ids=[35]),
        ]

    def discover_movies(
        self,
        *,
        page: int,
        released_through: date,
        min_vote_average: float,
        min_vote_count: int,
    ) -> list[dict[str, Any]]:
        return [candidate(12, "Explore", 10, genre_ids=[18])] if page == 1 else []


def candidate(
    tmdb_id: int,
    title: str,
    popularity: float,
    *,
    genre_ids: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "id": tmdb_id,
        "title": title,
        "adult": False,
        "video": False,
        "genre_ids": genre_ids or [18],
        "release_date": "2020-06-01",
        "popularity": popularity,
        "vote_average": 7.0,
        "vote_count": 1000,
    }


def configure(
    monkeypatch: pytest.MonkeyPatch,
    movies: Path,
    recommendations: Path | None = None,
) -> None:
    monkeypatch.setenv("MOVIES_DIRECTORY", str(movies))
    monkeypatch.setenv("RECOMMENDATIONS_DIRECTORY", str(recommendations or movies))
    monkeypatch.setenv("TMDB_BEARER_TOKEN", "test-token")
    monkeypatch.delenv("RECOMMENDATION_ENGINE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setattr("media_scope.recommend_cli.load_dotenv", lambda: False)


def llm_selection() -> SimpleNamespace:
    recommendation = Recommendation(
        tmdb_id=10,
        title="LLM Pick",
        year=2020,
        weighted_support=1.5,
        genre_ids=frozenset({18}),
        popularity=20.0,
        vote_average=7.5,
        vote_count=1000,
    )
    return SimpleNamespace(
        recommendations=[recommendation],
        reasons={10: "Matches the user's stated preference."},
        method="llm",
        candidate_count=4,
    )


class SelectorClient:
    calls: list[tuple[str, str]] = []
    entered = 0
    exited = 0

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        type(self).calls.append((api_key, model))

    def __enter__(self) -> SelectorClient:
        type(self).entered += 1
        return self

    def __exit__(self, *_args: object) -> None:
        type(self).exited += 1


@pytest.mark.parametrize("existing_count", [0, 1, 2])
def test_always_writes_three_recommendations_below_trigger(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    existing_count: int,
) -> None:
    configure(monkeypatch, tmp_path)
    for index in range(existing_count):
        (tmp_path / f"Existing {index}.mkv").touch()

    exit_code = main(
        [],
        client_factory=CliRecommendationClient,
        sheet_client_factory=FakeSheetClient,
        today=date(2030, 1, 1),
    )

    lines = (tmp_path / "RECOMMENDATIONS.txt").read_text(encoding="utf-8").splitlines()
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert lines == ["First (2020)", "Second (2020)", "Explore (2020)"]
    assert payload == {
        "schema_version": 1,
        "result": "recommendations_created",
        "recommendations": [
            {"tmdb_id": 10, "title": "First", "year": 2020},
            {"tmdb_id": 11, "title": "Second", "year": 2020},
            {"tmdb_id": 12, "title": "Explore", "year": 2020},
        ],
    }


def test_full_folder_skips_network_and_preserves_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    configure(monkeypatch, tmp_path)
    for index in range(3):
        (tmp_path / f"Existing {index}").mkdir()
    output = tmp_path / "RECOMMENDATIONS.txt"
    output.write_text("old contents\n", encoding="utf-8")

    def unexpected_sheet() -> FakeSheetClient:
        raise AssertionError("Google Sheets should not be read")

    def unexpected_factory(_token: str) -> CliRecommendationClient:
        raise AssertionError("TMDb client should not be created")

    exit_code = main([], client_factory=unexpected_factory, sheet_client_factory=unexpected_sheet)

    assert exit_code == 0
    assert output.read_text(encoding="utf-8") == "old contents\n"
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "result": "recommendations_not_needed",
        "recommendations": [],
    }


def test_counts_movies_and_writes_recommendations_in_separate_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    movies = tmp_path / "movies"
    movies.mkdir()
    configure(monkeypatch, movies, tmp_path)
    (movies / "Existing.mkv").touch()

    exit_code = main(
        [],
        client_factory=CliRecommendationClient,
        sheet_client_factory=FakeSheetClient,
        today=date(2030, 1, 1),
    )

    assert exit_code == 0
    assert not (movies / "RECOMMENDATIONS.txt").exists()
    assert (tmp_path / "RECOMMENDATIONS.txt").read_text(encoding="utf-8") == (
        "First (2020)\nSecond (2020)\nExplore (2020)\n"
    )


def test_short_result_is_written_with_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    configure(monkeypatch, tmp_path)

    class OneResultClient(CliRecommendationClient):
        def get_movie_recommendations(self, _tmdb_id: int) -> list[dict[str, Any]]:
            return [candidate(10, "Only One", 10)]

        def discover_movies(
            self,
            *,
            page: int,
            released_through: date,
            min_vote_average: float,
            min_vote_count: int,
        ) -> list[dict[str, Any]]:
            return []

    exit_code = main(
        [],
        client_factory=OneResultClient,
        sheet_client_factory=FakeSheetClient,
        today=date(2030, 1, 1),
    )

    assert exit_code == 0
    assert (tmp_path / "RECOMMENDATIONS.txt").read_text(encoding="utf-8") == ("Only One (2020)\n")
    assert "only 1 of the 3" in capsys.readouterr().err


def test_input_failure_preserves_existing_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch, tmp_path)
    output = tmp_path / "RECOMMENDATIONS.txt"
    output.write_text("keep me\n", encoding="utf-8")

    class FailedSheetClient(FakeSheetClient):
        def read_movie_rows(self) -> list[list[object]]:
            raise RecommendationInputError("read failed")

    exit_code = main(
        [], client_factory=CliRecommendationClient, sheet_client_factory=FailedSheetClient
    )

    assert exit_code != 0
    assert output.read_text(encoding="utf-8") == "keep me\n"


def test_output_write_failure_preserves_existing_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch, tmp_path)
    output = tmp_path / "RECOMMENDATIONS.txt"
    output.write_text("keep me\n", encoding="utf-8")

    def failed_temporary_file(**_kwargs: object) -> object:
        raise OSError("write failed")

    monkeypatch.setattr(
        "media_scope.recommend_cli.tempfile.NamedTemporaryFile", failed_temporary_file
    )

    exit_code = main(
        [],
        client_factory=CliRecommendationClient,
        sheet_client_factory=FakeSheetClient,
        today=date(2030, 1, 1),
    )

    assert exit_code == 5
    assert output.read_text(encoding="utf-8") == "keep me\n"


def test_missing_movies_directory_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MOVIES_DIRECTORY", raising=False)
    monkeypatch.setattr("media_scope.recommend_cli.load_dotenv", lambda: False)

    assert main([]) == 2


@pytest.mark.parametrize("folder_count", [None, 4])
def test_preview_uses_llm_without_folder_checks_or_file_writes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    folder_count: int | None,
) -> None:
    configure(monkeypatch, tmp_path)
    if folder_count is None:
        monkeypatch.delenv("MOVIES_DIRECTORY", raising=False)
    else:
        movies = tmp_path / "movies"
        movies.mkdir()
        for index in range(folder_count):
            (movies / f"Existing {index}").mkdir()
        monkeypatch.setenv("MOVIES_DIRECTORY", str(movies))
    monkeypatch.delenv("RECOMMENDATIONS_DIRECTORY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "preview-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "preview-model")
    monkeypatch.setattr(
        "media_scope.recommend_cli.build_llm_recommendations",
        lambda *_args, **_kwargs: llm_selection(),
    )

    SelectorClient.calls = []
    SelectorClient.entered = 0
    SelectorClient.exited = 0
    exit_code = main(
        ["--preview"],
        client_factory=CliRecommendationClient,
        sheet_client_factory=FakeSheetClient,
        selector_client_factory=SelectorClient,
        today=date(2030, 1, 1),
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload == {
        "schema_version": 1,
        "result": "recommendations_preview",
        "picks": [{"tmdb_id": 10, "title": "LLM Pick", "year": 2020}],
        "reasons": {"10": "Matches the user's stated preference."},
        "model": "preview-model",
        "candidate_count": 4,
        "method": "llm",
        "fallback": False,
    }
    assert "recommendations" not in payload
    with pytest.raises(MovieSearchInputError):
        load_recommendations(captured.out)
    assert not (tmp_path / "RECOMMENDATIONS.txt").exists()
    assert SelectorClient.calls == [("preview-key", "preview-model")]
    assert SelectorClient.entered == SelectorClient.exited == 1


@pytest.mark.parametrize("missing", ["OPENROUTER_API_KEY", "OPENROUTER_MODEL"])
def test_preview_requires_openrouter_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    missing: str,
) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.delenv("MOVIES_DIRECTORY", raising=False)
    monkeypatch.delenv("RECOMMENDATIONS_DIRECTORY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "configured-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "configured-model")
    monkeypatch.delenv(missing, raising=False)

    assert main(["--preview"]) == 2
    assert "OPENROUTER_" in capsys.readouterr().err


def test_llm_engine_preserves_production_handoff_and_logs_reasons(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("RECOMMENDATION_ENGINE", "llm")
    monkeypatch.setenv("OPENROUTER_API_KEY", "configured-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "configured-model")
    monkeypatch.setattr(
        "media_scope.recommend_cli.build_llm_recommendations",
        lambda *_args, **_kwargs: llm_selection(),
    )

    exit_code = main(
        ["--verbose"],
        client_factory=CliRecommendationClient,
        sheet_client_factory=FakeSheetClient,
        selector_client_factory=SelectorClient,
        today=date(2030, 1, 1),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == (
        '{"schema_version":1,"result":"recommendations_created",'
        '"recommendations":[{"tmdb_id":10,"title":"LLM Pick","year":2020}]}\n'
    )
    assert "recommendation_method=llm" in captured.err
    assert "recommendation_reason tmdb_id=10" in captured.err
    assert "Matches the user's stated preference." in captured.err
    assert "configured-key" not in captured.err


def test_legacy_engine_is_default_and_does_not_create_selector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "configured-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "configured-model")

    def unexpected_selector(_api_key: str, _model: str) -> SelectorClient:
        raise AssertionError("legacy is the default engine")

    exit_code = main(
        [],
        client_factory=CliRecommendationClient,
        sheet_client_factory=FakeSheetClient,
        selector_client_factory=unexpected_selector,
        today=date(2030, 1, 1),
    )

    assert exit_code == 0
    assert (tmp_path / "RECOMMENDATIONS.txt").exists()


def test_full_folder_skips_llm_credentials_and_all_clients(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("RECOMMENDATION_ENGINE", "llm")
    monkeypatch.delenv("TMDB_BEARER_TOKEN", raising=False)
    for index in range(3):
        (tmp_path / f"Movie {index}.mkv").touch()
    output = tmp_path / "RECOMMENDATIONS.txt"
    output.write_text("existing recommendations\n", encoding="utf-8")

    def unexpected_client(*_args: object) -> Any:
        raise AssertionError("No clients should be created for a full folder")

    assert (
        main(
            [],
            client_factory=unexpected_client,
            sheet_client_factory=unexpected_client,
            selector_client_factory=unexpected_client,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["result"] == "recommendations_not_needed"
    assert output.read_text(encoding="utf-8") == "existing recommendations\n"


def test_preview_preserves_existing_production_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "preview-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "preview-model")
    monkeypatch.setattr(
        "media_scope.recommend_cli.build_llm_recommendations",
        lambda *_args, **_kwargs: llm_selection(),
    )
    output = tmp_path / "RECOMMENDATIONS.txt"
    original = b"existing recommendations\r\n"
    output.write_bytes(original)
    before = set(tmp_path.iterdir())

    assert (
        main(
            ["--preview"],
            client_factory=CliRecommendationClient,
            sheet_client_factory=FakeSheetClient,
            selector_client_factory=SelectorClient,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["result"] == "recommendations_preview"
    assert output.read_bytes() == original
    assert set(tmp_path.iterdir()) == before
