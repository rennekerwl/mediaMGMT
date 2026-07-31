"""Resolve safe BitTorrent magnet URIs for provisional search candidates."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

from media_scope.exceptions import JackettError
from media_scope.jackett_client import JackettClient, normalize_infohash, sanitize_result_url
from media_scope.search_models import CanonicalResult, RawRelease

_MAX_MAGNET_LENGTH = 8192
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_REDIRECTS = 5
_MAGNET_TEXT = re.compile(r"(?i)magnet:\?[^\s<>\"']+")
_SECRET_KEYS = {
    "apikey",
    "api_key",
    "jackett_apikey",
    "jackett_api_key",
    "passkey",
}


class MagnetResolutionError(Exception):
    """Expected acquisition-reference failure with a stable diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ValidatedMagnet:
    """Validated and minimally normalized BTIH magnet."""

    magnet_uri: str
    infohash: str


@dataclass(frozen=True, slots=True)
class MagnetResolution:
    """Successful final magnet and its provenance."""

    magnet_uri: str
    infohash: str
    source: str


@dataclass(frozen=True, slots=True)
class MagnetResolutionFailure:
    """Sanitized failure returned when no fallback produced a usable magnet."""

    code: str
    message: str
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TorrentMetainfo:
    """Minimal metainfo facts needed to construct a public v1 magnet."""

    infohash: str
    display_name: str | None
    private: bool


class MagnetResolver:
    """Resolve direct, infohash-derived, or Jackett-backed magnet references."""

    def __init__(
        self,
        client: JackettClient,
        *,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        max_redirects: int = _MAX_REDIRECTS,
    ) -> None:
        self._client = client
        self._max_response_bytes = max_response_bytes
        self._max_redirects = max_redirects
        self._cache: dict[str, MagnetResolution | MagnetResolutionFailure] = {}
        base = urlsplit(client.base_url)
        self._jackett_origin = _origin(base)

    def resolve(
        self,
        result: CanonicalResult,
    ) -> MagnetResolution | MagnetResolutionFailure:
        """Resolve a final magnet for one already deduplicated provisional candidate."""
        occurrences = _ordered_occurrences(result)
        diagnostics: list[str] = []

        direct_groups = (
            (
                "torznab_magneturl",
                [value.torznab_magnet_uri for value in occurrences if value.torznab_magnet_uri],
            ),
            (
                "result_field",
                [magnet for value in occurrences for magnet in value.result_field_magnets],
            ),
        )
        for source, magnets in direct_groups:
            for magnet in dict.fromkeys(magnets):
                cache_key = f"magnet:{magnet}"
                cached = self._cache.get(cache_key)
                if cached is not None:
                    if isinstance(cached, MagnetResolution):
                        return cached
                    diagnostics.append(cached.code)
                    continue
                try:
                    validated = validate_magnet_uri(
                        magnet,
                        secret_checker=self._client.contains_configured_api_key,
                    )
                except MagnetResolutionError as exc:
                    failure = MagnetResolutionFailure(exc.code, str(exc))
                    self._cache[cache_key] = failure
                    diagnostics.append(exc.code)
                    continue
                resolution = MagnetResolution(
                    validated.magnet_uri,
                    validated.infohash,
                    source,
                )
                self._cache[cache_key] = resolution
                return resolution

        for infohash in dict.fromkeys(value.infohash for value in occurrences if value.infohash):
            cache_key = f"infohash:{infohash}"
            cached = self._cache.get(cache_key)
            if cached is not None:
                if isinstance(cached, MagnetResolution):
                    return cached
                diagnostics.append(cached.code)
                continue
            try:
                magnet = construct_magnet_uri(infohash, result.release.original_title)
                validated = validate_magnet_uri(
                    magnet,
                    secret_checker=self._client.contains_configured_api_key,
                )
            except MagnetResolutionError as exc:
                failure = MagnetResolutionFailure(exc.code, str(exc))
                self._cache[cache_key] = failure
                diagnostics.append(exc.code)
                continue
            resolution = MagnetResolution(
                validated.magnet_uri,
                validated.infohash,
                "torznab_infohash",
            )
            self._cache[cache_key] = resolution
            return resolution

        private_failure = False
        references = [
            reference
            for value in occurrences
            for reference in value.internal_download_references
            if self._is_jackett_reference(reference)
        ]
        for reference in dict.fromkeys(references):
            cache_key = f"reference:{sanitize_result_url(reference) or ''}"
            cached = self._cache.get(cache_key)
            if cached is None:
                cached = self._resolve_reference(reference, result.release.original_title)
                self._cache[cache_key] = cached
            if isinstance(cached, MagnetResolution):
                return cached
            diagnostics.extend(cached.diagnostics or (cached.code,))
            private_failure = private_failure or (
                cached.code == "PRIVATE_TORRENT_MAGNET_UNSUPPORTED"
            )

        if not references:
            diagnostics.append("JACKETT_REFERENCE_MISSING")
        code = "PRIVATE_TORRENT_MAGNET_UNSUPPORTED" if private_failure else "NO_USABLE_MAGNET"
        return MagnetResolutionFailure(
            code,
            (
                "The torrent is private and no safe public magnet can be produced."
                if private_failure
                else "No direct, infohash-derived, or Jackett-resolved magnet was available."
            ),
            tuple(dict.fromkeys(diagnostics)),
        )

    def _resolve_reference(
        self,
        reference: str,
        release_title: str,
    ) -> MagnetResolution | MagnetResolutionFailure:
        current = reference
        visited: set[str] = set()
        diagnostics: list[str] = []
        for redirect_count in range(self._max_redirects + 1):
            loop_key = sanitize_result_url(current) or current
            if loop_key in visited:
                return MagnetResolutionFailure(
                    "JACKETT_REDIRECT_LOOP",
                    "Jackett acquisition resolution encountered a redirect loop.",
                )
            visited.add(loop_key)
            try:
                response = self._client.fetch_acquisition_reference(
                    current,
                    max_bytes=self._max_response_bytes,
                )
            except JackettError as exc:
                return MagnetResolutionFailure(
                    "JACKETT_RESOLUTION_FAILED",
                    str(exc),
                    (exc.error_code,),
                )

            if 300 <= response.status_code <= 399:
                location = response.headers.get("location", "").strip()
                if not location:
                    return MagnetResolutionFailure(
                        "JACKETT_RESOLUTION_FAILED",
                        "Jackett returned a redirect without a Location header.",
                    )
                if location.casefold().startswith("magnet:"):
                    try:
                        validated = validate_magnet_uri(
                            location,
                            secret_checker=self._client.contains_configured_api_key,
                        )
                    except MagnetResolutionError as exc:
                        return MagnetResolutionFailure(exc.code, str(exc))
                    return MagnetResolution(
                        validated.magnet_uri,
                        validated.infohash,
                        "jackett_redirect",
                    )
                redirected = urljoin(current, location)
                scheme = urlsplit(redirected).scheme.casefold()
                if scheme not in {"http", "https"}:
                    return MagnetResolutionFailure(
                        "UNSUPPORTED_REDIRECT_SCHEME",
                        "Jackett returned a redirect using an unsupported URI scheme.",
                    )
                if redirect_count >= self._max_redirects:
                    return MagnetResolutionFailure(
                        "JACKETT_REDIRECT_LIMIT",
                        "Jackett acquisition resolution exceeded the redirect limit.",
                    )
                current = redirected
                continue

            try:
                return _resolution_from_body(
                    response.content,
                    response.headers.get("content-type", ""),
                    release_title,
                    secret_checker=self._client.contains_configured_api_key,
                )
            except MagnetResolutionError as exc:
                diagnostics.append(exc.code)
                return MagnetResolutionFailure(
                    exc.code,
                    str(exc),
                    tuple(diagnostics),
                )
        return MagnetResolutionFailure(
            "JACKETT_REDIRECT_LIMIT",
            "Jackett acquisition resolution exceeded the redirect limit.",
        )

    def _is_jackett_reference(self, value: str) -> bool:
        parts = urlsplit(value)
        return parts.scheme.casefold() in {"http", "https"} and (
            _origin(parts) == self._jackett_origin
        )


def validate_magnet_uri(
    value: str,
    *,
    secret_checker: Any | None = None,
) -> ValidatedMagnet:
    """Validate a magnet URI and normalize its first supported BTIH topic."""
    if not value or len(value) > _MAX_MAGNET_LENGTH:
        raise MagnetResolutionError(
            "INVALID_MAGNET_URI",
            "Magnet URI is empty or exceeds the supported size.",
        )
    if _contains_control_characters(value):
        raise MagnetResolutionError(
            "INVALID_MAGNET_URI",
            "Magnet URI contains control characters.",
        )
    parts = urlsplit(value.strip())
    if parts.scheme.casefold() != "magnet":
        raise MagnetResolutionError("INVALID_MAGNET_URI", "URI scheme is not magnet.")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not pairs:
        raise MagnetResolutionError("INVALID_MAGNET_URI", "Magnet URI has no parameters.")

    normalized_pairs: list[tuple[str, str]] = []
    selected_hash: str | None = None
    for key, item in pairs:
        if _contains_control_characters(key) or _contains_control_characters(item):
            raise MagnetResolutionError(
                "INVALID_MAGNET_URI",
                "Magnet URI contains decoded control characters.",
            )
        if _normalized_key(key) in _SECRET_KEYS:
            raise MagnetResolutionError(
                "INVALID_MAGNET_URI",
                "Magnet URI contains a Jackett authentication parameter.",
            )
        if secret_checker is not None and secret_checker(item):
            raise MagnetResolutionError(
                "INVALID_MAGNET_URI",
                "Magnet URI contains the configured Jackett API key.",
            )
        if key.casefold() == "xt" and item.casefold().startswith("urn:btih:"):
            candidate = normalize_infohash(item[9:])
            if candidate is None:
                if selected_hash is None:
                    continue
                continue
            if selected_hash is None:
                selected_hash = candidate
                normalized_pairs.append((key, f"urn:btih:{candidate}"))
            continue
        normalized_pairs.append((key, item))

    if selected_hash is None:
        raise MagnetResolutionError(
            "INVALID_INFOHASH",
            "Magnet URI contains no supported valid BTIH topic.",
        )
    query = urlencode(normalized_pairs, doseq=True, quote_via=quote, safe=":")
    normalized = f"magnet:?{query}"
    if len(normalized) > _MAX_MAGNET_LENGTH:
        raise MagnetResolutionError(
            "INVALID_MAGNET_URI",
            "Normalized magnet URI exceeds the supported size.",
        )
    return ValidatedMagnet(normalized, selected_hash)


def construct_magnet_uri(infohash: str, display_name: str) -> str:
    """Construct a tracker-free public v1 magnet from a valid infohash."""
    normalized = normalize_infohash(infohash)
    if normalized is None:
        raise MagnetResolutionError("INVALID_INFOHASH", "Infohash is not a valid BTIH value.")
    return f"magnet:?xt=urn:btih:{normalized}&dn={quote(display_name, safe='')}"


def parse_torrent_metainfo(content: bytes) -> TorrentMetainfo:
    """Hash the exact raw top-level info dictionary bytes from bencoded metainfo."""
    try:
        parser = _BencodeParser(content)
        info_value, info_start, info_end = parser.parse_top_level_info()
    except (IndexError, RecursionError, ValueError) as exc:
        raise MagnetResolutionError(
            "TORRENT_METAINFO_INVALID",
            "Torrent metainfo is truncated or invalid.",
        ) from exc
    if not isinstance(info_value, dict):
        raise MagnetResolutionError(
            "TORRENT_INFO_DICTIONARY_MISSING",
            "Torrent metainfo has no valid top-level info dictionary.",
        )
    digest = hashlib.sha1(content[info_start:info_end], usedforsecurity=False).hexdigest()
    private = info_value.get(b"private") == 1
    name_value = info_value.get(b"name.utf-8", info_value.get(b"name"))
    display_name = (
        name_value.decode("utf-8", errors="replace")
        if isinstance(name_value, bytes) and name_value
        else None
    )
    return TorrentMetainfo(digest, display_name, private)


def _resolution_from_body(
    content: bytes,
    content_type: str,
    release_title: str,
    *,
    secret_checker: Any | None,
) -> MagnetResolution:
    text = content.decode("utf-8", errors="replace")
    if "html" in content_type.casefold() or "<a" in text.casefold():
        parser = _MagnetHtmlParser()
        parser.feed(text)
        for candidate in parser.magnets:
            try:
                validated = validate_magnet_uri(
                    candidate,
                    secret_checker=secret_checker,
                )
            except MagnetResolutionError:
                continue
            return MagnetResolution(
                validated.magnet_uri,
                validated.infohash,
                "jackett_html",
            )

    for match in _MAGNET_TEXT.finditer(text):
        candidate = html.unescape(match.group(0))
        try:
            validated = validate_magnet_uri(
                candidate,
                secret_checker=secret_checker,
            )
        except MagnetResolutionError:
            continue
        return MagnetResolution(
            validated.magnet_uri,
            validated.infohash,
            "jackett_plain_text",
        )

    try:
        metainfo = parse_torrent_metainfo(content)
    except MagnetResolutionError as exc:
        raise MagnetResolutionError(
            "JACKETT_RESOLUTION_FAILED",
            "Jackett response contained no usable magnet or torrent metainfo.",
        ) from exc
    if metainfo.private:
        raise MagnetResolutionError(
            "PRIVATE_TORRENT_MAGNET_UNSUPPORTED",
            "Private torrent metainfo cannot be converted to a safe public magnet.",
        )
    magnet = construct_magnet_uri(
        metainfo.infohash,
        metainfo.display_name or release_title,
    )
    validated = validate_magnet_uri(magnet, secret_checker=secret_checker)
    return MagnetResolution(
        validated.magnet_uri,
        validated.infohash,
        "jackett_torrent_file",
    )


def _ordered_occurrences(result: CanonicalResult) -> list[RawRelease]:
    values = [result.release, *result.sources]
    unique: dict[int, RawRelease] = {}
    for value in values:
        unique.setdefault(value.sequence, value)
    return list(unique.values())


def _origin(parts: Any) -> tuple[str, str, int | None]:
    return (parts.scheme.casefold(), (parts.hostname or "").casefold(), parts.port)


def _normalized_key(value: str) -> str:
    return value.casefold().replace("-", "_")


def _contains_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


class _MagnetHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.magnets: list[str] = []

    def handle_starttag(
        self,
        _tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        for key, value in attrs:
            if key.casefold() == "href" and value and value.casefold().startswith("magnet:"):
                self.magnets.append(value)


class _BencodeParser:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.position = 0
        self.items = 0

    def parse_top_level_info(self) -> tuple[object, int, int]:
        if self._take() != ord("d"):
            raise ValueError("top-level value is not a dictionary")
        info: object | None = None
        info_start = -1
        info_end = -1
        while self._peek() != ord("e"):
            key = self._parse_bytes()
            value_start = self.position
            value = self._parse_value(1)
            if key == b"info":
                if info is not None:
                    raise ValueError("duplicate info dictionary")
                info = value
                info_start = value_start
                info_end = self.position
        self._take()
        if self.position != len(self.content):
            raise ValueError("trailing bencode data")
        if info is None:
            raise MagnetResolutionError(
                "TORRENT_INFO_DICTIONARY_MISSING",
                "Torrent metainfo has no top-level info dictionary.",
            )
        return info, info_start, info_end

    def _parse_value(self, depth: int) -> object:
        if depth > 100:
            raise ValueError("bencode nesting is too deep")
        self.items += 1
        if self.items > 1_000_000:
            raise ValueError("bencode contains too many values")
        token = self._peek()
        if token == ord("i"):
            return self._parse_integer()
        if token == ord("l"):
            self._take()
            values: list[object] = []
            while self._peek() != ord("e"):
                values.append(self._parse_value(depth + 1))
            self._take()
            return values
        if token == ord("d"):
            self._take()
            values: dict[bytes, object] = {}
            while self._peek() != ord("e"):
                key = self._parse_bytes()
                values[key] = self._parse_value(depth + 1)
            self._take()
            return values
        if ord("0") <= token <= ord("9"):
            return self._parse_bytes()
        raise ValueError("invalid bencode token")

    def _parse_integer(self) -> int:
        self._take()
        end = self.content.index(b"e", self.position)
        raw = self.content[self.position : end]
        if not raw or raw in {b"-0"} or (raw.startswith(b"0") and raw != b"0"):
            raise ValueError("invalid bencode integer")
        value = int(raw)
        self.position = end + 1
        return value

    def _parse_bytes(self) -> bytes:
        colon = self.content.index(b":", self.position)
        raw_length = self.content[self.position : colon]
        if not raw_length or not raw_length.isdigit():
            raise ValueError("invalid bencode string length")
        length = int(raw_length)
        start = colon + 1
        end = start + length
        if end > len(self.content):
            raise ValueError("truncated bencode string")
        self.position = end
        return self.content[start:end]

    def _peek(self) -> int:
        if self.position >= len(self.content):
            raise ValueError("truncated bencode value")
        return self.content[self.position]

    def _take(self) -> int:
        value = self._peek()
        self.position += 1
        return value
