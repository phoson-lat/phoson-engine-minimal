"""DocumentBlock (files) conversion across adapters.

Each adapter must send a document natively where its API accepts that type,
inline text-like files as text otherwise, and never silently drop a file:
unsupported types and missing local files become a visible placeholder.
"""

import base64

import pytest

from phoson_llm.chats import ollama as ollama_module
from phoson_llm.chats import chatgpt as chatgpt_module
from phoson_llm.chats import anthropic as anthropic_module
from phoson_llm.chats import _openai_compatible as openai_module
from phoson_llm.utils import (
    PDF_MIME,
    MAX_DOCUMENT_TEXT_CHARS,
    document_as_text,
    document_data_url,
    document_filename,
    is_text_media_type,
    document_media_type,
)
from phoson_llm.schemas import Message, TextBlock, DocumentBlock
from phoson_llm.chats.code_assist import _block_part

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return p


@pytest.fixture
def csv(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    return p


@pytest.fixture
def docx(tmp_path):
    p = tmp_path / "memo.docx"
    p.write_bytes(b"PK\x03\x04fake-docx")
    return p


def _msg(block: DocumentBlock) -> Message:
    return Message(role="user", content=[block])


# ─── Shared helpers ──────────────────────────────────────────────────────────


class TestDocumentHelpers:
    @pytest.mark.parametrize(
        ("block", "expected"),
        [
            (DocumentBlock(source="https://x.com/doc"), PDF_MIME),
            (DocumentBlock(source="file:///a/report.pdf"), PDF_MIME),
            (DocumentBlock(source="file:///a/data.csv"), "text/csv"),
            (DocumentBlock(source="file:///a/memo.docx"), DOCX_MIME),
            (DocumentBlock(source="file:///a/blob.xyz"), "application/octet-stream"),
            (DocumentBlock(source="data:text/plain;base64,aGk="), "text/plain"),
            (
                DocumentBlock(source="file:///a/x.bin", media_type="text/csv"),
                "text/csv",
            ),
        ],
    )
    def test_media_type(self, block, expected):
        assert document_media_type(block) == expected

    def test_filename_explicit_then_path_then_fallback(self):
        assert document_filename(DocumentBlock(source="file:///a/b.pdf")) == "b.pdf"
        assert (
            document_filename(DocumentBlock(source="file:///a/b.pdf", filename="x"))
            == "x"
        )
        assert (
            document_filename(DocumentBlock(source="data:application/pdf;base64,AA"))
            == "document.pdf"
        )

    def test_is_text_media_type(self):
        assert is_text_media_type("text/csv")
        assert is_text_media_type("application/json")
        assert not is_text_media_type(PDF_MIME)
        assert not is_text_media_type(DOCX_MIME)

    def test_as_text_inlines_text_file(self, csv):
        text = document_as_text(DocumentBlock(source=f"file://{csv}"))
        assert text == "[File: data.csv]\na,b\n1,2\n"

    def test_as_text_reads_inline_data_url(self):
        b64 = base64.b64encode(b"hello").decode()
        block = DocumentBlock(source=f"data:text/plain;base64,{b64}", filename="h.txt")
        assert document_as_text(block) == "[File: h.txt]\nhello"

    def test_as_text_caps_huge_file(self, tmp_path):
        p = tmp_path / "big.txt"
        p.write_text("x" * (MAX_DOCUMENT_TEXT_CHARS * 2), encoding="utf-8")
        text = document_as_text(DocumentBlock(source=f"file://{p}"))
        assert len(text) < MAX_DOCUMENT_TEXT_CHARS + 200
        assert "chars truncated" in text

    def test_as_text_binary_gets_visible_note(self, docx):
        text = document_as_text(DocumentBlock(source=f"file://{docx}"))
        assert "memo.docx" in text
        assert "not readable by this provider" in text

    def test_as_text_missing_file_is_i119_placeholder(self, tmp_path):
        block = DocumentBlock(source=f"file://{tmp_path / 'gone.csv'}")
        assert document_as_text(block) == "[document no longer available: gone.csv]"

    def test_data_url(self, pdf):
        url = document_data_url(DocumentBlock(source=f"file://{pdf}"))
        assert url == "data:application/pdf;base64," + base64.b64encode(
            b"%PDF-1.4 fake"
        ).decode("ascii")


# ─── OpenAI-compatible ───────────────────────────────────────────────────────


class TestOpenAICompatibleDocuments:
    def _part(self, block):
        return openai_module._convert_messages([_msg(block)])[0]["content"][0]

    def test_local_pdf_is_file_part(self, pdf):
        part = self._part(DocumentBlock(source=f"file://{pdf}"))
        assert part["type"] == "file"
        assert part["file"]["filename"] == "report.pdf"
        assert part["file"]["file_data"].startswith("data:application/pdf;base64,")

    def test_local_docx_is_file_part(self, docx):
        part = self._part(DocumentBlock(source=f"file://{docx}"))
        assert part["type"] == "file"
        assert part["file"]["file_data"].startswith(f"data:{DOCX_MIME};base64,")

    def test_text_file_inlined(self, csv):
        part = self._part(DocumentBlock(source=f"file://{csv}"))
        assert part == {"type": "text", "text": "[File: data.csv]\na,b\n1,2\n"}

    def test_missing_file_placeholder(self, tmp_path):
        part = self._part(DocumentBlock(source=f"file://{tmp_path / 'x.pdf'}"))
        assert part["type"] == "text"
        assert "no longer available: x.pdf" in part["text"]


# ─── Anthropic ───────────────────────────────────────────────────────────────


class TestAnthropicDocuments:
    def _block(self, block):
        return anthropic_module._convert_document_block(block)

    def test_local_pdf_base64(self, pdf):
        out = self._block(DocumentBlock(source=f"file://{pdf}"))
        assert out["type"] == "document"
        assert out["source"]["media_type"] == PDF_MIME
        assert base64.b64decode(out["source"]["data"]) == b"%PDF-1.4 fake"

    def test_remote_pdf_url(self):
        out = self._block(DocumentBlock(source="https://x.com/a.pdf"))
        assert out["source"] == {"type": "url", "url": "https://x.com/a.pdf"}

    def test_text_file_is_plain_text_document(self, csv):
        out = self._block(DocumentBlock(source=f"file://{csv}"))
        assert out == {
            "type": "document",
            "source": {
                "type": "text",
                "media_type": "text/plain",
                "data": "a,b\n1,2\n",
            },
            "title": "data.csv",
        }

    def test_docx_degrades_to_text(self, docx):
        out = self._block(DocumentBlock(source=f"file://{docx}"))
        assert out["type"] == "text"
        assert "memo.docx" in out["text"]


# ─── Gemini (Code Assist dict shape) ─────────────────────────────────────────


class TestCodeAssistDocuments:
    def test_pdf_inline(self, pdf):
        part = _block_part(DocumentBlock(source=f"file://{pdf}"), {})
        assert part["inlineData"]["mimeType"] == PDF_MIME
        assert base64.b64decode(part["inlineData"]["data"]) == b"%PDF-1.4 fake"

    def test_text_file_inlined(self, csv):
        part = _block_part(DocumentBlock(source=f"file://{csv}"), {})
        assert part == {"text": "[File: data.csv]\na,b\n1,2\n"}

    def test_docx_placeholder(self, docx):
        part = _block_part(DocumentBlock(source=f"file://{docx}"), {})
        assert "memo.docx" in part["text"]


class TestGeminiSdkDocuments:
    def test_pdf_raw_bytes_and_text_file(self, pdf, csv):
        pytest.importorskip("google.genai")
        from phoson_llm.chats.gemini import _convert_messages

        out = _convert_messages(
            [
                Message(
                    role="user",
                    content=[
                        DocumentBlock(source=f"file://{pdf}"),
                        DocumentBlock(source=f"file://{csv}"),
                    ],
                )
            ]
        )
        pdf_part, csv_part = out[0].parts
        assert pdf_part.inline_data.data == b"%PDF-1.4 fake"
        assert pdf_part.inline_data.mime_type == PDF_MIME
        assert csv_part.text == "[File: data.csv]\na,b\n1,2\n"


# ─── ChatGPT (Responses) ─────────────────────────────────────────────────────


class TestChatGPTDocuments:
    def _parts(self, block):
        items = chatgpt_module._message_items(_msg(block))
        return items[0]["content"]

    def test_local_pdf_is_input_file(self, pdf):
        (part,) = self._parts(DocumentBlock(source=f"file://{pdf}"))
        assert part["type"] == "input_file"
        assert part["filename"] == "report.pdf"
        assert part["file_data"].startswith("data:application/pdf;base64,")

    def test_remote_is_file_url(self):
        (part,) = self._parts(DocumentBlock(source="https://x.com/a.pdf"))
        assert part == {"type": "input_file", "file_url": "https://x.com/a.pdf"}

    def test_text_file_inlined(self, csv):
        (part,) = self._parts(DocumentBlock(source=f"file://{csv}"))
        assert part["type"] == "input_text"
        assert part["text"].startswith("[File: data.csv]")


# ─── Bedrock ─────────────────────────────────────────────────────────────────


class TestBedrockDocuments:
    def _convert(self, block):
        from phoson_llm.chats.bedrock import _convert_block_bedrock

        return _convert_block_bedrock(block)

    def test_pdf_raw_bytes(self, pdf):
        out = self._convert(DocumentBlock(source=f"file://{pdf}"))
        assert out == {
            "document": {
                "format": "pdf",
                "name": "report-pdf",
                "source": {"bytes": b"%PDF-1.4 fake"},
            }
        }

    def test_docx_and_csv_native(self, docx, csv):
        assert (
            self._convert(DocumentBlock(source=f"file://{docx}"))["document"]["format"]
            == "docx"
        )
        assert (
            self._convert(DocumentBlock(source=f"file://{csv}"))["document"]["format"]
            == "csv"
        )

    def test_json_inlined_as_text(self, tmp_path):
        p = tmp_path / "cfg.json"
        p.write_text('{"a": 1}', encoding="utf-8")
        out = self._convert(DocumentBlock(source=f"file://{p}"))
        assert out == {"text": '[File: cfg.json]\n{"a": 1}'}

    def test_name_sanitized(self, tmp_path):
        from phoson_llm.chats.bedrock import _bedrock_document_name

        assert _bedrock_document_name("my  report_v2.final.pdf") == (
            "my report-v2-final-pdf"
        )

    def test_image_uses_short_format_and_raw_bytes(self, tmp_path):
        from phoson_llm.schemas import ImageBlock

        img = tmp_path / "a.png"
        img.write_bytes(b"\x89PNG")
        out = self._convert(ImageBlock(source=f"file://{img}", media_type="image/png"))
        assert out == {"image": {"format": "png", "source": {"bytes": b"\x89PNG"}}}


# ─── Text-only adapters ──────────────────────────────────────────────────────


def test_ollama_inlines_text_document(csv):
    msg = Message(
        role="user",
        content=[TextBlock(text="Summarize:"), DocumentBlock(source=f"file://{csv}")],
    )
    out = ollama_module._convert_messages([msg])
    assert out == [
        {"role": "user", "content": "Summarize: [File: data.csv]\na,b\n1,2\n"}
    ]


def test_mistral_inlines_text_document(csv):
    pytest.importorskip("mistralai")
    from phoson_llm.chats.mistral import _convert_messages_mistral

    out = _convert_messages_mistral([_msg(DocumentBlock(source=f"file://{csv}"))])
    assert out == [{"role": "user", "content": "[File: data.csv]\na,b\n1,2\n"}]
