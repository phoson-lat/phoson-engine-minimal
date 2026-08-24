"""Unit tests for AttachmentManager size limit and compat warnings (#54)."""

from pathlib import Path

import pytest

from phoson_cli.attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentManager,
    provider_compat_warning,
)


def test_attach_rejects_oversized_file(tmp_path: Path) -> None:
    big = tmp_path / "big.png"
    big.write_bytes(b"\0" * (MAX_ATTACHMENT_BYTES + 1))

    mgr = AttachmentManager()
    with pytest.raises(ValueError, match="too large"):
        mgr.attach(str(big))
    assert len(mgr) == 0


def test_attach_accepts_file_under_limit(tmp_path: Path) -> None:
    img = tmp_path / "ok.png"
    img.write_bytes(b"x" * (MAX_ATTACHMENT_BYTES - 1))

    mgr = AttachmentManager()
    mgr.attach(str(img))
    assert len(mgr) == 1


def test_compat_warning_video() -> None:
    warning = provider_compat_warning(".mp4")
    assert warning is not None and "placeholder" in warning


def test_compat_warning_pdf() -> None:
    warning = provider_compat_warning(".pdf", active_provider="openrouter")
    assert warning is not None and "openrouter" in warning


def test_compat_warning_none_for_safe_type() -> None:
    assert provider_compat_warning(".png") is None
