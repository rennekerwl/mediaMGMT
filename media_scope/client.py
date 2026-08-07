"""Synchronous TMDb v3 API client with bounded retries."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from media_scope.exceptions import (
    AuthenticationError,
    InvalidResponseError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    TmdbApiError,
)
from media_scope.models import JsonObject

SleepFunction = Callable[[float], None]


class TmdbClient:
    """Access the TMDb endpoints needed to resolve acquisition scopes."""

    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(
        self,
        bearer_token: str,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.25,
        backoff_cap: float = 2.0,
        transport: httpx.BaseTransport | None = None,
        sleep: SleepFunction = time.sleep,
    ) -> None:
        """Create a client without ever exposing the bearer token."""
        if not bearer_token.strip():
            raise AuthenticationError("TMDB_BEARER_TOKEN is missing or empty.")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if backoff_base < 0 or backoff_cap < 0:
            raise ValueError("backoff values cannot be negative")

        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> TmdbClient:
        """Return this client as a context manager."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the underlying HTTP client."""
        self.close()

    def close(self) -> None:
        """Release network resources."""
        self._client.close()

    def search_movies(self, title: str, year: int | None = None) -> list[JsonObject]:
        """Search the first TMDb movie-results page."""
        params: dict[str, str | int | bool] = {"query": title, "include_adult": False}
        if year is not None:
            params["primary_release_year"] = year
        return self._search("/search/movie", params)

    def search_tv(self, title: str, year: int | None = None) -> list[JsonObject]:
        """Search the first TMDb television-results page."""
        params: dict[str, str | int | bool] = {"query": title, "include_adult": False}
        if year is not None:
            params["first_air_date_year"] = year
        return self._search("/search/tv", params)

    def get_movie(self, tmdb_id: int) -> JsonObject:
        """Retrieve one movie's full details."""
        return self._request(f"/movie/{tmdb_id}")

    def get_movie_recommendations(self, tmdb_id: int) -> list[JsonObject]:
        """Retrieve the first page of recommendations for one movie."""
        payload = self._request(f"/movie/{tmdb_id}/recommendations", params={"page": 1})
        results = payload.get("results")
        if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
            raise InvalidResponseError(
                "TMDb movie recommendations did not contain a valid results list."
            )
        return results

    def get_tv(self, tmdb_id: int) -> JsonObject:
        """Retrieve one television series' full details."""
        return self._request(f"/tv/{tmdb_id}")

    def get_tv_season(self, series_id: int, season_number: int) -> JsonObject:
        """Retrieve one television season and its episode records."""
        return self._request(f"/tv/{series_id}/season/{season_number}")

    def _search(self, path: str, params: dict[str, str | int | bool]) -> list[JsonObject]:
        payload = self._request(path, params=params)
        results = payload.get("results")
        if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
            raise InvalidResponseError("TMDb search response did not contain a valid results list.")
        return results

    def _request(
        self,
        path: str,
        *,
        params: dict[str, str | int | bool] | None = None,
    ) -> JsonObject:
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    self._wait(attempt, None)
                    continue
                raise NetworkError(
                    "TMDb could not be reached after the configured retry attempts."
                ) from exc

            status = response.status_code
            if status == 401:
                raise AuthenticationError(
                    "TMDb rejected the bearer token. Check TMDB_BEARER_TOKEN."
                )
            if status == 404:
                raise NotFoundError("The requested TMDb record was not found.")
            if status == 429:
                if attempt < self.max_retries:
                    self._wait(attempt, response.headers.get("Retry-After"))
                    continue
                raise RateLimitError(
                    "TMDb continued to rate-limit requests after the configured retries."
                )
            if 500 <= status <= 599:
                if attempt < self.max_retries:
                    self._wait(attempt, response.headers.get("Retry-After"))
                    continue
                raise TmdbApiError(f"TMDb returned HTTP {status} after the configured retries.")
            if 400 <= status <= 499:
                raise TmdbApiError(f"TMDb rejected the request with HTTP {status}.")

            try:
                payload: Any = response.json()
            except ValueError as exc:
                raise InvalidResponseError("TMDb returned malformed JSON.") from exc
            if not isinstance(payload, dict):
                raise InvalidResponseError("TMDb returned JSON with an unexpected top-level type.")
            return payload

        raise AssertionError("request retry loop terminated unexpectedly")

    def _wait(self, attempt: int, retry_after: str | None) -> None:
        delay = min(self.backoff_base * (2**attempt), self.backoff_cap)
        if retry_after is not None:
            try:
                delay = min(max(float(retry_after), 0.0), self.backoff_cap)
            except ValueError:
                pass
        self._sleep(delay)
