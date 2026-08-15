"""Authenticated Sheets client tests."""

from __future__ import annotations

import pytest

from media_scope.google_sheets import (
    GoogleSheetConfigurationError,
    GoogleSheetSchemaError,
    GoogleSheetsClient,
    create_google_sheets_client,
)


class FakeRequest:
    def __init__(self, result: object) -> None:
        self.result = result

    def execute(self) -> object:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeValues:
    def __init__(self, rows: list[list[object]]) -> None:
        self.rows = rows
        self.append_calls: list[dict[str, object]] = []

    def get(self, **_kwargs: object) -> FakeRequest:
        return FakeRequest({"values": self.rows})

    def append(self, **kwargs: object) -> FakeRequest:
        self.append_calls.append(kwargs)
        return FakeRequest({"updates": {"updatedRows": 1}})


class FakeSpreadsheets:
    def __init__(self, values: FakeValues) -> None:
        self._values = values

    def values(self) -> FakeValues:
        return self._values


class FakeService:
    def __init__(self, values: FakeValues) -> None:
        self._spreadsheets = FakeSpreadsheets(values)

    def spreadsheets(self) -> FakeSpreadsheets:
        return self._spreadsheets


def rows(*data: list[object]) -> list[list[object]]:
    return [["Title", "Year", "Rating", "Notes", "TMDb ID"], *data]


def test_record_appends_exact_history_row() -> None:
    values = FakeValues(rows(["Arrival", 2016, 5, "great", 329865]))
    client = GoogleSheetsClient(FakeService(values), "sheet_123", "Sheet1")

    appended = client.record_acquired_movie(
        tmdb_id=118340, title="Guardians of the Galaxy", year=2014
    )

    assert appended is True
    assert values.append_calls == [
        {
            "spreadsheetId": "sheet_123",
            "range": "'Sheet1'!A:E",
            "valueInputOption": "RAW",
            "insertDataOption": "INSERT_ROWS",
            "body": {"values": [["Guardians of the Galaxy", 2014, "", "", 118340]]},
        }
    ]


def test_record_is_idempotent_when_tmdb_id_exists() -> None:
    values = FakeValues(rows(["Guardians of the Galaxy", 2014, "", "", "118340"]))
    client = GoogleSheetsClient(FakeService(values), "sheet_123", "Sheet1")

    assert (
        client.record_acquired_movie(tmdb_id=118340, title="Guardians of the Galaxy", year=2014)
        is False
    )
    assert values.append_calls == []


def test_record_rejects_unexpected_headers_without_writing() -> None:
    values = FakeValues([["Title", "Year", "Rating", "Notes"]])
    client = GoogleSheetsClient(FakeService(values), "sheet_123", "Sheet1")

    with pytest.raises(GoogleSheetSchemaError, match="TMDb ID"):
        client.record_acquired_movie(tmdb_id=1, title="Movie", year=2000)
    assert values.append_calls == []


def test_invalid_inline_credentials_are_rejected_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-key-material"
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet_123")
    monkeypatch.setenv("GOOGLE_SHEET_TAB", "Sheet1")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", f"not-json-{secret}")

    with pytest.raises(GoogleSheetConfigurationError) as captured:
        create_google_sheets_client()

    assert secret not in str(captured.value)
