"""Private uv provisioning for plugin installs from the standalone binary."""

import io
import os
import sys
import hashlib
import tarfile
import zipfile

import pytest

from phoson_cli import managed_uv
from phoson_cli.managed_uv import (
    ManagedUvError,
    find_uv,
    ensure_uv,
    managed_uv_path,
    install_managed_uv,
)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOSON_HOME", str(tmp_path / ".phoson"))
    monkeypatch.delenv("PHOSON_NO_MANAGED_UV", raising=False)
    monkeypatch.setattr(managed_uv.shutil, "which", lambda _n: None)
    return tmp_path


def _tarball(payload: bytes = b"#!uv") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in [("uv-x/uvx", b"uvx"), ("uv-x/uv", payload)]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zipball(payload: bytes = b"MZuv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("uvx.exe", b"uvx")
        archive.writestr("uv.exe", payload)
    return buf.getvalue()


def _pin(monkeypatch, asset: str, data: bytes) -> None:
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(managed_uv, "platform_asset", lambda: (asset, digest))


def test_every_release_platform_has_a_pinned_asset() -> None:
    assert set(managed_uv._ASSETS) == {
        ("darwin", "arm64"),
        ("darwin", "x86_64"),
        ("linux", "x86_64"),
        ("linux", "arm64"),
        ("win32", "x86_64"),
    }
    for asset, digest in managed_uv._ASSETS.values():
        assert asset.startswith("uv-")
        assert len(digest) == 64 and int(digest, 16) >= 0


def test_installs_verified_binary(monkeypatch) -> None:
    data = _tarball(b"#!real-uv")
    _pin(monkeypatch, "uv-aarch64-apple-darwin.tar.gz", data)
    urls: list[str] = []
    notes: list[str] = []

    path = install_managed_uv(
        download=lambda url: urls.append(url) or data, notify=notes.append
    )

    assert path == str(managed_uv_path())
    with open(path, "rb") as handle:
        assert handle.read() == b"#!real-uv"
    if sys.platform != "win32":
        assert os.stat(path).st_mode & 0o111
    assert urls == [
        "https://github.com/astral-sh/uv/releases/download/"
        f"{managed_uv.UV_VERSION}/uv-aarch64-apple-darwin.tar.gz"
    ]
    assert notes and managed_uv.UV_VERSION in notes[0]
    assert find_uv() == path


def test_extracts_from_windows_zip(monkeypatch) -> None:
    data = _zipball(b"MZreal")
    _pin(monkeypatch, "uv-x86_64-pc-windows-msvc.zip", data)
    path = install_managed_uv(download=lambda _url: data)
    with open(path, "rb") as handle:
        assert handle.read() == b"MZreal"


def test_checksum_mismatch_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(managed_uv, "platform_asset", lambda: ("uv-x.tar.gz", "0" * 64))
    with pytest.raises(ManagedUvError, match="Checksum mismatch"):
        install_managed_uv(download=lambda _url: _tarball())
    assert not managed_uv_path().exists()


def test_download_failure_is_reported(monkeypatch) -> None:
    _pin(monkeypatch, "uv-x.tar.gz", b"")

    def boom(_url: str) -> bytes:
        raise OSError("offline")

    with pytest.raises(ManagedUvError, match="offline"):
        install_managed_uv(download=boom)


def test_unsupported_platform(monkeypatch) -> None:
    monkeypatch.setattr(managed_uv, "platform_asset", lambda: None)
    with pytest.raises(ManagedUvError, match="install uv yourself"):
        install_managed_uv(download=lambda _url: b"")


def test_ensure_prefers_path_then_private_copy(monkeypatch) -> None:
    monkeypatch.setattr(managed_uv.shutil, "which", lambda _n: "/usr/bin/uv")
    assert ensure_uv(download=lambda _u: pytest.fail("no download")) == "/usr/bin/uv"

    monkeypatch.setattr(managed_uv.shutil, "which", lambda _n: None)
    target = managed_uv_path()
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    assert ensure_uv(download=lambda _u: pytest.fail("no download")) == str(target)


def test_opt_out_skips_download(monkeypatch) -> None:
    monkeypatch.setenv("PHOSON_NO_MANAGED_UV", "1")
    with pytest.raises(ManagedUvError, match="PHOSON_NO_MANAGED_UV"):
        ensure_uv(download=lambda _u: pytest.fail("no download"))
