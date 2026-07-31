"""Command-line JSON, logging, files, and exit-code tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from media_scope.cli import main


class CliClient:
    """Context-managed CLI client double."""

    def __init__(
        self,
        token: str,
        *,
        movie: dict[str, Any],
        tv: dict[str, Any],
        seasons: dict[int, dict[str, Any]],
    ) -> None:
        self.token = token
        self.movie = movie
        self.tv = tv
        self.seasons = seasons

    def __enter__(self) -> CliClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get_movie(self, _tmdb_id: int) -> dict[str, Any]:
        return deepcopy(self.movie)

    def get_tv(self, _tmdb_id: int) -> dict[str, Any]:
        return deepcopy(self.tv)

    def get_tv_season(self, _series_id: int, season_number: int) -> dict[str, Any]:
        return deepcopy(self.seasons[season_number])

    def search_movies(self, _title: str, _year: int | None = None) -> list[dict[str, Any]]:
        return [
            {
                "id": self.movie["id"],
                "title": self.movie["title"],
                "original_title": self.movie["original_title"],
                "release_date": self.movie["release_date"],
                "overview": "Overview",
            }
        ]

    def search_tv(self, _title: str, _year: int | None = None) -> list[dict[str, Any]]:
        return [
            {
                "id": self.tv["id"],
                "name": self.tv["name"],
                "original_name": self.tv["original_name"],
                "first_air_date": self.tv["first_air_date"],
                "overview": "Overview",
            }
        ]


def make_factory(load_fixture: Any) -> Any:
    movie = load_fixture("movie_released.json")
    tv = load_fixture("tv_ended.json")
    seasons = {
        1: load_fixture("tv_season_1.json"),
        2: load_fixture("tv_season_2.json"),
    }
    return lambda token: CliClient(token, movie=movie, tv=tv, seasons=seasons)


def make_returning_factory(load_fixture: Any) -> Any:
    movie = load_fixture("movie_released.json")
    tv = load_fixture("tv_returning.json")
    seasons = {
        1: load_fixture("tv_season_1.json"),
        2: load_fixture("tv_season_2.json"),
        3: load_fixture("tv_season_3_future.json"),
    }
    return lambda token: CliClient(token, movie=movie, tv=tv, seasons=seasons)


def test_missing_token_is_structured_api_failure(
    monkeypatch: Any, capsys: Any, load_fixture: Any
) -> None:
    monkeypatch.delenv("TMDB_BEARER_TOKEN", raising=False)
    monkeypatch.setattr("media_scope.cli.load_dotenv", lambda: False)

    exit_code = main(["movie", "--tmdb-id", "1091"], client_factory=make_factory(load_fixture))

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 4
    assert payload["error_code"] == "TMDB_AUTHENTICATION_ERROR"
    assert captured.err == ""


def test_valid_json_stdout_and_verbose_logging_are_separate(
    monkeypatch: Any, capsys: Any, load_fixture: Any
) -> None:
    monkeypatch.setenv("TMDB_BEARER_TOKEN", "test-token")

    exit_code = main(
        ["movie", "The Thing", "--year", "1982", "--verbose"],
        client_factory=make_factory(load_fixture),
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["eligible"] is True
    assert "Resolving movie" in captured.err
    assert "INFO" not in captured.out


def test_output_file_exactly_matches_stdout(
    monkeypatch: Any, capsys: Any, load_fixture: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("TMDB_BEARER_TOKEN", "test-token")
    output = tmp_path / "result.json"

    exit_code = main(
        ["movie", "--tmdb-id", "1091", "--pretty", "--output", str(output)],
        client_factory=make_factory(load_fixture),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert output.read_text(encoding="utf-8") == captured.out
    assert captured.out.endswith("\n")


def test_ineligible_exit_code(monkeypatch: Any, capsys: Any, load_fixture: Any) -> None:
    monkeypatch.setenv("TMDB_BEARER_TOKEN", "test-token")
    factory = make_factory(load_fixture)
    original_factory = factory

    def ineligible_factory(token: str) -> CliClient:
        client = original_factory(token)
        client.movie["status"] = "Planned"
        return client

    exit_code = main(["movie", "--tmdb-id", "999"], client_factory=ineligible_factory)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert payload["ineligibility_code"] == "MOVIE_NOT_RELEASED"


def test_invalid_cli_is_json_and_exit_two(capsys: Any) -> None:
    exit_code = main(["movie", "--year", "99"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error_code"] == "INVALID_CLI_INPUT"


def test_tv_resolves_by_id(monkeypatch: Any, capsys: Any, load_fixture: Any) -> None:
    monkeypatch.setenv("TMDB_BEARER_TOKEN", "test-token")

    exit_code = main(["tv", "--tmdb-id", "66573"], client_factory=make_factory(load_fixture))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["scope"]["episode_count"] == 3


def test_output_write_failure_is_structured_exit_five(
    monkeypatch: Any, capsys: Any, load_fixture: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("TMDB_BEARER_TOKEN", "test-token")

    exit_code = main(
        ["movie", "--tmdb-id", "1091", "--output", str(tmp_path)],
        client_factory=make_factory(load_fixture),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 5
    assert payload["error_code"] == "OUTPUT_WRITE_ERROR"


def test_movie_rejects_latest_complete_season_flag(capsys: Any) -> None:
    exit_code = main(["movie", "The Thing", "--latest-complete-season"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error_code"] == "INVALID_CLI_INPUT"


def test_returning_tv_latest_complete_season_exits_zero(
    monkeypatch: Any, capsys: Any, load_fixture: Any
) -> None:
    monkeypatch.setenv("TMDB_BEARER_TOKEN", "test-token")

    exit_code = main(
        ["tv", "--tmdb-id", "456", "--latest-complete-season"],
        client_factory=make_returning_factory(load_fixture),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["scope_type"] == "latest_complete_season"
    assert payload["scope"]["seasons"][0]["season_number"] == 2


def test_returning_tv_latest_complete_season_resolves_by_title(
    monkeypatch: Any, capsys: Any, load_fixture: Any
) -> None:
    monkeypatch.setenv("TMDB_BEARER_TOKEN", "test-token")

    exit_code = main(
        [
            "tv",
            "Example Returning Show",
            "--year",
            "2016",
            "--latest-complete-season",
        ],
        client_factory=make_returning_factory(load_fixture),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["tmdb_id"] == 456


def test_ended_tv_latest_complete_season_exits_three(
    monkeypatch: Any, capsys: Any, load_fixture: Any
) -> None:
    monkeypatch.setenv("TMDB_BEARER_TOKEN", "test-token")

    exit_code = main(
        ["tv", "--tmdb-id", "66573", "--latest-complete-season"],
        client_factory=make_factory(load_fixture),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert payload["ineligibility_code"] == "TV_SERIES_NOT_RETURNING"
