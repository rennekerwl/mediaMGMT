"""Authenticated Google Sheets access for ratings and acquisition history."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from typing import Any, Protocol

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
EXPECTED_HEADERS = ("title", "year", "rating", "notes", "tmdb id")
_SPREADSHEET_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class GoogleSheetError(Exception):
    """Base class for safe Google Sheets failures."""

    error_code = "GOOGLE_SHEET_ERROR"


class GoogleSheetConfigurationError(GoogleSheetError):
    """Raised when Sheets authentication or destination configuration is invalid."""

    error_code = "GOOGLE_SHEET_CONFIGURATION_ERROR"


class GoogleSheetRequestError(GoogleSheetError):
    """Raised when an authenticated Sheets request fails."""

    error_code = "GOOGLE_SHEET_REQUEST_FAILED"


class GoogleSheetSchemaError(GoogleSheetError):
    """Raised when the configured tab does not expose the expected movie columns."""

    error_code = "GOOGLE_SHEET_SCHEMA_ERROR"


class SheetsValuesResource(Protocol):
    """Small protocol covering the Google values API used by this project."""

    def get(self, **kwargs: object) -> Any: ...

    def append(self, **kwargs: object) -> Any: ...


class SheetsService(Protocol):
    """Small protocol covering the generated Sheets service used by this project."""

    def spreadsheets(self) -> Any: ...


class GoogleSheetsClient:
    """Read ratings and idempotently append successfully acquired movies."""

    def __init__(self, service: SheetsService, spreadsheet_id: str, tab: str) -> None:
        self.service = service
        self.spreadsheet_id = _validate_spreadsheet_id(spreadsheet_id)
        self.tab = _validate_tab(tab)

    @property
    def movie_range(self) -> str:
        """Return the quoted ratings/history range."""
        return f"{_quote_tab(self.tab)}!A:E"

    def read_movie_rows(self) -> list[list[object]]:
        """Return the populated movie table values without exposing API response metadata."""
        try:
            response = (
                self.service.spreadsheets()
                .values()
                .get(spreadsheetId=self.spreadsheet_id, range=self.movie_range)
                .execute()
            )
        except Exception as exc:
            raise GoogleSheetRequestError("The Google Sheet could not be read.") from exc
        if not isinstance(response, dict):
            raise GoogleSheetRequestError("The Google Sheet returned an invalid response.")
        raw_rows = response.get("values", [])
        if not isinstance(raw_rows, list) or any(not isinstance(row, list) for row in raw_rows):
            raise GoogleSheetRequestError("The Google Sheet returned invalid row data.")
        return [list(row) for row in raw_rows]

    def record_acquired_movie(self, *, tmdb_id: int, title: str, year: int) -> bool:
        """Append one movie unless its TMDb ID is already present.

        Returns ``True`` when a row was appended and ``False`` when the ID was already
        recorded.
        """
        if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id <= 0:
            raise GoogleSheetSchemaError("The acquired movie has an invalid TMDb ID.")
        clean_title = title.strip() if isinstance(title, str) else ""
        if not clean_title:
            raise GoogleSheetSchemaError("The acquired movie has no title.")
        if not isinstance(year, int) or isinstance(year, bool) or not 1000 <= year <= 9999:
            raise GoogleSheetSchemaError("The acquired movie has an invalid year.")

        rows = self.read_movie_rows()
        _validate_headers(rows)
        if tmdb_id in _recorded_ids(rows[1:]):
            return False

        try:
            (
                self.service.spreadsheets()
                .values()
                .append(
                    spreadsheetId=self.spreadsheet_id,
                    range=self.movie_range,
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [[clean_title, year, "", "", tmdb_id]]},
                )
                .execute()
            )
        except Exception as exc:
            raise GoogleSheetRequestError("The acquired movie could not be recorded.") from exc
        return True


def create_google_sheets_client() -> GoogleSheetsClient:
    """Create the configured service-account Sheets client."""
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    tab = os.getenv("GOOGLE_SHEET_TAB", "").strip()
    credentials_text = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not credentials_text:
        raise GoogleSheetConfigurationError("GOOGLE_SERVICE_ACCOUNT_JSON is missing.")
    try:
        credentials_info = json.loads(credentials_text)
    except json.JSONDecodeError as exc:
        raise GoogleSheetConfigurationError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON."
        ) from exc
    if not isinstance(credentials_info, dict):
        raise GoogleSheetConfigurationError("GOOGLE_SERVICE_ACCOUNT_JSON is not a JSON object.")
    _validate_spreadsheet_id(spreadsheet_id)
    _validate_tab(tab)

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        credentials = Credentials.from_service_account_info(credentials_info, scopes=[SHEETS_SCOPE])
        service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    except (ImportError, OSError, ValueError) as exc:
        raise GoogleSheetConfigurationError(
            "The Google service-account credentials could not be loaded."
        ) from exc
    except Exception as exc:
        raise GoogleSheetRequestError("The Google Sheets client could not be created.") from exc
    return GoogleSheetsClient(service, spreadsheet_id, tab)


def _validate_spreadsheet_id(value: str) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if not clean or _SPREADSHEET_ID.fullmatch(clean) is None:
        raise GoogleSheetConfigurationError("GOOGLE_SHEET_ID is missing or invalid.")
    return clean


def _validate_tab(value: str) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if not clean or any(character in clean for character in ("\x00", "\n", "\r")):
        raise GoogleSheetConfigurationError("GOOGLE_SHEET_TAB is missing or invalid.")
    return clean


def _quote_tab(value: str) -> str:
    return f"'{value.replace(chr(39), chr(39) * 2)}'"


def _validate_headers(rows: Sequence[Sequence[object]]) -> None:
    if not rows:
        raise GoogleSheetSchemaError("The Google Sheet has no header row.")
    normalized = tuple(_cell_text(value).casefold() for value in rows[0][:5])
    if normalized != EXPECTED_HEADERS:
        raise GoogleSheetSchemaError(
            "Sheet1 must use the columns Title, Year, Rating, Notes, and TMDb ID."
        )


def _recorded_ids(rows: Sequence[Sequence[object]]) -> set[int]:
    result: set[int] = set()
    for row in rows:
        if len(row) < 5:
            continue
        value = _positive_integer(row[4])
        if value is not None:
            result.add(value)
    return result


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    text = _cell_text(value)
    try:
        number = int(text)
    except ValueError:
        return None
    return number if number > 0 and str(number) == text else None


def _cell_text(value: object) -> str:
    return "" if value is None else str(value).strip()
