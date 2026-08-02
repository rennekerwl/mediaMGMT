"""Opt-in live rTorrent metadata test using only operator-supplied legal content."""

from __future__ import annotations

import os
import time

import pytest

from media_scope.magnet_resolver import validate_magnet_uri
from media_scope.probe_directories import ProbeDirectoryManager
from media_scope.rtorrent_client import RtorrentClient


@pytest.mark.skipif(
    os.getenv("RUN_RTORRENT_INTEGRATION_TESTS", "").casefold() != "true",
    reason="Live rTorrent integration tests are explicitly disabled.",
)
def test_user_supplied_legal_magnet_retrieves_metadata_and_is_removed() -> None:
    """SIDE EFFECTS: temporarily adds, starts, stops, and erases the supplied magnet."""
    magnet = os.environ["RTORRENT_INTEGRATION_TEST_MAGNET"]
    validated = validate_magnet_uri(magnet)
    client = RtorrentClient(
        os.environ["RTORRENT_RPC_URL"],
        username=os.getenv("RTORRENT_RPC_USERNAME", ""),
        password=os.getenv("RTORRENT_RPC_PASSWORD", ""),
        verify_tls=os.getenv("RTORRENT_RPC_VERIFY_TLS", "true").casefold() == "true",
    )
    created = False
    directory = None
    manager: ProbeDirectoryManager | None = None
    try:
        with client:
            client.discover_capabilities()
            manager = ProbeDirectoryManager(
                client,
                os.environ["RTORRENT_PROBE_DIRECTORY"],
                "probe-integration-test",
            )
            manager.prepare_job()
            if client.torrent_exists(validated.infohash):
                pytest.skip(
                    "The legal integration-test torrent already exists; it was not modified."
                )
            directory = manager.prepare_candidate(validated.infohash)
            try:
                client.submit_magnet(validated.magnet_uri, validated.infohash, directory)
                created = True
                client.tag_probe(
                    validated.infohash,
                    job_id="probe-integration-test",
                    state="integration_test",
                    rank="integration_test",
                )
                deadline = time.monotonic() + float(
                    os.getenv("RTORRENT_INTEGRATION_TEST_TIMEOUT_SECONDS", "120")
                )
                while time.monotonic() < deadline:
                    if client.status(validated.infohash).metadata_retrieved:
                        break
                    time.sleep(2)
                else:
                    pytest.fail("The operator-supplied legal magnet did not retrieve metadata.")
            finally:
                if created and client.torrent_exists(validated.infohash):
                    client.stop(validated.infohash)
                    client.erase(validated.infohash)
    finally:
        if directory is not None and manager is not None:
            manager.cleanup_candidate(directory)
        if manager is not None:
            manager.cleanup_empty_job()
