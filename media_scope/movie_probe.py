"""Validate ranked movie torrents on a remote rTorrent seedbox."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
from dotenv import load_dotenv

from media_scope.exceptions import (
    JackettError,
    RtorrentConfigurationError,
    RtorrentError,
    SeedboxFilesystemError,
)
from media_scope.jackett_client import JackettClient, normalize_infohash
from media_scope.magnet_resolver import MagnetResolution, MagnetResolver
from media_scope.models import JsonObject
from media_scope.probe_directories import RemoteProbeDirectoryManager
from media_scope.probe_models import ProbeCandidate, SearchProbeInput
from media_scope.probe_service import ProbePolicy, TorrentProbeService
from media_scope.release_classifier import normalize_release_title
from media_scope.remote_filesystem import RemoteFilesystem, SftpRemoteFilesystem
from media_scope.rtorrent_client import RtorrentClient
from media_scope.search_models import RawRelease
from media_scope.serialization import configure_utf8_stdio, serialize_json

LOGGER = logging.getLogger("media_scope.movie_probe")
OUTPUT_FILENAME = "TORRENTVALIDATION.txt"


class MovieProbeInputError(ValueError):
    """Raised when the movie-search handoff is malformed."""


@dataclass(frozen=True, slots=True)
class MovieProbeCandidate:
    """One ranked movie result awaiting acquisition-reference resolution."""

    search_order: int
    recommendation_rank: int
    tmdb_id: int
    movie_title: str
    year: int
    jackett_rank: int
    release_title: str
    reported_seeders: int
    infohash: str | None
    magnet_uri: str | None
    download_urls: tuple[str, ...]
    raw: JsonObject

    def scope(self) -> JsonObject:
        return {
            "media_type": "movie",
            "tmdb_id": self.tmdb_id,
            "title": self.movie_title,
            "year": self.year,
            "recommendation_rank": self.recommendation_rank,
        }


@dataclass(slots=True)
class _ResolverInput:
    release: RawRelease
    sources: list[RawRelease]


class _JackettContext(AbstractContextManager[JackettClient], Protocol):
    pass


class _RtorrentContext(AbstractContextManager[RtorrentClient], Protocol):
    pass


class _FilesystemContext(AbstractContextManager[RemoteFilesystem], Protocol):
    pass


JackettFactory = Callable[[str, str], _JackettContext]
RtorrentFactory = Callable[..., _RtorrentContext]
FilesystemFactory = Callable[..., _FilesystemContext]


def build_parser() -> argparse.ArgumentParser:
    """Build the separately executable movie-probe parser."""
    parser = argparse.ArgumentParser(
        prog="media-probe-movies",
        description="Read movie Jackett JSON from stdin and select one live torrent.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    parser.add_argument("--verbose", action="store_true", help="Log diagnostics to stderr.")
    parser.add_argument(
        "--recommendation-rank",
        type=_positive_argument,
        metavar="N",
        help="Probe only recommendation N (a positive integer).",
    )
    return parser


def parse_movie_search(
    value: Any, *, recommendation_rank: int | None = None
) -> tuple[MovieProbeCandidate, ...]:
    """Validate and flatten a movie-search report in deterministic probe order."""
    if not isinstance(value, dict):
        raise MovieProbeInputError("Movie-search input must be a JSON object.")
    if value.get("schema_version") != 1:
        raise MovieProbeInputError("Movie-search schema_version must be 1.")
    if value.get("result") != "movie_search_completed":
        raise MovieProbeInputError("Movie-search input did not complete successfully.")
    movies = value.get("movies")
    if not isinstance(movies, list):
        raise MovieProbeInputError("Movie-search input must contain a movies array.")
    if recommendation_rank is not None and _positive_int(recommendation_rank) is None:
        raise MovieProbeInputError("recommendation_rank must be a positive integer.")
    if recommendation_rank is not None and recommendation_rank > len(movies):
        raise MovieProbeInputError(
            f"Recommendation rank {recommendation_rank} does not exist; "
            f"the movie-search input contains {len(movies)} recommendation(s)."
        )

    flattened: list[MovieProbeCandidate] = []
    search_order = 0
    if recommendation_rank is None:
        movie_entries = enumerate(movies, start=1)
    else:
        movie_entries = ((recommendation_rank, movies[recommendation_rank - 1]),)

    for recommendation_rank, movie in movie_entries:
        if not isinstance(movie, dict):
            raise MovieProbeInputError(f"Movie {recommendation_rank} must be an object.")
        recommendation = movie.get("recommendation")
        if not isinstance(recommendation, dict):
            raise MovieProbeInputError(
                f"Movie {recommendation_rank} must contain a recommendation object."
            )
        tmdb_id = _positive_int(recommendation.get("tmdb_id"))
        movie_title = recommendation.get("title")
        year = _year(recommendation.get("year"))
        if tmdb_id is None or not isinstance(movie_title, str) or not movie_title.strip():
            raise MovieProbeInputError(
                f"Movie {recommendation_rank} has an invalid recommendation."
            )
        if year is None:
            raise MovieProbeInputError(f"Movie {recommendation_rank} has an invalid year.")
        results = movie.get("results")
        if not isinstance(results, list):
            raise MovieProbeInputError(f"Movie {recommendation_rank} must contain a results array.")

        seen_ranks: set[int] = set()
        parsed_results: list[tuple[int, JsonObject]] = []
        for position, result in enumerate(results, start=1):
            if not isinstance(result, dict):
                raise MovieProbeInputError(
                    f"Movie {recommendation_rank} result {position} must be an object."
                )
            rank = _positive_int(result.get("rank"))
            release_title = result.get("title")
            seeders = _positive_int(result.get("reported_seeders"))
            if rank is None or rank in seen_ranks:
                raise MovieProbeInputError(
                    f"Movie {recommendation_rank} result {position} has an invalid rank."
                )
            if not isinstance(release_title, str) or not release_title.strip() or seeders is None:
                raise MovieProbeInputError(
                    f"Movie {recommendation_rank} result rank {rank} is malformed."
                )
            seen_ranks.add(rank)
            parsed_results.append((rank, dict(result)))

        for rank, result in sorted(parsed_results, key=lambda item: item[0]):
            supplied_hash = result.get("infohash")
            if supplied_hash is not None and (
                not isinstance(supplied_hash, str) or normalize_infohash(supplied_hash) is None
            ):
                raise MovieProbeInputError(
                    f"Movie {recommendation_rank} result rank {rank} has an invalid infohash."
                )
            magnet = result.get("magnet_uri")
            if magnet is not None and not isinstance(magnet, str):
                raise MovieProbeInputError(
                    f"Movie {recommendation_rank} result rank {rank} has an invalid magnet URI."
                )
            urls = _download_urls(result)
            if not any((supplied_hash, magnet, urls)):
                raise MovieProbeInputError(
                    f"Movie {recommendation_rank} result rank {rank} has no acquisition reference."
                )
            search_order += 1
            flattened.append(
                MovieProbeCandidate(
                    search_order=search_order,
                    recommendation_rank=recommendation_rank,
                    tmdb_id=tmdb_id,
                    movie_title=movie_title.strip(),
                    year=year,
                    jackett_rank=rank,
                    release_title=str(result["title"]).strip(),
                    reported_seeders=int(result["reported_seeders"]),
                    infohash=normalize_infohash(supplied_hash),
                    magnet_uri=magnet.strip()
                    if isinstance(magnet, str) and magnet.strip()
                    else None,
                    download_urls=urls,
                    raw=result,
                )
            )
    return tuple(flattened)


def resolve_candidates(
    client: JackettClient,
    candidates: Sequence[MovieProbeCandidate],
    *,
    maximum: int,
) -> tuple[
    tuple[ProbeCandidate, ...],
    dict[int, MovieProbeCandidate],
    dict[int, str],
    list[JsonObject],
]:
    """Resolve up to ``maximum`` unique candidates without counting resolution failures."""
    resolver = MagnetResolver(client)
    resolved: list[ProbeCandidate] = []
    source_by_rank: dict[int, MovieProbeCandidate] = {}
    method_by_rank: dict[int, str] = {}
    skipped: list[JsonObject] = []
    seen_hashes: set[str] = set()

    for candidate in candidates:
        if len(resolved) >= maximum:
            skipped.append(
                _skipped(candidate, "CANDIDATE_LIMIT", "The rTorrent attempt limit was reached.")
            )
            continue
        outcome = resolver.resolve(cast(Any, _resolver_input(client, candidate)))
        if not isinstance(outcome, MagnetResolution):
            skipped.append(_skipped(candidate, outcome.code, outcome.message))
            continue
        if candidate.infohash is not None and candidate.infohash != outcome.infohash:
            skipped.append(
                _skipped(
                    candidate,
                    "INFOHASH_MISMATCH",
                    "The resolved magnet does not match the Jackett-supplied infohash.",
                )
            )
            continue
        if outcome.infohash in seen_hashes:
            skipped.append(
                _skipped(
                    candidate, "DUPLICATE_INFOHASH", "This torrent was already queued for probing."
                )
            )
            continue
        seen_hashes.add(outcome.infohash)
        probe_rank = len(resolved) + 1
        raw = {
            **candidate.raw,
            "original_title": candidate.release_title,
            "seeders": candidate.reported_seeders,
            "search_order": candidate.search_order,
            "recommendation_rank": candidate.recommendation_rank,
            "jackett_rank": candidate.jackett_rank,
            "resolution_source": outcome.source,
        }
        resolved.append(ProbeCandidate(probe_rank, outcome.magnet_uri, outcome.infohash, raw))
        source_by_rank[probe_rank] = candidate
        method_by_rank[probe_rank] = outcome.source
    return tuple(resolved), source_by_rank, method_by_rank, skipped


def main(
    argv: Sequence[str] | None = None,
    *,
    input_text: str | None = None,
    jackett_client_factory: JackettFactory | None = None,
    rtorrent_client_factory: RtorrentFactory | None = None,
    filesystem_factory: FilesystemFactory | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Run the movie torrent-validation stage."""
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    load_dotenv()

    output = _output_path()
    job_id = _new_job_id()
    payload: JsonObject
    exit_code = 8
    try:
        text = input_text if input_text is not None else sys.stdin.read()
        if not text.strip():
            raise MovieProbeInputError("Movie-search input was empty.")
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MovieProbeInputError("Movie-search input was not valid JSON.") from exc
        candidates = parse_movie_search(decoded, recommendation_rank=args.recommendation_rank)
        maximum = _environment_positive_int("RTORRENT_PROBE_MAX_CANDIDATES", 10)

        jackett_constructor = jackett_client_factory or JackettClient
        with jackett_constructor(
            os.getenv("JACKETT_URL", ""), os.getenv("JACKETT_API_KEY", "")
        ) as jackett:
            resolved, source_by_rank, method_by_rank, skipped = resolve_candidates(
                jackett, candidates, maximum=maximum
            )
        if not resolved:
            payload = {
                "schema_version": 1,
                "result": "NO_PROBEABLE_CANDIDATES",
                "error_code": "NO_PROBEABLE_CANDIDATES",
                "message": "No movie result could be resolved to a unique public magnet.",
                "job_id": job_id,
                "scope": {"media_type": "movie"},
                "attempts": [],
                "skipped_candidates": skipped,
                "selected_candidate": None,
                "warnings": [],
            }
            exit_code = 3
        else:
            policy = ProbePolicy(
                maximum_candidates=maximum,
                metadata_timeout_seconds=_environment_positive_int(
                    "RTORRENT_METADATA_TIMEOUT_SECONDS", 300
                ),
                poll_interval_seconds=_environment_positive_int(
                    "RTORRENT_METADATA_POLL_INTERVAL_SECONDS", 5
                ),
                preflight_timeout_seconds=_environment_positive_int(
                    "RTORRENT_PREFLIGHT_TIMEOUT_SECONDS", 120
                ),
            )
            root = os.getenv("RTORRENT_PROBE_DIRECTORY", "").strip()
            if not root:
                raise RtorrentConfigurationError(
                    "RTORRENT_PROBE_DIRECTORY is missing; configure a remote absolute POSIX path."
                )
            rtorrent = _create_rtorrent(rtorrent_client_factory, transport)
            filesystem = _create_filesystem(filesystem_factory)
            LOGGER.info(
                "Starting movie probe job %s against %s.", job_id, rtorrent.sanitized_endpoint
            )
            with rtorrent, filesystem:
                directories = RemoteProbeDirectoryManager(filesystem, root, job_id)
                payload, exit_code = TorrentProbeService(
                    rtorrent,
                    directories,  # type: ignore[arg-type]
                    policy,
                    job_id=job_id,
                ).run(
                    SearchProbeInput({"media_type": "movie"}, resolved),
                    preflight_magnet=os.getenv("RTORRENT_PREFLIGHT_MAGNET", "").strip() or None,
                    skip_preflight=False,
                )
            _enrich_report(payload, source_by_rank, method_by_rank, skipped)
    except MovieProbeInputError as exc:
        payload = _error_payload("INVALID_MOVIE_SEARCH_INPUT", str(exc), job_id=job_id)
        exit_code = 2
    except JackettError as exc:
        payload = _error_payload(exc.error_code, str(exc), job_id=job_id)
        exit_code = 4
    except (RtorrentError, SeedboxFilesystemError) as exc:
        payload = _error_payload(exc.error_code, str(exc), job_id=job_id)
        exit_code = 4
    except Exception:
        LOGGER.exception("Unexpected internal movie-probe failure.")
        payload = _error_payload(
            "INTERNAL_ERROR",
            "An unexpected internal error occurred. Enable --verbose for diagnostics.",
            job_id=job_id,
        )
        exit_code = 8
    return _emit(payload, exit_code, pretty=args.pretty, output=output)


def _resolver_input(client: JackettClient, candidate: MovieProbeCandidate) -> _ResolverInput:
    references: list[str] = []
    for value in candidate.download_urls:
        try:
            references.append(client.authenticate_acquisition_reference(value))
        except JackettError:
            continue
    release = RawRelease(
        sequence=0,
        indexer_id="movie-search",
        indexer_name="Movie search",
        query=f"{candidate.movie_title} {candidate.year}",
        original_title=candidate.release_title,
        normalized_title=normalize_release_title(candidate.release_title),
        infohash=candidate.infohash,
        magnet_uri=candidate.magnet_uri,
        torznab_magnet_uri=candidate.magnet_uri,
        internal_download_references=tuple(references),
    )
    return _ResolverInput(release, [])


def _download_urls(result: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    direct = result.get("download_url")
    if isinstance(direct, str) and direct.strip():
        values.append(direct.strip())
    sources = result.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            value = source.get("download_url")
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
    return tuple(dict.fromkeys(values))


def _skipped(candidate: MovieProbeCandidate, code: str, message: str) -> JsonObject:
    return {
        "search_order": candidate.search_order,
        "recommendation_rank": candidate.recommendation_rank,
        "jackett_rank": candidate.jackett_rank,
        "tmdb_id": candidate.tmdb_id,
        "movie_title": candidate.movie_title,
        "release_title": candidate.release_title,
        "reason": code,
        "message": message,
    }


def _enrich_report(
    payload: JsonObject,
    source_by_rank: dict[int, MovieProbeCandidate],
    method_by_rank: dict[int, str],
    skipped: list[JsonObject],
) -> None:
    payload["skipped_candidates"] = skipped
    for key in ("attempts", "unattempted_candidates"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            probe_rank = value.get("original_rank")
            source = source_by_rank.get(probe_rank) if isinstance(probe_rank, int) else None
            if source is not None:
                value.update(_candidate_context(source, probe_rank, method_by_rank.get(probe_rank)))
    selected = payload.get("selected_candidate")
    if not isinstance(selected, dict):
        return
    probe_rank = selected.get("original_rank")
    source = source_by_rank.get(probe_rank) if isinstance(probe_rank, int) else None
    if source is None:
        return
    selected.update(_candidate_context(source, probe_rank, method_by_rank.get(probe_rank)))
    selected["original_rank"] = source.jackett_rank
    payload["scope"] = source.scope()


def _candidate_context(
    source: MovieProbeCandidate,
    probe_rank: int,
    resolution_source: str | None,
) -> JsonObject:
    return {
        "probe_order": probe_rank,
        "original_rank": source.jackett_rank,
        "recommendation_rank": source.recommendation_rank,
        "tmdb_id": source.tmdb_id,
        "movie_title": source.movie_title,
        "year": source.year,
        "resolution_source": resolution_source,
    }


def _create_rtorrent(
    factory: RtorrentFactory | None,
    transport: httpx.BaseTransport | None,
) -> _RtorrentContext:
    constructor = factory or RtorrentClient
    return constructor(
        os.getenv("RTORRENT_RPC_URL", ""),
        username=os.getenv("RTORRENT_RPC_USERNAME", ""),
        password=os.getenv("RTORRENT_RPC_PASSWORD", ""),
        verify_tls=_environment_bool("RTORRENT_RPC_VERIFY_TLS", True),
        timeout_seconds=_environment_positive_float("RTORRENT_RPC_TIMEOUT_SECONDS", 15),
        transport=transport,
    )


def _create_filesystem(factory: FilesystemFactory | None) -> _FilesystemContext:
    constructor = factory or SftpRemoteFilesystem
    known_hosts = os.getenv("SEEDBOX_SSH_KNOWN_HOSTS", "").strip()
    return constructor(
        os.getenv("SEEDBOX_SSH_HOST", "").strip(),
        port=_environment_positive_int("SEEDBOX_SSH_PORT", 22),
        username=os.getenv("SEEDBOX_USERNAME", "").strip(),
        password=os.getenv("SEEDBOX_PASSWORD", ""),
        timeout_seconds=_environment_positive_float("SEEDBOX_SSH_TIMEOUT_SECONDS", 15),
        known_hosts=Path(known_hosts) if known_hosts else None,
    )


def _output_path() -> Path | None:
    directory = os.getenv("RECOMMENDATIONS_DIRECTORY", "").strip()
    return Path(directory).expanduser() / OUTPUT_FILENAME if directory else None


def _emit(payload: JsonObject, exit_code: int, *, pretty: bool, output: Path | None) -> int:
    text = serialize_json(payload, pretty=pretty)
    if output is None or not output.parent.is_dir():
        payload = _error_payload(
            "OUTPUT_DIRECTORY_ERROR",
            "RECOMMENDATIONS_DIRECTORY does not identify an accessible directory.",
            job_id=str(payload.get("job_id", "")) or None,
        )
        text = serialize_json(payload, pretty=pretty)
        exit_code = 8
    else:
        try:
            _write_atomic(output, text)
        except OSError:
            LOGGER.exception("Could not write %s.", OUTPUT_FILENAME)
            payload = _error_payload(
                "OUTPUT_WRITE_ERROR",
                f"The resulting JSON could not be written to {OUTPUT_FILENAME}.",
                job_id=str(payload.get("job_id", "")) or None,
            )
            text = serialize_json(payload, pretty=pretty)
            exit_code = 8
    sys.stdout.write(text)
    return exit_code


def _write_atomic(path: Path, text: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            temporary = Path(handle.name)
        temporary.replace(path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _error_payload(error_code: str, message: str, *, job_id: str | None = None) -> JsonObject:
    payload: JsonObject = {
        "schema_version": 1,
        "result": "error",
        "error_code": error_code,
        "message": message,
        "selected_candidate": None,
        "warnings": [],
    }
    if job_id:
        payload["job_id"] = job_id
    return payload


def _new_job_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"probe-movie-{stamp}-{uuid.uuid4().hex[:8]}"


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _positive_argument(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _year(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 1000 <= value <= 9999
        else None
    )


def _environment_positive_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    try:
        parsed = int(value) if value else default
    except ValueError as exc:
        raise RtorrentConfigurationError(f"{name} must be a positive integer.") from exc
    if parsed <= 0:
        raise RtorrentConfigurationError(f"{name} must be a positive integer.")
    return parsed


def _environment_positive_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    try:
        parsed = float(value) if value else default
    except ValueError as exc:
        raise RtorrentConfigurationError(f"{name} must be positive.") from exc
    if parsed <= 0:
        raise RtorrentConfigurationError(f"{name} must be positive.")
    return parsed


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().casefold()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RtorrentConfigurationError(f"{name} must be true or false.")


if __name__ == "__main__":
    raise SystemExit(main())
