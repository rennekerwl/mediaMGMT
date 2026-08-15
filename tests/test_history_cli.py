"""Acquisition-history command tests."""

from __future__ import annotations

import json

from media_scope.google_sheets import GoogleSheetRequestError
from media_scope.history_cli import main


def transfer_payload(result: str = "transfer_completed") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "result": result,
            "scope": {
                "media_type": "movie",
                "tmdb_id": 118340,
                "title": "Guardians of the Galaxy",
                "year": 2014,
            },
            "transfer": {"status": "COMPLETED"},
        }
    )


class FakeHistoryClient:
    def __init__(self, appended: bool = True) -> None:
        self.appended = appended
        self.calls: list[dict[str, object]] = []

    def record_acquired_movie(self, **movie: object) -> bool:
        self.calls.append(movie)
        return self.appended


def test_records_exact_movie_after_verified_transfer(capsys: object) -> None:
    client = FakeHistoryClient()

    code = main([], input_text=transfer_payload(), client_factory=lambda: client)
    payload = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]

    assert code == 0
    assert payload["result"] == "movie_history_recorded"
    assert client.calls == [{"tmdb_id": 118340, "title": "Guardians of the Galaxy", "year": 2014}]


def test_cleanup_failure_still_records_completed_copy(capsys: object) -> None:
    client = FakeHistoryClient(appended=False)

    code = main(
        [],
        input_text=transfer_payload("transfer_completed_cleanup_failed"),
        client_factory=lambda: client,
    )
    payload = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]

    assert code == 0
    assert payload["result"] == "movie_history_already_recorded"
    assert len(client.calls) == 1


def test_sheet_failure_is_structured_and_does_not_leak_details(capsys: object) -> None:
    secret = "private-key-material"

    def failed_client() -> FakeHistoryClient:
        raise GoogleSheetRequestError(f"request failed: {secret}")

    code = main([], input_text=transfer_payload(), client_factory=failed_client)
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert code == 4
    assert json.loads(captured.out)["result"] == "movie_history_failed"
    assert secret not in captured.out + captured.err
