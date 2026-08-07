"""One-shot movie recommendation CLI tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from media_scope.recommend_cli import main
from media_scope.recommendations import RecommendationInputError


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
            candidate(10, "First", 30),
            candidate(11, "Second", 20),
            candidate(12, "Third", 10),
        ]


def candidate(tmdb_id: int, title: str, popularity: float) -> dict[str, Any]:
    return {
        "id": tmdb_id,
        "title": title,
        "adult": False,
        "release_date": "2020-06-01",
        "popularity": popularity,
        "vote_average": 7.0,
    }


def configure(
    monkeypatch: pytest.MonkeyPatch,
    movies: Path,
    recommendations: Path | None = None,
) -> None:
    monkeypatch.setenv("MOVIES_DIRECTORY", str(movies))
    monkeypatch.setenv("RECOMMENDATIONS_DIRECTORY", str(recommendations or movies))
    monkeypatch.setenv("GOOGLE_SHEET_CSV_URL", "https://example.test/sheet.csv")
    monkeypatch.setenv("TMDB_BEARER_TOKEN", "test-token")
    monkeypatch.setattr("media_scope.recommend_cli.load_dotenv", lambda: False)


@pytest.mark.parametrize("existing_count", [0, 1, 2])
def test_writes_only_enough_recommendations_to_fill_three(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    existing_count: int,
) -> None:
    configure(monkeypatch, tmp_path)
    for index in range(existing_count):
        (tmp_path / f"Existing {index}.mkv").touch()

    exit_code = main(
        [],
        client_factory=CliRecommendationClient,
        csv_fetcher=lambda _url: "Title,Rating\nSeed,5\n",
        today=date(2030, 1, 1),
    )

    lines = (tmp_path / "RECOMMENDATIONS.txt").read_text(encoding="utf-8").splitlines()
    assert exit_code == 0
    assert lines == ["First (2020)", "Second (2020)", "Third (2020)"][: 3 - existing_count]


def test_full_folder_skips_network_and_preserves_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch, tmp_path)
    for index in range(3):
        (tmp_path / f"Existing {index}").mkdir()
    output = tmp_path / "RECOMMENDATIONS.txt"
    output.write_text("old contents\n", encoding="utf-8")

    def unexpected_fetch(_url: str) -> str:
        raise AssertionError("CSV should not be fetched")

    def unexpected_factory(_token: str) -> CliRecommendationClient:
        raise AssertionError("TMDb client should not be created")

    exit_code = main([], client_factory=unexpected_factory, csv_fetcher=unexpected_fetch)

    assert exit_code == 0
    assert output.read_text(encoding="utf-8") == "old contents\n"


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
        csv_fetcher=lambda _url: "Title,Rating\nSeed,5\n",
        today=date(2030, 1, 1),
    )

    assert exit_code == 0
    assert not (movies / "RECOMMENDATIONS.txt").exists()
    assert (tmp_path / "RECOMMENDATIONS.txt").read_text(encoding="utf-8") == (
        "First (2020)\nSecond (2020)\n"
    )


def test_short_result_is_written_with_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    configure(monkeypatch, tmp_path)

    class OneResultClient(CliRecommendationClient):
        def get_movie_recommendations(self, _tmdb_id: int) -> list[dict[str, Any]]:
            return [candidate(10, "Only One", 10)]

    exit_code = main(
        [],
        client_factory=OneResultClient,
        csv_fetcher=lambda _url: "Title,Rating\nSeed,5\n",
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

    def failed_fetch(_url: str) -> str:
        raise RecommendationInputError("download failed")

    exit_code = main([], client_factory=CliRecommendationClient, csv_fetcher=failed_fetch)

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
        csv_fetcher=lambda _url: "Title,Rating\nSeed,5\n",
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
