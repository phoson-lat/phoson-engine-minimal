"""Standalone-binary layout: discovery, verified self-update and uninstall."""

import io
import os
import json
import hashlib
import tarfile
from pathlib import Path

import pytest

from phoson_cli.binary_install import (
    BinaryInstallError,
    asset_name,
    platform_tag,
    extract_bundle,
    uninstall_binary,
    download_verified,
    find_binary_install,
    update_binary_install,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Unix layout")
TAG = "linux-x86_64"


def _archive(version: str, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        files = {
            "phoson-cli/phoson-cli": f"#!/bin/sh\necho phoson-cli {version}\n".encode(),
            "phoson-cli/_internal/lib.txt": b"lib",
            **(extra or {}),
        }
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _release(version: str, payload: bytes, *, checksum: str | None = None):
    name = asset_name(TAG)
    digest = checksum or hashlib.sha256(payload).hexdigest()
    files = {
        f"v{version}/SHA256SUMS": f"{digest}  {name}\n".encode(),
        f"v{version}/{name}": payload,
    }
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        key = "/".join(url.split("/")[-2:])
        if key not in files:
            raise BinaryInstallError(f"Not found: {url}")
        return files[key]

    fetch.seen = seen  # type: ignore[attr-defined]
    return fetch


def _installed(tmp_path: Path, version: str = "1.0.0") -> tuple[Path, Path]:
    """Lay out an installer-made install; return (root, launcher path)."""
    root = tmp_path / "share" / "phoson"
    extract_bundle(_archive(version), TAG, root / "versions" / version)
    (root / "current").symlink_to(Path("versions") / version)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "phoson-cli"
    link.symlink_to(root / "current" / "phoson-cli" / "phoson-cli")
    receipt = {
        "method": "binary",
        "version": version,
        "platform": TAG,
        "bin_link": str(link),
    }
    (root / "install-receipt.json").write_text(json.dumps(receipt))
    return root, link


def test_platform_tags() -> None:
    assert platform_tag("Darwin", "arm64") == "darwin-arm64"
    assert platform_tag("Linux", "aarch64") == "linux-arm64"
    assert platform_tag("Windows", "AMD64") == "windows-x86_64"
    assert asset_name("windows-x86_64") == "phoson-cli-windows-x86_64.zip"
    assert asset_name("darwin-arm64") == "phoson-cli-darwin-arm64.tar.gz"
    with pytest.raises(BinaryInstallError):
        platform_tag("SunOS", "sparc")


def test_find_install_through_the_bin_link(tmp_path: Path) -> None:
    root, link = _installed(tmp_path)
    install = find_binary_install(str(link))  # resolves the symlinks
    assert install is not None and install.root == root.resolve()
    assert install.version == "1.0.0"


def test_hand_downloaded_binary_is_not_managed(tmp_path: Path) -> None:
    exe = tmp_path / "phoson-cli"
    exe.write_text("")
    assert find_binary_install(str(exe)) is None


def test_update_switches_current_and_keeps_previous(tmp_path: Path) -> None:
    root, link = _installed(tmp_path)
    install = find_binary_install(str(link))
    assert install is not None

    update_binary_install(install, "1.1.0", fetch=_release("1.1.0", _archive("1.1.0")))

    assert os.readlink(root / "current") == "versions/1.1.0"
    assert (link.resolve().read_text()).endswith("phoson-cli 1.1.0\n")
    assert json.loads((root / "install-receipt.json").read_text())["version"] == "1.1.0"
    assert sorted(p.name for p in (root / "versions").iterdir()) == ["1.0.0", "1.1.0"]

    newer = find_binary_install(str(link))
    assert newer is not None
    update_binary_install(newer, "1.2.0", fetch=_release("1.2.0", _archive("1.2.0")))
    assert sorted(p.name for p in (root / "versions").iterdir()) == ["1.1.0", "1.2.0"]


def test_checksum_mismatch_changes_nothing(tmp_path: Path) -> None:
    root, link = _installed(tmp_path)
    install = find_binary_install(str(link))
    assert install is not None
    fetch = _release("1.1.0", _archive("1.1.0"), checksum="0" * 64)

    with pytest.raises(BinaryInstallError, match="Checksum mismatch"):
        update_binary_install(install, "1.1.0", fetch=fetch)

    assert os.readlink(root / "current") == "versions/1.0.0"
    assert [p.name for p in (root / "versions").iterdir()] == ["1.0.0"]


def test_missing_release_assets_explain_themselves() -> None:
    with pytest.raises(BinaryInstallError, match="no prebuilt binary for linux-x86_64"):
        download_verified("9.9.9", TAG, fetch=_release("1.0.0", b""))


def test_unsafe_archives_are_rejected(tmp_path: Path) -> None:
    payload = _archive("1.0.0", extra={"../escape.txt": b"x"})
    with pytest.raises(BinaryInstallError, match="Unsafe path"):
        extract_bundle(payload, TAG, tmp_path / "dest")
    assert not (tmp_path / "escape.txt").exists()


def test_uninstall_removes_link_and_root(tmp_path: Path) -> None:
    root, link = _installed(tmp_path)
    install = find_binary_install(str(link))
    assert install is not None

    outcome = uninstall_binary(install)

    assert outcome.removed_root and outcome.removed_link == link
    assert not root.exists() and not os.path.lexists(link)


def test_uninstall_leaves_foreign_links_alone(tmp_path: Path) -> None:
    root, link = _installed(tmp_path)
    link.unlink()
    link.symlink_to(tmp_path / "somewhere-else")
    install = find_binary_install(str(root / "current" / "phoson-cli" / "phoson-cli"))
    assert install is not None

    outcome = uninstall_binary(install)

    assert outcome.removed_link is None and os.path.lexists(link)
