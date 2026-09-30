"""将选中的图片及正文引用编译为顺序一致的模型输入。"""

import re
from typing import Any

from app.core.exceptions import AppException
from app.core.media_inputs import (
    FIRST_FRAME_URL_KEYS,
    GENERIC_UPLOAD_MEDIA_KEYS,
    LAST_FRAME_URL_KEYS,
    REFERENCE_IMAGE_URL_KEYS,
)
from app.schemas.conversation import ConversationImageReference


def compile_image_references(
    content: str,
    extra: dict[str, Any],
    references: list[ConversationImageReference],
    message_type: str,
    *,
    resolved_urls: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    if "image_references" in extra:
        raise AppException("图片引用请通过顶层 image_references 提交", code=40016)
    if not references:
        if message_type in {"image", "video"} and re.search(r"@\{[^{}]+\}", content):
            raise AppException("请先在当前输入框上传图片，再使用 @ 引用", code=40016)
        return content, extra
    if message_type not in {"image", "video"}:
        raise AppException("图片引用仅支持图像或视频生成", code=40016)

    conflicting_keys = (
        *REFERENCE_IMAGE_URL_KEYS, *FIRST_FRAME_URL_KEYS, *LAST_FRAME_URL_KEYS,
        *GENERIC_UPLOAD_MEDIA_KEYS, "media", "media_items", "content", "messages",
    )
    if any(extra.get(key) for key in conflicting_keys):
        raise AppException(
            "使用 image_references 时，请将所有参考图片放入该数组，不要混用其他图片或首尾帧参数",
            code=40016,
        )

    urls: list[str] = []
    indices = {}
    for reference in references:
        url = (resolved_urls or {}).get(reference.name) or str(reference.url or "")
        if not url:
            raise AppException("图片 ID 尚未解析为可用附件", code=40016)
        if url not in urls:
            urls.append(url)
        indices[reference.name] = urls.index(url) + 1

    for match in re.finditer(r"@\{([^{}]+)\}|(?<![\w@])@([\w-]+)", content):
        if (match.group(1) or match.group(2)) not in indices:
            raise AppException("引用图片不在本轮附件中，请在当前输入框上传后重新选择", code=40016)

    names = "|".join(re.escape(name) for name in sorted(indices, key=len, reverse=True))
    pattern = re.compile(r"@\{(" + names + r")\}|(?<![\w@])@(" + names + r")(?![\w-])")

    def replace_mention(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        return f"@图片{indices[name]}"

    prompt = pattern.sub(replace_mention, content)
    return prompt, {**extra, "image_urls": urls}
