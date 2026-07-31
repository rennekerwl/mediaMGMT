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


class SearchInputError(MediaScopeError):
    """Raised when a search scope file is malformed or structurally invalid."""

    error_code = "INVALID_SCOPE_INPUT"


class UnsupportedSearchScopeError(MediaScopeError):
    """Raised when a valid scope is outside complete ended-TV search support."""

    error_code = "UNSUPPORTED_SCOPE"


class JackettError(MediaScopeError):
    """Base class for Jackett configuration and communication failures."""

    error_code = "JACKETT_ERROR"


class JackettConfigurationError(JackettError):
    """Raised when Jackett configuration is missing or invalid."""

    error_code = "JACKETT_CONFIGURATION_ERROR"


class JackettAuthenticationError(JackettError):
    """Raised when Jackett rejects the configured API key."""

    error_code = "JACKETT_AUTHENTICATION_ERROR"


class JackettNetworkError(JackettError):
    """Raised when retryable Jackett failures exhaust the retry budget."""

    error_code = "JACKETT_UNAVAILABLE"


class JackettResponseError(JackettError):
    """Raised when Jackett returns malformed or unsafe XML."""

    error_code = "JACKETT_INVALID_RESPONSE"


class JackettApiError(JackettError):
    """Raised for non-retryable Jackett HTTP failures."""

    error_code = "JACKETT_API_ERROR"


class AllIndexersFailedError(JackettError):
    """Raised when no selected Jackett indexer completes a valid query."""

    error_code = "ALL_INDEXERS_FAILED"

    def __init__(self, message: str, *, diagnostics: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics
