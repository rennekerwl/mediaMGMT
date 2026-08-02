"""State-machine tests using a fully mocked rTorrent abstraction."""

from __future__ import annotations

from typing import Any

from media_scope.exceptions import RtorrentRpcError, RtorrentRpcFault
from media_scope.probe_directories import ProbeDirectoryManager
from media_scope.probe_input import parse_probe_input
from media_scope.probe_models import RtorrentCapabilities, TorrentStatus
from media_scope.probe_service import ProbePolicy, TorrentProbeService
from tests.test_probe_input import HASH_A, HASH_B, candidate, report

HASH_C = "c" * 40


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeRtorrent:
    sanitized_endpoint = "http://rtorrent.test/RPC"

    def __init__(self, statuses: dict[str, list[Any]], *, existing: set[str] | None = None) -> None:
        self.statuses = {key.upper(): list(value) for key, value in statuses.items()}
        self.existing = {value.upper() for value in (existing or set())}
        self.last_status: dict[str, Any] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.fail_submission: set[str] = set()
        self.fail_submission_after_add: set[str] = set()
        self.fail_erase: set[str] = set()
        self.inactive: set[str] = set()
        self.remote_dirs = {"/remote/home", "/remote/home/probes"}
        self.capabilities = RtorrentCapabilities(
            "0.9.8",
            "0.13.8",
            "9",
            frozenset(),
            "load.start_verbose",
            True,
            "d.is_meta",
        )

    def discover_capabilities(self) -> RtorrentCapabilities:
        self.calls.append(("discover",))
        return self.capabilities

    def torrent_exists(self, infohash: str) -> bool:
        return infohash.upper() in self.existing

    def submit_magnet(self, _magnet: str, infohash: str, directory: str) -> str:
        target = infohash.upper()
        self.calls.append(("submit", target, directory))
        if target in self.fail_submission:
            raise RtorrentRpcError("submission rejected")
        self.existing.add(target)
        if target in self.fail_submission_after_add:
            raise RtorrentRpcError("response lost after submission")
        return "load.start_verbose"

    def is_active(self, infohash: str) -> bool:
        return infohash.upper() not in self.inactive

    def call(self, method: str, *params: object) -> object:
        self.calls.append(("rpc", method, *params))
        if method == "system.cwd":
            return "/remote/home"
        if method == "execute.capture":
            command = params[1]
            path = str(params[-1])
            if command == "/usr/bin/realpath":
                if path not in self.remote_dirs:
                    raise RtorrentRpcFault("missing", fault_code=1)
                return path
            if command == "/usr/bin/stat":
                return "directory\n"
        if method == "execute.throw":
            command = params[1]
            if command == "/usr/bin/test":
                if str(params[-1]) not in self.remote_dirs:
                    raise RtorrentRpcFault("not writable", fault_code=1)
                return 0
            if command == "/bin/mkdir":
                path = str(params[-1])
                parent = path.rsplit("/", 1)[0]
                if parent not in self.remote_dirs:
                    raise RtorrentRpcFault("parent missing", fault_code=1)
                self.remote_dirs.add(path)
                return 0
            if command == "/bin/rm":
                path = str(params[-1])
                self.remote_dirs = {
                    value
                    for value in self.remote_dirs
                    if value != path and not value.startswith(path + "/")
                }
                return 0
            if command == "/bin/rmdir":
                path = str(params[-1])
                if any(value.startswith(path + "/") for value in self.remote_dirs):
                    raise RtorrentRpcFault("not empty", fault_code=1)
                self.remote_dirs.discard(path)
                return 0
        raise AssertionError(f"unexpected remote command: {method} {params}")

    def tag_probe(self, infohash: str, **tags: Any) -> None:
        self.calls.append(("tag", infohash.upper(), tags))

    def set_probe_state(self, infohash: str, state: str) -> None:
        self.calls.append(("state", infohash.upper(), state))

    def status(self, infohash: str) -> TorrentStatus:
        target = infohash.upper()
        values = self.statuses.get(target, [])
        if values:
            value = values.pop(0)
            self.last_status[target] = value
        else:
            value = self.last_status.get(target, TorrentStatus(False, "d.is_meta"))
        if isinstance(value, Exception):
            raise value
        if value == "disappear":
            self.existing.discard(target)
            return TorrentStatus(False, "d.is_meta")
        return value

    def stop(self, infohash: str) -> None:
        self.calls.append(("stop", infohash.upper()))

    def erase(self, infohash: str) -> None:
        self.calls.append(("erase", infohash.upper()))
        if infohash.upper() in self.fail_erase:
            raise RtorrentRpcError("erase failed")
        self.existing.discard(infohash.upper())


def metadata(healthy: bool, peers: int = 0, complete: int = 0) -> TorrentStatus:
    return TorrentStatus(healthy, "d.is_meta", peers, complete, None)


def make_service(
    client: FakeRtorrent,
    *,
    maximum: int = 10,
    timeout: int = 2,
    keep: bool = False,
) -> TorrentProbeService:
    clock = FakeClock()
    manager = ProbeDirectoryManager(client, "/remote/home/probes", "probe-test")
    return TorrentProbeService(
        client,  # type: ignore[arg-type]
        manager,
        ProbePolicy(maximum, timeout, 1, 2, keep),
        job_id="probe-test",
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def run_service(
    service: TorrentProbeService,
    *values: dict[str, Any],
    preflight: str | None = None,
) -> tuple[dict[str, Any], int]:
    return service.run(
        parse_probe_input(report(*values)),
        preflight_magnet=preflight,
        skip_preflight=False,
    )


def test_first_candidate_retrieves_metadata_and_is_stopped_retained() -> None:
    client = FakeRtorrent({HASH_A: [metadata(True, 3, 1)]})
    payload, code = run_service(make_service(client), candidate(1))
    assert code == 0
    assert payload["result"] == "candidate_health_validated"
    assert payload["selected_candidate"]["original_rank"] == 1
    assert payload["selected_candidate"]["rtorrent_state"] == "stopped"
    assert HASH_A.upper() in client.existing
    assert ("stop", HASH_A.upper()) in client.calls
    assert ("erase", HASH_A.upper()) not in client.calls


def test_first_times_out_is_cleaned_and_second_succeeds() -> None:
    client = FakeRtorrent(
        {
            HASH_A: [metadata(False)],
            HASH_B: [metadata(True, 4, 2)],
        }
    )
    payload, code = run_service(
        make_service(client),
        candidate(2, HASH_B),
        candidate(1, HASH_A),
    )
    assert code == 0
    assert [item["original_rank"] for item in payload["attempts"]] == [1, 2]
    assert payload["attempts"][0]["status"] == "METADATA_TIMEOUT"
    assert payload["attempts"][0]["cleanup_performed"] is True
    assert payload["selected_candidate"]["original_rank"] == 2
    assert ("erase", HASH_A.upper()) in client.calls


def test_all_time_out_returns_six_and_no_selection() -> None:
    client = FakeRtorrent({HASH_A: [metadata(False)], HASH_B: [metadata(False)]})
    payload, code = run_service(make_service(client), candidate(1), candidate(2, HASH_B))
    assert code == 6
    assert payload["result"] == "NO_HEALTHY_TORRENT_FOUND"
    assert payload["selected_candidate"] is None
    assert {call[1] for call in client.calls if call[0] == "erase"} == {
        HASH_A.upper(),
        HASH_B.upper(),
    }


def test_submission_failure_continues_to_second_candidate() -> None:
    client = FakeRtorrent({HASH_B: [metadata(True)]})
    client.fail_submission.add(HASH_A.upper())
    payload, code = run_service(make_service(client), candidate(1), candidate(2, HASH_B))
    assert code == 0
    assert payload["attempts"][0]["status"] == "SUBMISSION_FAILED"
    assert payload["selected_candidate"]["original_rank"] == 2


def test_partial_submission_failure_is_detected_and_cleaned() -> None:
    client = FakeRtorrent({})
    client.fail_submission_after_add.add(HASH_A.upper())
    payload, code = run_service(make_service(client), candidate(1))
    assert code == 6
    assert payload["attempts"][0]["status"] == "SUBMISSION_FAILED"
    assert payload["attempts"][0]["cleanup_performed"] is True
    assert HASH_A.upper() not in client.existing


def test_polling_failure_is_retried_then_reported() -> None:
    failure = RtorrentRpcError("status failed")
    client = FakeRtorrent({HASH_A: [failure, failure, failure]})
    payload, code = run_service(make_service(client, timeout=10), candidate(1))
    assert code == 6
    assert payload["attempts"][0]["status"] == "RPC_FAILED"


def test_torrent_disappearance_is_reported_and_cleanup_safe() -> None:
    client = FakeRtorrent({HASH_A: ["disappear"]})
    payload, code = run_service(make_service(client), candidate(1))
    assert code == 6
    assert payload["attempts"][0]["status"] == "TORRENT_DISAPPEARED"


def test_peer_maxima_are_recorded_without_indexer_seed_addition() -> None:
    client = FakeRtorrent({HASH_A: [metadata(False, 2, 1), metadata(True, 5, 3)]})
    payload, _code = run_service(make_service(client), candidate(1))
    attempt = payload["attempts"][0]
    assert attempt["maximum_connected_peers"] == 5
    assert attempt["maximum_complete_peers"] == 3
    assert attempt["indexer_reported_seeders"] == 4


def test_stops_after_first_healthy_and_reports_lower_candidates_unattempted() -> None:
    client = FakeRtorrent({HASH_A: [metadata(True)], HASH_B: [metadata(True)]})
    payload, _code = run_service(make_service(client), candidate(1), candidate(2, HASH_B))
    assert len(payload["attempts"]) == 1
    assert payload["unattempted_candidates"][0]["original_rank"] == 2
    assert not any(call[:2] == ("submit", HASH_B.upper()) for call in client.calls)


def test_candidate_limit_leaves_extra_candidates_unattempted() -> None:
    client = FakeRtorrent({HASH_A: [metadata(False)], HASH_B: [metadata(True)]})
    payload, code = run_service(
        make_service(client, maximum=1), candidate(1), candidate(2, HASH_B)
    )
    assert code == 6
    assert len(payload["attempts"]) == 1
    assert payload["unattempted_candidates"][0]["original_rank"] == 2


def test_preexisting_regular_torrent_is_accepted_without_any_mutation() -> None:
    client = FakeRtorrent({HASH_A: [metadata(True)]}, existing={HASH_A})
    payload, code = run_service(make_service(client), candidate(1))
    assert code == 0
    attempt = payload["attempts"][0]
    assert attempt["status"] == "METADATA_AVAILABLE_PREEXISTING"
    assert attempt["preexisting"] is True
    assert payload["selected_candidate"]["rtorrent_state"] == "preexisting_unchanged"
    assert not any(call[0] in {"submit", "tag", "state", "stop", "erase"} for call in client.calls)


def test_preexisting_meta_torrent_times_out_and_is_never_erased() -> None:
    client = FakeRtorrent({HASH_A: [metadata(False)]}, existing={HASH_A})
    payload, code = run_service(make_service(client), candidate(1))
    assert code == 6
    assert payload["attempts"][0]["cleanup_status"] == "SKIPPED_PREEXISTING"
    assert ("erase", HASH_A.upper()) not in client.calls


def test_created_probe_receives_named_custom_tags() -> None:
    client = FakeRtorrent({HASH_A: [metadata(True)]})
    run_service(make_service(client), candidate(1))
    tag = next(call for call in client.calls if call[0] == "tag")
    assert tag[2] == {
        "job_id": "probe-test",
        "state": "waiting_for_metadata",
        "rank": 1,
    }
    assert ("state", HASH_A.upper(), "validated_waiting_for_download") in client.calls


def test_keep_failed_probes_prevents_cleanup() -> None:
    client = FakeRtorrent({HASH_A: [metadata(False)]})
    payload, code = run_service(make_service(client, keep=True), candidate(1))
    assert code == 6
    attempt = payload["attempts"][0]
    assert attempt["cleanup_status"] == "SKIPPED_KEEP_FAILED_PROBES"
    assert HASH_A.upper() in client.existing


def test_cleanup_failure_is_reported_with_exit_seven() -> None:
    client = FakeRtorrent({HASH_A: [metadata(False)]})
    client.fail_erase.add(HASH_A.upper())
    payload, code = run_service(make_service(client), candidate(1))
    assert code == 7
    assert payload["attempts"][0]["cleanup_status"] == "CLEANUP_FAILED"
    assert any("operator attention" in value for value in payload["warnings"])


def test_no_preflight_configuration_emits_warning() -> None:
    client = FakeRtorrent({HASH_A: [metadata(True)]})
    payload, _code = run_service(make_service(client), candidate(1))
    assert payload["preflight"]["status"] == "NOT_CONFIGURED"
    assert any("cannot be cleanly distinguished" in value for value in payload["warnings"])


def test_preflight_can_be_explicitly_skipped() -> None:
    client = FakeRtorrent({HASH_A: [metadata(True)]})
    service = make_service(client)
    payload, code = service.run(
        parse_probe_input(report(candidate(1))),
        preflight_magnet=f"magnet:?xt=urn:btih:{HASH_C}",
        skip_preflight=True,
    )
    assert code == 0
    assert payload["preflight"]["status"] == "SKIPPED"
    assert not any(call[:2] == ("submit", HASH_C.upper()) for call in client.calls)


def test_preflight_failure_stops_before_candidates_are_submitted() -> None:
    client = FakeRtorrent({HASH_C: [metadata(False)], HASH_A: [metadata(True)]})
    magnet = f"magnet:?xt=urn:btih:{HASH_C}"
    payload, code = run_service(make_service(client), candidate(1), preflight=magnet)
    assert code == 5
    assert payload["result"] == "RTORRENT_NETWORK_UNHEALTHY"
    assert payload["attempts"] == []
    assert not any(call[:2] == ("submit", HASH_A.upper()) for call in client.calls)


def test_preflight_success_is_removed_then_candidates_run() -> None:
    client = FakeRtorrent({HASH_C: [metadata(True)], HASH_A: [metadata(True)]})
    magnet = f"magnet:?xt=urn:btih:{HASH_C}"
    payload, code = run_service(make_service(client), candidate(1), preflight=magnet)
    assert code == 0
    assert payload["preflight"]["status"] == "PASSED"
    assert ("erase", HASH_C.upper()) in client.calls


def test_inactive_submission_fails_without_waiting_for_metadata_timeout() -> None:
    client = FakeRtorrent({HASH_A: [metadata(False)]})
    client.inactive.add(HASH_A.upper())
    payload, code = run_service(make_service(client), candidate(1))
    assert code == 6
    assert payload["attempts"][0]["status"] == "TORRENT_NOT_ACTIVE"
    assert payload["attempts"][0]["activation_confirmed"] is False
