"""文本对话的完整轮次筛选与多模态上下文构造。"""

from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.media_inputs import as_list, extract_uploaded_media_url
from app.models.conversation import ConversationMessage


DEFAULT_TEXT_CONTEXT_MESSAGE_LIMIT = 20
DEFAULT_TEXT_CONTEXT_MESSAGE_SCAN_LIMIT = 60
TEXT_MULTIMODAL_MEDIA_KEYS = (
    "images",
    "image",
    "image_url",
    "image_urls",
    "imageUrl",
    "imageUrls",
    "uploaded_images",
    "uploadedImages",
    "reference_image",
    "reference_image_url",
    "reference_images",
    "reference_image_urls",
    "referenceImage",
    "referenceImageUrl",
    "referenceImages",
    "referenceImageUrls",
    "videos",
    "video",
    "video_url",
    "video_urls",
)


async def build_text_context_messages(
    db: AsyncSession,
    conversation_id: UUID,
    current_content: str,
    current_extra: Optional[Dict[str, Any]] = None,
    *,
    before_sequence_no: Optional[int] = None,
) -> List[Dict[str, Any]]:
    conditions = [
        ConversationMessage.conversation_id == conversation_id,
        ConversationMessage.message_type == "text",
        ConversationMessage.role.in_(("user", "assistant")),
    ]
    if before_sequence_no is not None:
        conditions.append(
            or_(
                ConversationMessage.sequence_no.is_(None),
                ConversationMessage.sequence_no < before_sequence_no,
            )
        )
    result = await db.execute(
        select(ConversationMessage)
        .where(*conditions)
        .order_by(
            ConversationMessage.sequence_no.desc().nullslast(),
            ConversationMessage.created_at.desc(),
            ConversationMessage.id.desc(),
        )
        .limit(DEFAULT_TEXT_CONTEXT_MESSAGE_SCAN_LIMIT)
    )
    history = list(reversed(result.scalars().all()))
    messages = _completed_text_context_messages(history)[-DEFAULT_TEXT_CONTEXT_MESSAGE_LIMIT:]
    messages.append(
        {
            "role": "user", "content": _build_text_multimodal_content(current_content, current_extra or {}),
        }
    )
    return messages


def _completed_text_context_messages(
    history: List[ConversationMessage],
) -> List[Dict[str, Any]]:
    turns: Dict[UUID, Dict[str, Any]] = {}
    ordered_entries: List[Tuple[str, Any]] = []

    for item in history:
        turn_id = getattr(item, "turn_id", None)
        if turn_id is None:
            if not _should_skip_text_context_message(item):
                ordered_entries.append(("legacy", item))
            continue

        if turn_id not in turns:
            turns[turn_id] = {"user": None, "assistant": None}
            ordered_entries.append(("turn", turn_id))

        status = getattr(item, "status", None)
        if item.role == "user" and status == "success" and item.content:
            turns[turn_id]["user"] = item
        elif item.role == "assistant" and status == "success" and item.content:
            turns[turn_id]["assistant"] = item

    messages: List[Dict[str, Any]] = []
    for entry_type, value in ordered_entries:
        if entry_type == "legacy":
            role = "assistant" if value.role == "assistant" else "user"
            content = (
                _build_text_multimodal_content(value.content, value.extra or {})
                if role == "user"
                else value.content
            )
            messages.append({"role": role, "content": content})
            continue

        turn = turns[value]
        if turn["user"] is None or turn["assistant"] is None:
            continue
        messages.extend(
            [
                {
                    "role": "user",
                    "content": _build_text_multimodal_content(
                        turn["user"].content,
                        turn["user"].extra or {},
                    ),
                },
                {"role": "assistant", "content": turn["assistant"].content},
            ]
        )
    return messages


def _should_skip_text_context_message(message: ConversationMessage) -> bool:
    if not message.content:
        return True
    if message.content.startswith("任务已提交") or message.content.startswith("任务执行失败"):
        return True
    if _is_placeholder_assistant_content(message.content):
        return True
    if message.role == "assistant" and (message.extra or {}).get("task_status") != "success":
        return True
    return False


def _is_placeholder_assistant_content(content: Any) -> bool:
    if not isinstance(content, str):
        return False
    return content.startswith("模型未返回有效内容")


def _build_text_multimodal_content(content: str, extra: Dict[str, Any]) -> Any:
    media_urls = _collect_text_multimodal_media_urls(extra)
    if not media_urls:
        return content

    message_content: List[Dict[str, Any]] = [{"type": "text", "text": content}]
    for url in media_urls:
        message_content.append({"type": "image_url", "image_url": {"url": url}})
    return message_content


def _collect_text_multimodal_media_urls(extra: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in TEXT_MULTIMODAL_MEDIA_KEYS:
        if key in extra:
            values.extend(as_list(extra[key]))

    urls: List[str] = []
    for value in values:
        url = extract_uploaded_media_url(value)
        if url:
            urls.append(url)
    return urls
