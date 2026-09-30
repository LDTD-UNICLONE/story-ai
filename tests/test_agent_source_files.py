from io import BytesIO
import time
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import UploadFile
from pypdf import PdfWriter

from app.core.exceptions import AppException
from app.core.config import settings
from app.services.agent import source_files as agent_source_files
from app.services.agent.source_files import extract_agent_source_text, parse_agent_source_file


def _slow_source_extract(_filename: str, _content: bytes):
    time.sleep(2)
    return "延迟返回", []


def test_text_source_supports_gb18030_encoding() -> None:
    text, warnings = extract_agent_source_text("整剧.txt", "第一集：雨夜".encode("gb18030"))

    assert text == "第一集：雨夜"
    assert warnings == []


def test_docx_source_extracts_paragraphs() -> None:
    document_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:r><w:t>第一集</w:t></w:r></w:p>
        <w:p><w:r><w:t>雨夜相遇</w:t></w:r></w:p>
      </w:body>
    </w:document>""".encode()
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)

    text, warnings = extract_agent_source_text("整剧.docx", buffer.getvalue())

    assert text == "第一集\n雨夜相遇"
    assert warnings == []


def test_docx_source_rejects_xml_entities() -> None:
    document_xml = b"""<?xml version="1.0"?>
    <!DOCTYPE document [<!ENTITY payload "expanded">]>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>&payload;</w:t></w:r></w:p></w:body>
    </w:document>"""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)

    with pytest.raises(AppException) as exc_info:
        extract_agent_source_text("整剧.docx", buffer.getvalue())

    assert exc_info.value.code == 40057


def test_docx_source_rejects_suspicious_compression_ratio() -> None:
    document_xml = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:p><w:r><w:t>"
        + b"A" * 1_000_000
        + b"</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)

    with pytest.raises(AppException) as exc_info:
        extract_agent_source_text("压缩异常.docx", buffer.getvalue())

    assert exc_info.value.code == 40057


@pytest.mark.asyncio
async def test_pdf_source_parsing_times_out(monkeypatch) -> None:
    monkeypatch.setattr(agent_source_files, "extract_agent_source_text", _slow_source_extract)
    monkeypatch.setattr(settings, "agent_source_parse_timeout_seconds", 1)
    file = UploadFile(filename="超时.pdf", file=BytesIO(b"pdf"))

    with pytest.raises(AppException) as exc_info:
        await parse_agent_source_file(file)

    assert exc_info.value.code == 40801


@pytest.mark.asyncio
async def test_docx_source_parses_in_isolated_process() -> None:
    document_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>第一集：雨夜</w:t></w:r></w:p></w:body>
    </w:document>""".encode()
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    file = UploadFile(filename="整剧.docx", file=BytesIO(buffer.getvalue()))

    parsed = await parse_agent_source_file(file)

    assert parsed.content == "第一集：雨夜"
    assert parsed.source_type == "docx"


def test_blank_pdf_reports_pages_without_extractable_text() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = BytesIO()
    writer.write(buffer)

    text, warnings = extract_agent_source_text("扫描版.pdf", buffer.getvalue())

    assert text == ""
    assert warnings == ["有 1 页未提取到文本"]


def test_source_file_rejects_unsupported_extension() -> None:
    with pytest.raises(AppException) as exc_info:
        extract_agent_source_text("整剧.rtf", b"script")

    assert exc_info.value.code == 40053


def test_source_file_rejects_filename_too_long_for_storage() -> None:
    with pytest.raises(AppException) as exc_info:
        extract_agent_source_text(f"{'a' * 252}.txt", b"script")

    assert exc_info.value.code == 40060
