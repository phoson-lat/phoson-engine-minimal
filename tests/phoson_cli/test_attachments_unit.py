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


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("report.pdf", "application/pdf"),
        (
            "memo.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("data.csv", "text/csv"),
        ("notes.md", "text/markdown"),
    ],
)
def test_attach_documents_carry_mime_and_filename(
    tmp_path: Path, name: str, mime: str
) -> None:
    from phoson_llm.schemas import DocumentBlock

    f = tmp_path / name
    f.write_bytes(b"x")
    mgr = AttachmentManager()
    mgr.attach(str(f))
    (block,) = mgr.flush()
    assert isinstance(block, DocumentBlock)
    assert block.media_type == mime
    assert block.filename == name


def test_attach_rejects_unknown_type(tmp_path: Path) -> None:
    f = tmp_path / "blob.xyz"
    f.write_bytes(b"x")
    with pytest.raises(ValueError, match="Unsupported file type"):
        AttachmentManager().attach(str(f))


def test_compat_warning_office_file() -> None:
    warning = provider_compat_warning(".docx")
    assert warning is not None and "Bedrock" in warning
