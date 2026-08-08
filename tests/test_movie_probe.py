"""Movie-search adaptation, resolution, remote probing, and CLI tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from media_scope.download_input import parse_download_input
from media_scope.jackett_client import JackettClient
from media_scope.movie_probe import (
    MovieProbeInputError,
    main,
    parse_movie_search,
    resolve_candidates,
)
from media_scope.probe_directories import RemoteProbeDirectoryManager
from tests.fake_remote_filesystem import FakeRemoteFilesystem
from tests.test_probe_input import HASH_A, HASH_B
from tests.test_probe_service import FakeRtorrent, metadata


def movie_report(*results: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": "movie_search_completed",
        "movies": [
            {
                "recommendation": {"tmdb_id": 1091, "title": "The Thing", "year": 1982},
                "results": list(results),
            }
        ],
    }


def result(
    rank: int,
    infohash: str | None = HASH_A,
    *,
    magnet: str | None = None,
    download_url: str | None = None,
) -> dict[str, Any]:
    return {
        "rank": rank,
        "title": f"The Thing 1982 Release {rank}",
        "reported_seeders": 5,
        "infohash": infohash,
        "magnet_uri": magnet,
        "download_url": download_url,
        "sources": [],
    }


class ContextRtorrent(FakeRtorrent):
    def __enter__(self) -> ContextRtorrent:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_parse_movie_search_sorts_results_and_rejects_bad_handoffs() -> None:
    values = parse_movie_search(movie_report(result(2, HASH_B), result(1, HASH_A)))
    assert [value.jackett_rank for value in values] == [1, 2]
    assert values[0].tmdb_id == 1091

    for value in ({}, {"schema_version": 1, "result": "error", "movies": []}):
        with pytest.raises(MovieProbeInputError):
            parse_movie_search(value)


def test_resolution_uses_hash_fallback_and_does_not_count_failures() -> None:
    invalid = result(1, None, magnet="not-a-magnet")
    valid = result(2, HASH_B)
    candidates = parse_movie_search(movie_report(invalid, valid))
    with JackettClient("http://jackett.test", "secret") as client:
        resolved, mapping, methods, skipped = resolve_candidates(client, candidates, maximum=1)
    assert len(resolved) == 1
    assert resolved[0].infohash == HASH_B
    assert mapping[1].jackett_rank == 2
    assert methods[1] == "torznab_infohash"
    assert skipped[0]["reason"] == "NO_USABLE_MAGNET"


def test_download_url_is_authenticated_and_public_torrent_is_resolved() -> None:
    requests: list[httpx.Request] = []
    torrent = (
        (Path(__file__).parent / "fixtures" / "jackett_responses" / "public.torrent")
        .read_bytes()
        .strip()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=torrent, headers={"Content-Type": "application/x-bittorrent"}
        )

    candidates = parse_movie_search(
        movie_report(result(1, None, download_url="http://jackett.test/dl/item?path=one"))
    )
    with JackettClient(
        "http://jackett.test", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        resolved, _mapping, methods, skipped = resolve_candidates(client, candidates, maximum=10)
    assert len(resolved) == 1
    assert methods[1] == "jackett_torrent_file"
    assert skipped == []
    assert requests[0].url.params["apikey"] == "secret"


def test_remote_probe_directory_cleanup_is_scoped() -> None:
    filesystem = FakeRemoteFilesystem()
    manager = RemoteProbeDirectoryManager(filesystem, "/srv/probes", "probe-movie-test")
    manager.prepare_job()
    candidate = manager.prepare_candidate(HASH_A)
    filesystem.add_file(candidate / "payload.part")
    manager.cleanup_candidate(candidate)
    manager.cleanup_empty_job()
    assert not filesystem.exists(candidate)
    assert not filesystem.exists(manager.job_directory)
    assert filesystem.exists(manager.root)


def test_cli_selects_one_torrent_and_mirrors_exact_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RECOMMENDATIONS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("JACKETT_URL", "http://jackett.test")
    monkeypatch.setenv("JACKETT_API_KEY", "secret")
    monkeypatch.setenv("RTORRENT_PROBE_DIRECTORY", "/srv/probes")
    monkeypatch.setenv("RTORRENT_PROBE_MAX_CANDIDATES", "10")
    monkeypatch.setenv("RTORRENT_PREFLIGHT_MAGNET", "")
    monkeypatch.setattr("media_scope.movie_probe.load_dotenv", lambda: False)
    rtorrent = ContextRtorrent({HASH_A: [metadata(True, 3, 1)]})
    filesystem = FakeRemoteFilesystem()

    code = main(
        [],
        input_text=json.dumps(movie_report(result(1, HASH_A))),
        rtorrent_client_factory=lambda *_args, **_kwargs: rtorrent,
        filesystem_factory=lambda *_args, **_kwargs: filesystem,
    )

    captured = capsys.readouterr()
    mirrored = (tmp_path / "TORRENTVALIDATION.txt").read_text(encoding="utf-8")
    payload = json.loads(captured.out)
    assert code == 0
    assert mirrored == captured.out
    assert payload["scope"] == {
        "media_type": "movie",
        "tmdb_id": 1091,
        "title": "The Thing",
        "year": 1982,
        "recommendation_rank": 1,
    }
    assert payload["selected_candidate"]["status"] == "READY_FOR_DOWNLOAD"
    assert payload["selected_candidate"]["original_rank"] == 1
    handoff = parse_download_input(payload)
    assert handoff.scope["tmdb_id"] == 1091
    assert handoff.candidate.infohash == HASH_A
    assert "secret" not in captured.out + captured.err + mirrored
    assert HASH_A.upper() in rtorrent.existing
