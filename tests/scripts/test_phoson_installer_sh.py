"""scripts/phoson-installer.sh and scripts/uninstall.sh against a local fake
release (file:// URLs): verified binary install, re-install, tamper refusal,
fallback to uv when the binary cannot run, and uninstall."""

import io
import os
import json
import shutil
import hashlib
import tarfile
import platform
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX installer")

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "phoson-installer.sh"
UNINSTALLER = ROOT / "scripts" / "uninstall.sh"


def _tag() -> str:
    system = {"Darwin": "darwin", "Linux": "linux"}[platform.system()]
    machine = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }[platform.machine().lower()]
    return f"{system}-{machine}"


def _bundle(version: str, *, works: bool = True) -> bytes:
    script = (
        f"#!/bin/sh\necho phoson-cli {version}\n" if works else "#!/bin/sh\nexit 127\n"
    ).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("phoson-cli/phoson-cli")
        info.size, info.mode = len(script), 0o755
        tar.addfile(info, io.BytesIO(script))
    return buffer.getvalue()


def _publish(
    release: Path, version: str, payload: bytes, *, latest: bool = True
) -> None:
    name = f"phoson-cli-{_tag()}.tar.gz"
    targets = [release / "download" / f"v{version}"]
    if latest:
        targets.append(release / "latest" / "download")
    for target in targets:
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        (target / "SHA256SUMS").write_text(f"{digest}  {name}\n")


@pytest.fixture
def env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    return {
        "HOME": str(home),
        "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "PHOSON_RELEASE_BASE": f"file://{tmp_path / 'release'}",
        "_release": str(tmp_path / "release"),
        "_fake_bin": str(fake_bin),
    }


def _run(script: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in env.items() if not k.startswith("_")}
    return subprocess.run(
        ["sh", str(script), *args],
        env=clean,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _share(env: dict) -> Path:
    return Path(env["HOME"]) / ".local" / "share" / "phoson"


def test_installs_verified_binary_and_links_it(env) -> None:
    _publish(Path(env["_release"]), "1.0.0", _bundle("1.0.0"))

    result = _run(INSTALLER, env, "--ci")

    assert result.returncode == 0, result.stderr
    link = Path(env["HOME"]) / ".local" / "bin" / "phoson-cli"
    out = subprocess.run([str(link), "--version"], capture_output=True, text=True)
    assert out.stdout.strip() == "phoson-cli 1.0.0"
    receipt = json.loads((_share(env) / "install-receipt.json").read_text())
    assert receipt == {
        "method": "binary",
        "version": "1.0.0",
        "platform": _tag(),
        "bin_link": str(link),
    }
    assert os.readlink(_share(env) / "current") == "versions/1.0.0"


def test_pinned_version_and_reinstall_switch_current(env) -> None:
    release = Path(env["_release"])
    _publish(release, "1.0.0", _bundle("1.0.0"))
    _publish(release, "0.9.0", _bundle("0.9.0"), latest=False)
    assert _run(INSTALLER, env, "--ci").returncode == 0

    result = _run(INSTALLER, env, "--ci", "--version", "v0.9.0")

    assert result.returncode == 0, result.stderr
    assert os.readlink(_share(env) / "current") == "versions/0.9.0"
    assert sorted(p.name for p in (_share(env) / "versions").iterdir()) == [
        "0.9.0",
        "1.0.0",
    ]


def test_tampered_archive_aborts_without_fallback(env) -> None:
    release = Path(env["_release"])
    _publish(release, "1.0.0", _bundle("1.0.0"))
    archive = release / "latest" / "download" / f"phoson-cli-{_tag()}.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"x")

    result = _run(INSTALLER, env, "--ci")

    assert result.returncode == 1
    assert "Checksum mismatch" in result.stderr
    assert not (_share(env) / "current").exists()


def test_falls_back_to_uv_when_the_binary_cannot_run(env) -> None:
    _publish(Path(env["_release"]), "1.0.0", _bundle("1.0.0", works=False))
    log = Path(env["_fake_bin"]) / "uv.log"
    uv = Path(env["_fake_bin"]) / "uv"
    uv.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    uv.chmod(0o755)

    result = _run(INSTALLER, env, "--ci")

    assert result.returncode == 0, result.stderr
    assert "does not run on this system" in result.stderr
    assert (
        "tool install --upgrade --python 3.12 phoson-engine-minimal" in log.read_text()
    )
    assert not (_share(env) / "current").exists()


def test_download_failure_aborts_instead_of_falling_back(env) -> None:
    """A missing release asset (network/GitHub error) must not switch the
    install to PyPI: a transient 504 would otherwise swap a working binary
    install for a uv one (and uv refuses to overwrite the existing link)."""
    log = Path(env["_fake_bin"]) / "uv.log"
    uv = Path(env["_fake_bin"]) / "uv"
    uv.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    uv.chmod(0o755)
    env["PHOSON_RELEASE_BASE"] = (Path(env["_release"]) / "missing").as_uri()

    result = _run(INSTALLER, env, "--ci")

    assert result.returncode != 0
    assert "network or GitHub error" in result.stderr
    assert "Falling back" not in result.stderr
    assert not log.exists()


def test_explicit_binary_method_does_not_fall_back(env) -> None:
    result = _run(INSTALLER, env, "--ci", "--method", "binary")
    assert result.returncode == 1
    assert "re-run with --method uv" in result.stderr


def test_uninstall_script_removes_binary_install(env) -> None:
    _publish(Path(env["_release"]), "1.0.0", _bundle("1.0.0"))
    assert _run(INSTALLER, env, "--ci").returncode == 0
    (Path(env["HOME"]) / ".phoson").mkdir()

    result = _run(UNINSTALLER, env)

    assert result.returncode == 0, result.stderr
    assert not _share(env).exists()
    assert not os.path.lexists(Path(env["HOME"]) / ".local" / "bin" / "phoson-cli")
    assert (Path(env["HOME"]) / ".phoson").is_dir()  # kept without --purge
    assert _run(UNINSTALLER, env, "--purge").returncode == 0
    assert not (Path(env["HOME"]) / ".phoson").exists()


@pytest.mark.skipif(
    shutil.which("shellcheck") is None, reason="shellcheck not installed"
)
def test_shellcheck() -> None:
    result = subprocess.run(
        ["shellcheck", "-s", "sh", "-S", "warning", str(INSTALLER), str(UNINSTALLER)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout
