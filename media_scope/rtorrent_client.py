"""Secret-safe rTorrent XML-RPC client and method compatibility layer."""

from __future__ import annotations

import logging
import re
import xmlrpc.client
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from media_scope.download_models import (
    DownloadCapabilities,
    DownloadSnapshot,
)
from media_scope.exceptions import (
    MagnetSubmissionUnsupportedError,
    RtorrentAuthenticationError,
    RtorrentConfigurationError,
    RtorrentMethodError,
    RtorrentRpcError,
    RtorrentRpcFault,
)
from media_scope.probe_models import RtorrentCapabilities, TorrentStatus

LOGGER = logging.getLogger("media_scope.probe")
_KNOWN_METHODS = frozenset(
    {
        "system.client_version",
        "system.library_version",
        "system.api_version",
        "d.name",
        "d.hash",
        "d.get_hash",
        "d.is_meta",
        "d.size_files",
        "d.size_bytes",
        "d.peers_connected",
        "d.peers_complete",
        "d.message",
        "d.state",
        "d.is_active",
        "d.is_open",
        "d.complete",
        "d.completed_bytes",
        "d.left_bytes",
        "d.down.rate",
        "d.up.rate",
        "d.up.total",
        "d.base_path",
        "d.directory",
        "d.ratio",
        "d.hashing",
        "d.directory.set",
        "d.custom",
        "d.custom.set",
        "d.start",
        "d.stop",
        "d.erase",
        "load.start_verbose",
        "load.start",
        "load.normal_verbose",
        "load.normal",
    }
)
_START_LOAD_METHODS = ("load.start_verbose", "load.start")
_NORMAL_LOAD_METHODS = ("load.normal_verbose", "load.normal")


class RtorrentClient:
    """Call rTorrent through one complete user-supplied HTTP(S) XML-RPC URL."""

    def __init__(
        self,
        rpc_url: str,
        *,
        username: str = "",
        password: str = "",
        verify_tls: bool = True,
        timeout_seconds: float = 15,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.rpc_url = _validate_rpc_url(rpc_url)
        self.sanitized_endpoint = sanitize_rpc_endpoint(self.rpc_url)
        if timeout_seconds <= 0:
            raise RtorrentConfigurationError("RTORRENT_RPC_TIMEOUT_SECONDS must be positive.")
        auth = httpx.BasicAuth(username, password) if username or password else None
        self._client = httpx.Client(
            auth=auth,
            verify=verify_tls,
            timeout=timeout_seconds,
            transport=transport,
            headers={"Content-Type": "text/xml", "Accept": "text/xml"},
        )
        self.capabilities: RtorrentCapabilities | None = None
        self.download_capabilities: DownloadCapabilities | None = None
        if not verify_tls and urlsplit(self.rpc_url).scheme == "https":
            LOGGER.warning("TLS certificate verification is disabled for the rTorrent gateway.")

    def __enter__(self) -> RtorrentClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP transport."""
        self._client.close()

    def call(self, method: str, *params: object) -> object:
        """Encode, send, and decode one XML-RPC request without leaking credentials."""
        request_body = xmlrpc.client.dumps(params, methodname=method, allow_none=True).encode()
        try:
            response = self._client.post(self.rpc_url, content=request_body)
        except httpx.TimeoutException as exc:
            raise RtorrentRpcError("The rTorrent RPC request timed out.") from exc
        except httpx.HTTPError as exc:
            raise RtorrentRpcError("The rTorrent RPC gateway could not be reached.") from exc
        if response.status_code in (401, 403):
            raise RtorrentAuthenticationError("The rTorrent RPC gateway rejected authentication.")
        if response.status_code >= 400:
            raise RtorrentRpcError(
                f"The rTorrent RPC gateway returned HTTP {response.status_code}."
            )
        try:
            values, _method = xmlrpc.client.loads(response.content)
        except xmlrpc.client.Fault as exc:
            message = _sanitize_fault(exc.faultString)
            raise RtorrentRpcFault(message, fault_code=exc.faultCode) from exc
        except Exception as exc:
            raise RtorrentRpcError("The rTorrent RPC gateway returned malformed XML-RPC.") from exc
        if not values:
            return None
        return values[0] if len(values) == 1 else values

    def discover_capabilities(self) -> RtorrentCapabilities:
        """Discover versions and select supported load and metadata operations."""
        methods = self._discover_methods()
        versions: dict[str, str | None] = {}
        for name, method in (
            ("client", "system.client_version"),
            ("library", "system.library_version"),
            ("api", "system.api_version"),
        ):
            versions[name] = self._optional_text(method, methods)

        missing = {"d.name", "d.directory.set", "d.custom.set", "d.stop", "d.erase"} - methods
        if missing:
            joined = ", ".join(sorted(missing))
            raise RtorrentMethodError(f"Connected rTorrent lacks required methods: {joined}.")

        load_method = next((value for value in _START_LOAD_METHODS if value in methods), None)
        load_starts = True
        if load_method is None:
            load_method = next((value for value in _NORMAL_LOAD_METHODS if value in methods), None)
            load_starts = False
        if load_method is None or (not load_starts and "d.start" not in methods):
            raise MagnetSubmissionUnsupportedError(
                "Connected rTorrent exposes no supported RPC magnet-submission operation."
            )
        if "d.is_meta" in methods:
            detection = "d.is_meta"
        elif {"d.size_files", "d.size_bytes"} <= methods:
            detection = "compatibility_fallback"
        else:
            raise RtorrentMethodError(
                "Connected rTorrent cannot report metadata state with a supported method."
            )
        self.capabilities = RtorrentCapabilities(
            client_version=versions["client"],
            library_version=versions["library"],
            api_version=versions["api"],
            methods=frozenset(methods),
            load_method=load_method,
            load_starts=load_starts,
            metadata_detection_method=detection,
        )
        return self.capabilities

    def discover_download_capabilities(self) -> DownloadCapabilities:
        """Discover the read/write operations required by the Step 6 controller."""
        methods = self._discover_methods()
        versions: dict[str, str | None] = {}
        for name, method in (
            ("client", "system.client_version"),
            ("library", "system.library_version"),
            ("api", "system.api_version"),
        ):
            versions[name] = self._optional_text(method, methods)
        hash_method = next((name for name in ("d.hash", "d.get_hash") if name in methods), None)
        required = {
            "d.name",
            "d.size_bytes",
            "d.completed_bytes",
            "d.directory",
            "d.base_path",
            "d.directory.set",
            "d.custom",
            "d.custom.set",
            "d.start",
            "d.stop",
        }
        missing = required - methods
        if hash_method is None:
            missing.add("d.hash")
        if missing:
            joined = ", ".join(sorted(missing))
            raise RtorrentMethodError(
                f"Connected rTorrent lacks required download methods: {joined}."
            )
        if "d.is_meta" in methods:
            detection = "d.is_meta"
        elif "d.size_files" in methods:
            detection = "compatibility_fallback"
        else:
            raise RtorrentMethodError(
                "Connected rTorrent cannot confirm that torrent metadata is available."
            )
        self.download_capabilities = DownloadCapabilities(
            client_version=versions["client"],
            library_version=versions["library"],
            api_version=versions["api"],
            methods=frozenset(methods),
            metadata_detection_method=detection,
            hash_method=hash_method or "d.hash",
        )
        return self.download_capabilities

    def torrent_exists(self, infohash: str) -> bool:
        """Return whether a torrent hash is present without changing it."""
        try:
            self.call("d.name", infohash.upper())
        except RtorrentRpcFault:
            return False
        return True

    def submit_magnet(self, magnet_uri: str, infohash: str, directory: Path) -> str:
        """Load and start a magnet using the discovered compatible method."""
        capabilities = self._require_capabilities()
        method = capabilities.load_method
        if method is None:
            raise MagnetSubmissionUnsupportedError("No compatible magnet load method was selected.")
        directory_value = str(directory)
        if any(value in directory_value for value in ("\n", "\r", ",")):
            raise RtorrentConfigurationError("Probe directory contains an unsafe character.")
        self.call(method, "", magnet_uri, f"d.directory.set={directory_value}")
        if not capabilities.load_starts:
            self.call("d.start", infohash.upper())
        return method

    def tag_probe(self, infohash: str, *, job_id: str, state: str, rank: int | str) -> None:
        """Apply named ownership fields to a newly created probe torrent."""
        target = infohash.upper()
        self.call("d.custom.set", target, "media_probe_job_id", job_id)
        self.call("d.custom.set", target, "media_probe_state", state)
        self.call("d.custom.set", target, "media_probe_original_rank", str(rank))

    def set_probe_state(self, infohash: str, state: str) -> None:
        """Update the named probe state on an owned torrent."""
        self.call("d.custom.set", infohash.upper(), "media_probe_state", state)

    def status(self, infohash: str) -> TorrentStatus:
        """Read content-agnostic metadata and peer state for one torrent."""
        capabilities = self._require_capabilities()
        target = infohash.upper()
        detection = capabilities.metadata_detection_method
        if detection == "d.is_meta":
            metadata_retrieved = _integer(self.call("d.is_meta", target)) == 0
        elif detection == "compatibility_fallback":
            name = str(self.call("d.name", target)).strip()
            files = _integer(self.call("d.size_files", target))
            size = _integer(self.call("d.size_bytes", target))
            meta_placeholder = f"{target}.meta"
            metadata_retrieved = (
                name.casefold() != meta_placeholder.casefold() and files > 0 and size > 0
            )
        else:
            raise RtorrentMethodError("No metadata detection method is available.")
        methods = capabilities.methods
        connected = self._optional_integer("d.peers_connected", target, methods)
        complete = self._optional_integer("d.peers_complete", target, methods)
        message = self._optional_status_message(target, methods)
        return TorrentStatus(metadata_retrieved, detection, connected, complete, message)

    def download_snapshot(self, infohash: str) -> DownloadSnapshot:
        """Read a normalized full-download status snapshot without changing the item."""
        capabilities = self._require_download_capabilities()
        target = infohash.upper()
        methods = capabilities.methods
        actual_hash = str(self.call(capabilities.hash_method, target)).strip().lower()
        name = str(self.call("d.name", target)).strip()
        size = max(_integer(self.call("d.size_bytes", target)), 0)
        completed = max(_integer(self.call("d.completed_bytes", target)), 0)
        if capabilities.metadata_detection_method == "d.is_meta":
            metadata = _integer(self.call("d.is_meta", target)) == 0
        else:
            files = max(_integer(self.call("d.size_files", target)), 0)
            metadata = bool(
                name and files > 0 and size > 0 and name.casefold() != f"{target}.meta".casefold()
            )
        left = self._optional_integer("d.left_bytes", target, methods)
        if "d.left_bytes" not in methods:
            left = max(size - completed, 0)
        complete = bool(self._optional_integer("d.complete", target, methods))
        if "d.complete" not in methods:
            complete = size > 0 and completed >= size and left == 0
        return DownloadSnapshot(
            infohash=actual_hash,
            name=name,
            metadata_retrieved=metadata,
            state=self._optional_integer("d.state", target, methods),
            is_active=bool(self._optional_integer("d.is_active", target, methods)),
            is_open=bool(self._optional_integer("d.is_open", target, methods)),
            complete=complete,
            completed_bytes=completed,
            size_bytes=size,
            left_bytes=left,
            download_rate=self._optional_integer("d.down.rate", target, methods),
            upload_rate=self._optional_integer("d.up.rate", target, methods),
            uploaded_bytes=self._optional_integer("d.up.total", target, methods),
            connected_peers=self._optional_integer("d.peers_connected", target, methods),
            complete_peers=self._optional_integer("d.peers_complete", target, methods),
            message=self._optional_status_message(target, methods),
            base_path=str(self.call("d.base_path", target)).strip(),
            directory=str(self.call("d.directory", target)).strip(),
            ratio=self._optional_integer("d.ratio", target, methods),
            hashing=bool(self._optional_integer("d.hashing", target, methods)),
        )

    def set_download_directory(self, infohash: str, directory: Path) -> None:
        """Register one already-validated permanent directory on an existing item."""
        value = str(directory)
        if any(character in value for character in ("\x00", "\n", "\r", ",")):
            raise RtorrentConfigurationError("Permanent download directory is unsafe for RPC.")
        self.call("d.directory.set", infohash.upper(), value)

    def tag_download(
        self,
        infohash: str,
        *,
        job_id: str,
        state: str,
        source: str,
        tmdb_id: int | str,
    ) -> None:
        """Set only Step 6's named custom fields, leaving all other fields untouched."""
        target = infohash.upper()
        values = {
            "media_download_job_id": job_id,
            "media_download_state": state,
            "media_download_source": source,
            "media_download_tmdb_id": str(tmdb_id),
        }
        for name, value in values.items():
            self.call("d.custom.set", target, name, value)

    def set_download_state(self, infohash: str, state: str) -> None:
        """Update only Step 6's named state field."""
        self.call("d.custom.set", infohash.upper(), "media_download_state", state)

    def get_custom(self, infohash: str, name: str) -> str | None:
        """Read one named custom field without touching legacy custom slots."""
        value = self.call("d.custom", infohash.upper(), name)
        text = str(value or "").strip()
        return text or None

    def start(self, infohash: str) -> None:
        """Start or resume one existing torrent."""
        self.call("d.start", infohash.upper())

    def stop(self, infohash: str) -> None:
        """Stop one torrent."""
        self.call("d.stop", infohash.upper())

    def erase(self, infohash: str) -> None:
        """Erase one torrent from rTorrent without claiming data deletion."""
        self.call("d.erase", infohash.upper())

    def _discover_methods(self) -> set[str]:
        try:
            listed = self.call("system.listMethods")
        except RtorrentRpcFault:
            listed = None
        if isinstance(listed, list | tuple):
            return {str(value) for value in listed}
        supported: set[str] = set()
        for method in _KNOWN_METHODS:
            try:
                exists = self.call("system.methodExist", method)
            except RtorrentRpcFault as exc:
                raise RtorrentMethodError(
                    "rTorrent supports neither system.listMethods nor system.methodExist."
                ) from exc
            if bool(exists):
                supported.add(method)
        return supported

    def _optional_text(self, method: str, methods: set[str]) -> str | None:
        if method not in methods:
            return None
        value = self.call(method)
        return None if value is None else str(value)

    def _optional_integer(self, method: str, target: str, methods: frozenset[str]) -> int:
        if method not in methods:
            return 0
        try:
            return max(_integer(self.call(method, target)), 0)
        except RtorrentRpcFault:
            return 0

    def _optional_status_message(self, target: str, methods: frozenset[str]) -> str | None:
        if "d.message" not in methods:
            return None
        try:
            value = self.call("d.message", target)
        except RtorrentRpcFault:
            return None
        text = _sanitize_untrusted_message(str(value or ""))
        return text[:500] or None

    def _require_capabilities(self) -> RtorrentCapabilities:
        if self.capabilities is None:
            raise RtorrentMethodError("rTorrent capabilities have not been discovered.")
        return self.capabilities

    def _require_download_capabilities(self) -> DownloadCapabilities:
        if self.download_capabilities is None:
            raise RtorrentMethodError("rTorrent download capabilities have not been discovered.")
        return self.download_capabilities


def sanitize_rpc_endpoint(value: str) -> str:
    """Remove user information, query data, and fragments from an RPC URL."""
    parts = urlsplit(value)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _validate_rpc_url(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise RtorrentConfigurationError(
            "RTORRENT_RPC_URL is missing; configure the complete HTTP(S) RPC gateway URL."
        )
    parts = urlsplit(stripped)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise RtorrentConfigurationError("RTORRENT_RPC_URL must be a complete HTTP(S) URL.")
    if parts.username is not None or parts.password is not None:
        raise RtorrentConfigurationError(
            "Put RPC credentials in the username/password settings, not in RTORRENT_RPC_URL."
        )
    return stripped


def _sanitize_fault(value: str) -> str:
    text = _sanitize_untrusted_message(value)
    return (
        f"rTorrent returned an XML-RPC fault: {text[:300]}"
        if text
        else ("rTorrent returned an XML-RPC fault.")
    )


def _sanitize_untrusted_message(value: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    return re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", "[redacted-url]", text, flags=re.IGNORECASE)


def _integer(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RtorrentRpcError("rTorrent returned a non-integer status value.") from exc
