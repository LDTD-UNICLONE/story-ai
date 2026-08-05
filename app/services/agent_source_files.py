import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePath
from typing import Dict, List, Tuple
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from anyio import BrokenWorkerProcess, fail_after, to_process
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from fastapi import UploadFile
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.exceptions import AppException
from app.models.agent_production import ProjectSourceDocument
from app.models.project import Project
from app.models.user import User
from app.services.projects import get_project_or_404
from app.services.uploads import upload_story_file


SOURCE_TYPES = {
    ".txt": "txt",
    ".md": "md",
    ".docx": "docx",
    ".pdf": "pdf",
}
PREVIEW_CHARACTERS = 3000
MAX_PDF_PAGES = 500
MAX_DOCX_ENTRIES = 2000
MAX_DOCX_COMPRESSION_RATIO = 100


@dataclass(frozen=True)
class ParsedAgentSourceFile:
    filename: str
    source_type: str
    content: str
    warnings: List[str]


async def preview_agent_source_file(
    db: AsyncSession,
    project_id: UUID,
    user: User,
    file: UploadFile,
) -> Dict[str, object]:
    await get_project_or_404(db, project_id, user.id)
    parsed = await parse_agent_source_file(file)
    filename = parsed.filename
    source_type = parsed.source_type
    content = parsed.content
    warnings = parsed.warnings
    await file.seek(0)
    uploaded = await upload_story_file(file, category=f"agent-source/{project_id}")
    source = await _save_source_document(
        db,
        project_id=project_id,
        user_id=user.id,
        source_type=source_type,
        filename=filename,
        content=content,
        file_url=uploaded.url,
        upload_extra={
            "object_key": uploaded.object_key,
            "content_type": uploaded.content_type,
            "file_size": uploaded.size,
            "warnings": warnings,
        },
        commit=True,
    )
    preview = content[:PREVIEW_CHARACTERS]
    return {
        "id": source.id,
        "project_id": source.project_id,
        "source_type": source.source_type,
        "file_url": source.file_url,
        "file_name": source.file_name,
        "content_hash": source.content_hash,
        "character_count": source.character_count,
        "version": source.version,
        "parse_status": source.parse_status,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
        "content_preview": preview,
        "content_preview_truncated": len(content) > len(preview),
        "warnings": warnings,
    }


async def parse_agent_source_file(file: UploadFile) -> ParsedAgentSourceFile:
    filename = (file.filename or "").strip()
    source_type = _source_type(filename)
    max_bytes = max(1, settings.agent_source_max_file_size_mb) * 1024 * 1024
    content_bytes = await file.read(max_bytes + 1)
    if not content_bytes:
        raise AppException("上传剧本文件不能为空", code=40054, status_code=400)
    if len(content_bytes) > max_bytes:
        raise AppException(
            f"剧本文件不能超过 {settings.agent_source_max_file_size_mb}MB",
            code=41301,
            status_code=413,
        )

    content, warnings = await _extract_agent_source_text_safely(
        filename,
        source_type,
        content_bytes,
    )
    content = _normalize_content(content)
    if not content:
        message = "PDF 未提取到文本，请上传可复制文字的 PDF" if source_type == "pdf" else "剧本文件未提取到文本"
        raise AppException(message, code=40055, status_code=400)
    if settings.agent_source_max_characters > 0 and len(content) > settings.agent_source_max_characters:
        raise AppException(
            f"整剧剧本不能超过 {settings.agent_source_max_characters} 个字符",
            code=40050,
            status_code=400,
        )

    await file.seek(0)
    return ParsedAgentSourceFile(
        filename=filename,
        source_type=source_type,
        content=content,
        warnings=warnings,
    )


def extract_agent_source_text(filename: str, content: bytes) -> Tuple[str, List[str]]:
    source_type = _source_type(filename)
    if source_type in {"txt", "md"}:
        return _decode_text(content), []
    if source_type == "docx":
        return _extract_docx(content), []
    return _extract_pdf(content)


async def _extract_agent_source_text_safely(
    filename: str,
    source_type: str,
    content: bytes,
) -> Tuple[str, List[str]]:
    if source_type in {"txt", "md"}:
        return await run_in_threadpool(extract_agent_source_text, filename, content)

    limiter = to_process.current_default_process_limiter()
    limiter.total_tokens = max(1, settings.agent_source_parse_concurrency)
    try:
        with fail_after(max(1, settings.agent_source_parse_timeout_seconds)):
            return await to_process.run_sync(
                extract_agent_source_text,
                filename,
                content,
                cancellable=True,
                limiter=limiter,
            )
    except TimeoutError as exc:
        raise AppException("剧本文件解析超时", code=40801, status_code=408) from exc
    except BrokenWorkerProcess as exc:
        raise AppException("剧本文件解析进程异常终止", code=40064, status_code=400) from exc


def _source_type(filename: str) -> str:
    if len(filename) > 255:
        raise AppException("剧本文件名不能超过 255 个字符", code=40060, status_code=400)
    suffix = PurePath(filename).suffix.lower()
    source_type = SOURCE_TYPES.get(suffix)
    if source_type is None:
        raise AppException(
            "仅支持 txt、md、docx、pdf 剧本文件",
            code=40053,
            status_code=400,
        )
    return source_type


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AppException("无法识别剧本文件编码，请使用 UTF-8 编码", code=40056, status_code=400)


def _extract_docx(content: bytes) -> str:
    try:
        with ZipFile(BytesIO(content)) as archive:
            if len(archive.infolist()) > MAX_DOCX_ENTRIES:
                raise AppException("DOCX 文件条目过多", code=40057, status_code=400)
            info = archive.getinfo("word/document.xml")
            if info.file_size > max(1, settings.agent_source_max_file_size_mb) * 1024 * 1024 * 5:
                raise AppException("DOCX 解压后内容过大", code=40057, status_code=400)
            if info.file_size and (
                info.compress_size <= 0
                or info.file_size / info.compress_size > MAX_DOCX_COMPRESSION_RATIO
            ):
                raise AppException("DOCX 压缩比例异常", code=40057, status_code=400)
            root = ElementTree.fromstring(archive.read(info))
    except (
        BadZipFile,
        KeyError,
        RuntimeError,
        NotImplementedError,
        ElementTree.ParseError,
        DefusedXmlException,
    ) as exc:
        raise AppException("DOCX 文件损坏或格式不正确", code=40057, status_code=400) from exc
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    character_count = 0
    for paragraph in root.iter(f"{namespace}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
        if text.strip():
            character_count += len(text)
            _validate_extracted_character_count(character_count)
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _extract_pdf(content: bytes) -> Tuple[str, List[str]]:
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            raise AppException("暂不支持加密 PDF", code=40058, status_code=400)
        if len(reader.pages) > MAX_PDF_PAGES:
            raise AppException(
                f"PDF 不能超过 {MAX_PDF_PAGES} 页",
                code=40059,
                status_code=400,
            )
        page_texts = []
        character_count = 0
        for page in reader.pages:
            text = (page.extract_text() or "").strip()
            character_count += len(text)
            _validate_extracted_character_count(character_count)
            page_texts.append(text)
    except AppException:
        raise
    except Exception as exc:
        raise AppException("PDF 文件损坏或格式不正确", code=40058, status_code=400) from exc
    empty_pages = sum(not text for text in page_texts)
    warnings = [f"有 {empty_pages} 页未提取到文本"] if empty_pages else []
    return "\n\n".join(text for text in page_texts if text), warnings


def _validate_extracted_character_count(character_count: int) -> None:
    if (
        settings.agent_source_max_characters > 0
        and character_count > settings.agent_source_max_characters
    ):
        raise AppException(
            f"整剧剧本不能超过 {settings.agent_source_max_characters} 个字符",
            code=40050,
            status_code=400,
        )


def _normalize_content(content: str) -> str:
    lines = [line.rstrip() for line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


async def _save_source_document(
    db: AsyncSession,
    *,
    project_id: UUID,
    user_id: UUID,
    source_type: str,
    filename: str,
    content: str,
    file_url: str,
    upload_extra: dict,
    commit: bool = True,
) -> ProjectSourceDocument:
    await db.execute(
        select(Project.id)
        .where(Project.id == project_id, Project.user_id == user_id)
        .with_for_update(of=Project)
    )
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    result = await db.execute(
        select(ProjectSourceDocument).where(
            ProjectSourceDocument.project_id == project_id,
            ProjectSourceDocument.content_hash == content_hash,
        )
    )
    source = result.scalar_one_or_none()
    if source is None:
        version_result = await db.execute(
            select(func.coalesce(func.max(ProjectSourceDocument.version), 0)).where(
                ProjectSourceDocument.project_id == project_id
            )
        )
        source = ProjectSourceDocument(
            project_id=project_id,
            user_id=user_id,
            source_type=source_type,
            file_url=file_url,
            file_name=filename,
            content=content,
            content_hash=content_hash,
            character_count=len(content),
            version=int(version_result.scalar_one()) + 1,
            parse_status="previewed",
            extra=upload_extra,
        )
        db.add(source)
    else:
        source.file_url = file_url
        source.file_name = filename
        source.source_type = source_type
        source.parse_status = "previewed"
        source.extra = {**(source.extra or {}), **upload_extra}
    if commit:
        await db.commit()
        await db.refresh(source)
    else:
        await db.flush()
    return source
