"""Movie recommendation handoff, Jackett filtering, ranking, and CLI tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from media_scope.exceptions import JackettNetworkError
from media_scope.movie_search import (
    MovieRecommendation,
    MovieSearchInputError,
    format_jackett_results,
    load_recommendations,
    main,
    search_recommended_movies,
)
from media_scope.release_classifier import normalize_release_title
from media_scope.search_models import IndexerCapabilities, RawRelease, TorznabCategory


def capability(indexer_id: str) -> IndexerCapabilities:
    return IndexerCapabilities(
        indexer_id,
        indexer_id.title(),
        False,
        True,
        (TorznabCategory(2000, "Movies"),),
    )


def release(
    sequence: int,
    title: str,
    *,
    indexer_id: str = "alpha",
    seeders: int | None = 1,
    peers: int | None = 2,
    infohash: str | None = None,
    size: int | None = 1_000,
    download_url: str | None = None,
    guid: str | None = None,
    published_at: str = "2026-01-01T00:00:00Z",
) -> RawRelease:
    return RawRelease(
        sequence=sequence,
        indexer_id=indexer_id,
        indexer_name=indexer_id.title(),
        query="The Thing 1982",
        original_title=title,
        normalized_title=normalize_release_title(title),
        guid=guid,
        download_url=download_url,
        published_at=published_at,
        categories=(TorznabCategory(2000, "Movies"),),
        size_bytes=size,
        seeders=seeders,
        peers=peers,
        infohash=infohash,
        magnet_uri=f"magnet:?xt=urn:btih:{infohash}" if infohash else None,
    )


class FakeMovieClient:
    def __init__(
        self,
        values: dict[tuple[str, str], list[RawRelease] | Exception],
        *,
        indexers: list[IndexerCapabilities] | None = None,
    ) -> None:
        self.values = values
        self.indexers = indexers or [capability("alpha")]
        self.calls: list[tuple[str, str, bool, int]] = []

    def __enter__(self) -> FakeMovieClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def discover_indexers(self) -> list[IndexerCapabilities]:
        return self.indexers

    def get_capabilities(self, indexer_id: str) -> IndexerCapabilities:
        return next(value for value in self.indexers if value.id == indexer_id)

    def search_movies(
        self,
        indexer: IndexerCapabilities,
        query: str,
        *,
        fresh: bool,
        sequence_start: int,
    ) -> list[RawRelease]:
        self.calls.append((indexer.id, query, fresh, sequence_start))
        value = self.values.get((indexer.id, query), [])
        if isinstance(value, Exception):
            raise value
        return value


def input_payload(*movies: dict[str, object]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "result": "recommendations_created",
            "recommendations": list(movies),
        }
    )


def test_load_recommendations_validates_machine_handoff() -> None:
    values = load_recommendations(
        input_payload({"tmdb_id": 1091, "title": "The Thing", "year": 1982})
    )
    assert values == [MovieRecommendation(1091, "The Thing", 1982)]

    for text in (
        "",
        "not json",
        "[]",
        json.dumps({"schema_version": 2, "recommendations": []}),
        input_payload({"tmdb_id": 1, "title": "The Thing", "year": None}),
    ):
        with pytest.raises(MovieSearchInputError):
            load_recommendations(text)


def test_filters_mismatches_seedless_and_unusable_results_then_ranks() -> None:
    recommendation = MovieRecommendation(1091, "The Thing", 1982)
    raw = [
        release(0, "The.Thing.1982.1080p", seeders=5, infohash="a" * 40),
        release(1, "Group The Thing 1982 REMUX", seeders=50, infohash="b" * 40),
        release(2, "The Thing 2011 1080p", seeders=100, infohash="c" * 40),
        release(3, "The Thing 1982 WEB-DL", seeders=0, infohash="d" * 40),
        release(4, "The Thing 1982 BluRay", seeders=None, infohash="e" * 40),
        release(5, "The Thing 1982 DVDRip", seeders=20, infohash=None, size=None),
    ]
    client = FakeMovieClient({("alpha", "The Thing 1982"): raw})

    payload = search_recommended_movies(
        client,
        [recommendation],
        searched_at=datetime(2026, 8, 8, tzinfo=UTC),
    )

    movie = payload["movies"][0]  # type: ignore[index]
    assert movie["raw_result_count"] == 6
    assert movie["accepted_result_count"] == 2
    assert [value["title"] for value in movie["results"]] == [  # type: ignore[index]
        "The.Thing.1982.1080p",
        "Group The Thing 1982 REMUX",
    ]


def test_deduplicates_hashes_and_title_size_and_merges_reported_counts() -> None:
    recommendation = MovieRecommendation(1091, "The Thing", 1982)
    shared_hash = "a" * 40
    raw = [
        release(
            0,
            "The Thing 1982 BluRay",
            indexer_id="alpha",
            seeders=3,
            peers=4,
            infohash=shared_hash,
        ),
        release(
            1,
            "The Thing 1982 BluRay",
            indexer_id="beta",
            seeders=9,
            peers=12,
            infohash=shared_hash,
        ),
        release(
            2,
            "The Thing 1982 WEB-DL",
            indexer_id="alpha",
            seeders=2,
            infohash=None,
            size=2_000,
            download_url="https://jackett.test/dl?path=one",
        ),
        release(
            3,
            "The.Thing.1982.WEB-DL",
            indexer_id="beta",
            seeders=4,
            infohash=None,
            size=2_000,
            download_url="https://jackett.test/dl?path=two",
        ),
    ]
    client = FakeMovieClient(
        {
            ("alpha", "The Thing 1982"): raw[::2],
            ("beta", "The Thing 1982"): raw[1::2],
        },
        indexers=[capability("alpha"), capability("beta")],
    )

    payload = search_recommended_movies(client, [recommendation])
    movie = payload["movies"][0]  # type: ignore[index]
    results = movie["results"]
    assert movie["duplicate_result_count"] == 2
    assert len(results) == 2
    assert results[0]["reported_seeders"] == 9
    assert results[0]["reported_peers"] == 12
    assert results[0]["source_indexers"] == ["alpha", "beta"]
    assert results[1]["reported_seeders"] == 4


def test_http_guid_is_retained_as_a_safe_download_reference() -> None:
    recommendation = MovieRecommendation(1091, "The Thing", 1982)
    value = release(
        0,
        "The Thing 1982 BluRay",
        seeders=3,
        infohash=None,
        guid="https://jackett.test/dl?path=one",
    )
    payload = search_recommended_movies(
        FakeMovieClient({("alpha", "The Thing 1982"): [value]}),
        [recommendation],
    )
    result = payload["movies"][0]["results"][0]  # type: ignore[index]
    assert result["download_url"] == "https://jackett.test/dl?path=one"


def test_caps_ranked_results_at_ten() -> None:
    recommendation = MovieRecommendation(1091, "The Thing", 1982)
    raw = [
        release(
            sequence,
            f"The Thing 1982 Release {sequence}",
            seeders=sequence + 1,
            infohash=f"{sequence + 1:040x}",
        )
        for sequence in range(12)
    ]
    payload = search_recommended_movies(
        FakeMovieClient({("alpha", "The Thing 1982"): raw}),
        [recommendation],
    )
    movie = payload["movies"][0]  # type: ignore[index]
    assert movie["accepted_result_count"] == 12
    assert movie["returned_result_count"] == 10
    assert movie["results"][0]["reported_seeders"] == 12


def test_partial_indexer_failure_keeps_successful_results() -> None:
    recommendation = MovieRecommendation(1091, "The Thing", 1982)
    client = FakeMovieClient(
        {
            ("alpha", "The Thing 1982"): [release(0, "The Thing 1982 BluRay", infohash="a" * 40)],
            ("beta", "The Thing 1982"): JackettNetworkError("offline"),
        },
        indexers=[capability("alpha"), capability("beta")],
    )
    payload = search_recommended_movies(client, [recommendation], fresh=True)
    assert payload["indexers_succeeded"] == ["alpha"]
    assert payload["indexers_failed"] == ["beta"]
    assert payload["movies"][0]["returned_result_count"] == 1  # type: ignore[index]
    assert any(value["code"] == "PARTIAL_INDEXER_FAILURE" for value in payload["warnings"])
    assert all(call[2] for call in client.calls)


def test_cli_writes_safe_text_and_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RECOMMENDATIONS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("JACKETT_URL", "http://jackett.test")
    monkeypatch.setenv("JACKETT_API_KEY", "secret")
    monkeypatch.setenv("JACKETT_INDEXERS", "alpha")
    monkeypatch.setattr("media_scope.movie_search.load_dotenv", lambda: False)
    value = release(
        0,
        "The Thing 1982 BluRay",
        seeders=8,
        infohash="a" * 40,
        download_url="https://jackett.test/dl?path=one",
    )
    client = FakeMovieClient({("alpha", "The Thing 1982"): [value]})

    exit_code = main(
        ["--fresh"],
        client_factory=lambda _url, _key: client,
        input_text=input_payload({"tmdb_id": 1091, "title": "The Thing", "year": 1982}),
        searched_at=datetime(2026, 8, 8, tzinfo=UTC),
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    report = (tmp_path / "JACKETTRESULTS.txt").read_text(encoding="utf-8")
    assert exit_code == 0
    assert payload["movies"][0]["results"][0]["reported_seeders"] == 8
    assert "The Thing 1982 BluRay" in report
    assert "Magnet: magnet:?xt=urn:btih:" in report
    assert "https://" not in report
    assert "secret" not in captured.out + captured.err + report


def test_empty_handoff_skips_jackett_and_replaces_text_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RECOMMENDATIONS_DIRECTORY", str(tmp_path))
    monkeypatch.setattr("media_scope.movie_search.load_dotenv", lambda: False)

    def unexpected_factory(_url: str, _key: str) -> FakeMovieClient:
        raise AssertionError("Jackett should not be created")

    exit_code = main(
        [],
        client_factory=unexpected_factory,
        input_text=input_payload(),
        searched_at=datetime(2026, 8, 8, tzinfo=UTC),
    )
    payload = json.loads(capsys.readouterr().out)
    report = (tmp_path / "JACKETTRESULTS.txt").read_text(encoding="utf-8")
    assert exit_code == 0
    assert payload["movies"] == []
    assert "No recommendations to search" in report


def test_fatal_jackett_failure_preserves_existing_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RECOMMENDATIONS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("JACKETT_URL", "http://jackett.test")
    monkeypatch.setenv("JACKETT_API_KEY", "secret")
    monkeypatch.setattr("media_scope.movie_search.load_dotenv", lambda: False)
    output = tmp_path / "JACKETTRESULTS.txt"
    output.write_text("keep me\n", encoding="utf-8")
    client = FakeMovieClient({("alpha", "The Thing 1982"): JackettNetworkError("offline")})

    exit_code = main(
        [],
        client_factory=lambda _url, _key: client,
        input_text=input_payload({"tmdb_id": 1091, "title": "The Thing", "year": 1982}),
    )
    assert exit_code == 4
    assert output.read_text(encoding="utf-8") == "keep me\n"


def test_text_formatter_includes_magnets_but_not_download_urls() -> None:
    magnet = "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    payload: dict[str, Any] = {
        "searched_at": "2026-08-08T00:00:00Z",
        "movies": [
            {
                "recommendation": {"title": "The Thing", "year": 1982},
                "query": "The Thing 1982",
                "raw_result_count": 1,
                "accepted_result_count": 1,
                "returned_result_count": 1,
                "results": [
                    {
                        "rank": 1,
                        "title": "The Thing 1982",
                        "source_indexers": ["alpha"],
                        "reported_seeders": 4,
                        "reported_peers": 5,
                        "size_bytes": 1024,
                        "published_at": None,
                        "magnet_uri": magnet,
                        "download_url": "https://jackett.test/dl?path=secret",
                    }
                ],
            }
        ],
    }
    text = format_jackett_results(payload)  # type: ignore[arg-type]
    assert magnet in text
    assert "https://jackett.test" not in text
    assert "secret" not in text
