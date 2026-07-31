"""Synchronous Jackett Torznab client and defensive XML parsing."""

from __future__ import annotations

import base64
import math
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from media_scope.exceptions import (
    JackettApiError,
    JackettAuthenticationError,
    JackettConfigurationError,
    JackettNetworkError,
    JackettResponseError,
)
from media_scope.search_models import (
    IndexerCapabilities,
    RawRelease,
    TorznabCategory,
)

SleepFunction = Callable[[float], None]
_INDEXER_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_TORZNAB_NAMESPACE = "http://torznab.com/schemas/2015/feed"
_MAX_XML_BYTES = 10 * 1024 * 1024
_SECRET_QUERY_KEYS = {"apikey", "api_key", "passkey"}


class JackettClient:
    """Access the Jackett endpoints needed for independent Torznab searches."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: SleepFunction = time.sleep,
    ) -> None:
        self.base_url = normalize_jackett_url(base_url)
        if not api_key.strip():
            raise JackettConfigurationError(
                "JACKETT_API_KEY is missing. Configure it in the environment or a .env file."
            )
        self._api_key = api_key.strip()
        self._sleep = sleep
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(20.0, connect=5.0),
            follow_redirects=False,
            headers={"Accept": "application/xml, application/rss+xml"},
        )

    def __enter__(self) -> JackettClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def discover_indexers(self) -> list[IndexerCapabilities]:
        """Discover configured indexers and their embedded capabilities."""
        content = self._get_xml(
            "all",
            {"t": "indexers", "configured": "true"},
            allow_all=True,
        )
        root = _parse_xml(content)
        if _local_name(root.tag) != "indexers":
            raise JackettResponseError("Jackett indexer discovery returned an unexpected root.")
        indexers: list[IndexerCapabilities] = []
        for element in root:
            if _local_name(element.tag) != "indexer":
                continue
            indexer_id = (element.get("id") or "").strip()
            if not indexer_id or not _INDEXER_ID.fullmatch(indexer_id):
                continue
            if (element.get("configured") or "").lower() not in {"true", "1"}:
                continue
            title = _child_text(element, "title") or indexer_id
            caps = next(
                (child for child in element if _local_name(child.tag) == "caps"),
                None,
            )
            if caps is None:
                continue
            indexers.append(_parse_capabilities(caps, indexer_id, title))
        if not indexers:
            raise JackettResponseError("Jackett reported no configured searchable indexers.")
        return sorted(indexers, key=lambda item: item.id.casefold())

    def get_capabilities(self, indexer_id: str) -> IndexerCapabilities:
        """Retrieve capabilities for one explicitly selected indexer."""
        _validate_indexer_id(indexer_id)
        root = _parse_xml(self._get_xml(indexer_id, {"t": "caps"}))
        if _local_name(root.tag) != "caps":
            raise JackettResponseError("Jackett capabilities returned an unexpected root.")
        return _parse_capabilities(root, indexer_id, indexer_id)

    def search(
        self,
        indexer: IndexerCapabilities,
        query: str,
        *,
        fresh: bool,
        sequence_start: int,
    ) -> list[RawRelease]:
        """Search one indexer once without following any returned links."""
        mode = indexer.search_mode
        if mode is None:
            raise JackettApiError(f"Indexer {indexer.id} supports neither tvsearch nor search.")
        params: dict[str, str] = {"t": mode, "q": query}
        if indexer.supports_tv_category:
            params["cat"] = "5000"
        if fresh:
            params["cache"] = "false"
        content = self._get_xml(indexer.id, params)
        return parse_torznab_results(
            content,
            indexer=indexer,
            query=query,
            sequence_start=sequence_start,
        )

    def _get_xml(
        self,
        indexer_id: str,
        params: dict[str, str],
        *,
        allow_all: bool = False,
    ) -> bytes:
        _validate_indexer_id(indexer_id, allow_all=allow_all)
        url = f"{self.base_url}/api/v2.0/indexers/{indexer_id}/results/torznab/api"
        request_params = {"apikey": self._api_key, **params}
        response: httpx.Response | None = None
        for attempt in range(4):
            try:
                response = self._client.get(url, params=request_params)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 3:
                    raise JackettNetworkError(
                        "Jackett could not be reached after bounded retries."
                    ) from exc
                self._sleep(float(2**attempt))
                continue

            if response.status_code in (401, 403):
                raise JackettAuthenticationError("Jackett rejected the configured API key.")
            retryable = response.status_code == 429 or 500 <= response.status_code <= 599
            if retryable:
                if attempt == 3:
                    raise JackettNetworkError(
                        f"Jackett returned HTTP {response.status_code} after bounded retries."
                    )
                self._sleep(_retry_delay(response, attempt))
                continue
            if 400 <= response.status_code:
                raise JackettApiError(
                    f"Jackett returned non-retryable HTTP {response.status_code}."
                )
            content = response.content
            if len(content) > _MAX_XML_BYTES:
                raise JackettResponseError("Jackett XML response exceeded the 10 MiB limit.")
            return content
        raise JackettNetworkError("Jackett request failed unexpectedly.")


def normalize_jackett_url(value: str) -> str:
    """Validate and normalize a Jackett base URL without losing proxy paths."""
    stripped = value.strip().rstrip("/")
    if not stripped:
        raise JackettConfigurationError(
            "JACKETT_URL is missing. Configure it in the environment or a .env file."
        )
    parts = urlsplit(stripped)
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise JackettConfigurationError(
            "JACKETT_URL must be an HTTP(S) base URL without credentials, query, or fragment."
        )
    return stripped


def sanitize_result_url(value: str | None) -> str | None:
    """Remove Jackett authentication query parameters from a serialized URL."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.lower().startswith("magnet:"):
        return stripped
    parts = urlsplit(stripped)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return stripped
    safe_query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in _SECRET_QUERY_KEYS
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(safe_query, doseq=True), parts.fragment)
    )


def normalize_infohash(value: str | None) -> str | None:
    """Normalize hexadecimal or base32 BitTorrent v1 infohashes."""
    if value is None:
        return None
    stripped = value.strip()
    if re.fullmatch(r"[0-9A-Fa-f]{40}", stripped):
        return stripped.lower()
    if re.fullmatch(r"[A-Za-z2-7]{32}", stripped):
        try:
            return base64.b32decode(stripped.upper()).hex()
        except ValueError:
            return None
    return None


def magnet_btih(value: str | None) -> str | None:
    """Extract and normalize the BTIH topic from a magnet URI."""
    if value is None or not value.lower().startswith("magnet:"):
        return None
    for key, item in parse_qsl(urlsplit(value).query, keep_blank_values=True):
        if key.casefold() == "xt" and item.lower().startswith("urn:btih:"):
            return normalize_infohash(item[9:])
    return None


def parse_torznab_results(
    content: bytes,
    *,
    indexer: IndexerCapabilities,
    query: str,
    sequence_start: int = 0,
) -> list[RawRelease]:
    """Parse a Jackett RSS/Torznab response without retrieving linked content."""
    root = _parse_xml(content)
    items = [element for element in root.iter() if _local_name(element.tag) == "item"]
    results: list[RawRelease] = []
    for offset, item in enumerate(items):
        title = _child_text(item, "title")
        if not title:
            continue
        attrs: dict[str, list[str]] = {}
        for child in item:
            if _local_name(child.tag) == "attr" and (
                child.tag.startswith(f"{{{_TORZNAB_NAMESPACE}}}") or "name" in child.attrib
            ):
                name = (child.get("name") or "").casefold()
                value = child.get("value")
                if name and value is not None:
                    attrs.setdefault(name, []).append(value)

        category_ids = _category_ids(item, attrs)
        categories = tuple(
            sorted(
                (
                    TorznabCategory(category_id, indexer.category_name(category_id))
                    for category_id in category_ids
                ),
                key=lambda category: category.id,
            )
        )
        link = _child_text(item, "link")
        enclosure = next(
            (child for child in item if _local_name(child.tag) == "enclosure"),
            None,
        )
        enclosure_url = enclosure.get("url") if enclosure is not None else None
        magnet = _first_attr(attrs, "magneturl")
        if magnet is None:
            for candidate in (link, enclosure_url):
                if candidate and candidate.lower().startswith("magnet:"):
                    magnet = candidate.strip()
                    break
        download_url = next(
            (
                sanitize_result_url(candidate)
                for candidate in (link, enclosure_url)
                if candidate and not candidate.lower().startswith("magnet:")
            ),
            None,
        )
        infohash = normalize_infohash(_first_attr(attrs, "infohash"))
        if infohash is None:
            infohash = magnet_btih(magnet)
        seeders = _optional_int(_first_attr(attrs, "seeders"))
        peers = _optional_int(_first_attr(attrs, "peers"))
        size = _optional_int(_child_text(item, "size"))
        if size is None and enclosure is not None:
            size = _optional_int(enclosure.get("length"))
        guid = sanitize_result_url(_child_text(item, "guid"))
        details = sanitize_result_url(_child_text(item, "comments"))
        warnings: list[str] = []
        source_name = indexer.name
        source_element = next(
            (child for child in item if _local_name(child.tag) == "jackettindexer"),
            None,
        )
        if source_element is not None:
            returned_id = (source_element.get("id") or "").strip()
            if source_element.text and source_element.text.strip():
                source_name = source_element.text.strip()
            if returned_id and returned_id != indexer.id:
                warnings.append(
                    f"Response source indexer {returned_id!r} differs from requested indexer."
                )
        results.append(
            RawRelease(
                sequence=sequence_start + offset,
                indexer_id=indexer.id,
                indexer_name=source_name,
                query=query,
                original_title=title,
                normalized_title=_normalize_release_title(title),
                guid=guid,
                download_url=download_url,
                details_url=details,
                published_at=_parse_publish_date(_child_text(item, "pubDate")),
                categories=categories,
                size_bytes=size if size is None or size >= 0 else None,
                seeders=seeders if seeders is None or seeders >= 0 else None,
                peers=peers if peers is None or peers >= 0 else None,
                infohash=infohash,
                magnet_uri=magnet.strip() if magnet else None,
                download_volume_factor=_optional_float(_first_attr(attrs, "downloadvolumefactor")),
                upload_volume_factor=_optional_float(_first_attr(attrs, "uploadvolumefactor")),
                warnings=tuple(warnings),
            )
        )
    return results


def _parse_xml(content: bytes) -> ET.Element:
    upper = content.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise JackettResponseError(
            "Jackett XML containing DTD or entity declarations was rejected."
        )
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise JackettResponseError("Jackett returned malformed XML.") from exc


def _parse_capabilities(
    root: ET.Element,
    indexer_id: str,
    name: str,
) -> IndexerCapabilities:
    searching = next(
        (element for element in root if _local_name(element.tag) == "searching"),
        None,
    )
    tvsearch = False
    search = False
    if searching is not None:
        for element in searching:
            local = _local_name(element.tag)
            available = (element.get("available") or "").casefold() in {"yes", "true", "1"}
            if local == "tv-search":
                tvsearch = available
            elif local == "search":
                search = available

    categories: list[TorznabCategory] = []
    category_root = next(
        (element for element in root if _local_name(element.tag) == "categories"),
        None,
    )
    if category_root is not None:
        for element in category_root.iter():
            if _local_name(element.tag) not in {"category", "subcat"}:
                continue
            category_id = _optional_int(element.get("id"))
            if category_id is not None:
                categories.append(TorznabCategory(category_id, element.get("name")))
    unique = {category.id: category for category in categories}
    return IndexerCapabilities(
        id=indexer_id,
        name=name,
        tvsearch_available=tvsearch,
        search_available=search,
        categories=tuple(sorted(unique.values(), key=lambda category: category.id)),
    )


def _validate_indexer_id(value: str, *, allow_all: bool = False) -> None:
    if (value.casefold() == "all" and not allow_all) or not _INDEXER_ID.fullmatch(value):
        raise JackettConfigurationError(f"Invalid Jackett indexer ID: {value!r}.")


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    if response.status_code == 429:
        value = response.headers.get("Retry-After")
        try:
            parsed = float(value) if value is not None else float(2**attempt)
            return min(max(parsed, 0.0), 30.0) if math.isfinite(parsed) else float(2**attempt)
        except ValueError:
            pass
    return float(2**attempt)


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name and child.text:
            stripped = child.text.strip()
            return stripped or None
    return None


def _first_attr(attrs: dict[str, list[str]], name: str) -> str | None:
    values = attrs.get(name)
    return values[0] if values else None


def _category_ids(item: ET.Element, attrs: dict[str, list[str]]) -> set[int]:
    values = [
        child.text
        for child in item
        if _local_name(child.tag) == "category" and child.text is not None
    ]
    values.extend(attrs.get("category", []))
    result: set[int] = set()
    for value in values:
        parsed = _optional_int(value)
        if parsed is not None:
            result.add(parsed)
    return result


def _optional_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: str | None) -> float | None:
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is None or math.isfinite(parsed) else None


def _parse_publish_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _normalize_release_title(value: str) -> str:
    from media_scope.release_classifier import normalize_release_title

    return normalize_release_title(value)
