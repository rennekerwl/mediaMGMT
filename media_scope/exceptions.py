"""Application-specific exceptions."""

from __future__ import annotations

from typing import Any


class MediaScopeError(Exception):
    """Base class for expected media-scope failures."""

    error_code = "MEDIA_SCOPE_ERROR"


class CliInputError(MediaScopeError):
    """Raised when command-line arguments are invalid."""

    error_code = "INVALID_CLI_INPUT"


class TmdbError(MediaScopeError):
    """Base class for TMDb communication failures."""

    error_code = "TMDB_API_ERROR"


class AuthenticationError(TmdbError):
    """Raised when a TMDb token is missing or rejected."""

    error_code = "TMDB_AUTHENTICATION_ERROR"


class NotFoundError(TmdbError):
    """Raised when TMDb has no matching record."""

    error_code = "NOT_FOUND"


class RateLimitError(TmdbError):
    """Raised when TMDb continues to rate-limit requests after retries."""

    error_code = "TMDB_RATE_LIMIT"


class NetworkError(TmdbError):
    """Raised when temporary transport failures exhaust the retry budget."""

    error_code = "TMDB_NETWORK_ERROR"


class InvalidResponseError(TmdbError):
    """Raised when TMDb returns malformed or structurally invalid data."""

    error_code = "TMDB_INVALID_RESPONSE"


class TmdbApiError(TmdbError):
    """Raised for non-specialized TMDb HTTP failures."""

    error_code = "TMDB_API_ERROR"


class AmbiguityError(MediaScopeError):
    """Raised when search results cannot be selected without guessing."""

    error_code = "AMBIGUOUS_TITLE"

    def __init__(
        self,
        *,
        media_type: str,
        query: str,
        year: int | None,
        candidates: list[dict[str, Any]],
    ) -> None:
        super().__init__(
            "The title could not be resolved uniquely. Rerun with --tmdb-id using one "
            "of the candidate records."
        )
        self.media_type = media_type
        self.query = query
        self.year = year
        self.candidates = candidates
