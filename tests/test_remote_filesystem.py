"""SFTP connection and path-operation tests without a live seedbox."""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from media_scope.exceptions import SeedboxFilesystemError
from media_scope.remote_filesystem import SftpRemoteFilesystem


class FakeSftp:
    def normalize(self, value: str) -> str:
        return "/home/seedboxer1" if value == "." else value

    def close(self) -> None:
        pass


class FakeClient:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.connected: dict[str, Any] | None = None
        self.policy: object | None = None

    def load_system_host_keys(self) -> None:
        pass

    def load_host_keys(self, _path: str) -> None:
        pass

    def set_missing_host_key_policy(self, policy: object) -> None:
        self.policy = policy

    def connect(self, **kwargs: Any) -> None:
        if self.failure:
            raise self.failure
        self.connected = kwargs

    def open_sftp(self) -> FakeSftp:
        return FakeSftp()

    def close(self) -> None:
        pass


def install_fake_paramiko(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    class AuthenticationException(Exception):
        pass

    class BadHostKeyException(Exception):
        pass

    module = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=object,
        AuthenticationException=AuthenticationException,
        BadHostKeyException=BadHostKeyException,
    )
    monkeypatch.setitem(sys.modules, "paramiko", module)


def test_sftp_requires_complete_configuration() -> None:
    with pytest.raises(SeedboxFilesystemError) as captured:
        SftpRemoteFilesystem("", port=22, username="user", password="secret")
    assert captured.value.error_code == "SFTP_CONFIGURATION_ERROR"
    assert "secret" not in str(captured.value)


def test_sftp_uses_password_auth_and_verified_host_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeClient()
    install_fake_paramiko(monkeypatch, client)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("seedbox ssh-ed25519 AAAA", encoding="utf-8")
    filesystem = SftpRemoteFilesystem(
        "seedbox.test",
        port=22,
        username="user",
        password="secret",
        known_hosts=known_hosts,
    )
    assert filesystem.home() == PurePosixPath("/home/seedboxer1")
    assert client.connected is not None
    assert client.connected["look_for_keys"] is False
    assert client.connected["allow_agent"] is False
    assert client.connected["password"] == "secret"
    assert client.policy is not None


def test_sftp_authentication_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    class AuthenticationException(Exception):
        pass

    client = FakeClient(AuthenticationException("secret"))
    module = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=object,
        AuthenticationException=AuthenticationException,
        BadHostKeyException=type("BadHostKeyException", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "paramiko", module)
    filesystem = SftpRemoteFilesystem("seedbox.test", port=22, username="user", password="secret")
    with pytest.raises(SeedboxFilesystemError) as captured:
        filesystem.home()
    assert captured.value.error_code == "SFTP_AUTHENTICATION_FAILED"
    assert "secret" not in str(captured.value)


def test_sftp_rejects_an_unknown_host_key(monkeypatch: pytest.MonkeyPatch) -> None:
    class SSHException(Exception):
        pass

    client = FakeClient(SSHException("Server not found in known_hosts"))
    module = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=object,
        AuthenticationException=type("AuthenticationException", (Exception,), {}),
        BadHostKeyException=type("BadHostKeyException", (Exception,), {}),
        SSHException=SSHException,
    )
    monkeypatch.setitem(sys.modules, "paramiko", module)
    filesystem = SftpRemoteFilesystem("seedbox.test", port=22, username="user", password="secret")
    with pytest.raises(SeedboxFilesystemError) as captured:
        filesystem.home()
    assert captured.value.error_code == "SFTP_HOST_KEY_ERROR"
