"""Compile explicitly connected, pinned text and images into a frozen model request."""

import re

from sqlalchemy import select

from app.core.exceptions import AppException
from app.models.project_media import ProjectMedia
from app.schemas.project_canvas import CanvasContent
from app.services.generation.runner import validate_model_request
from app.services.seedance_images import is_seedance_model, provider_scope, reviewed_video_extra


def invalid(message):
    raise AppException(message, code=40073, status_code=400)


async def compile_inputs(db, project_id, user_id, node, nodes, edges, model):
    content = CanvasContent.model_validate(node.content)
    prompt = content.text.strip()
    if not prompt:
        invalid("请先填写节点提示词")
    if re.search(r"@(?:图片|文本)\d+", prompt):
        invalid("引用编号仅用于展示，请使用 @{连接 UUID} 保存引用")
    incoming = sorted(
        (e for e in edges.values() if e.target_id == node.id),
        key=lambda e: (e.input, e.position, str(e.id)),
    )
    text_edges = {str(e.id): e for e in incoming if e.input == "text"}
    incoming = [e for e in incoming if e.input != "text"]
    if any(nodes[e.source_id].kind != "image" for e in incoming):
        invalid("当前生成仅支持图片输入连接，请移除文本或视频输入")
    if node.kind == "text" and incoming:
        invalid("当前文本节点不支持图片输入")
    if any(e.media_id is None for e in incoming):
        invalid("输入连接尚未固定图片，请更新输入图片后再生成")
    ids = {e.media_id for e in incoming}
    media = (
        {
            m.id: m
            for m in await db.scalars(
                select(ProjectMedia).where(
                    ProjectMedia.project_id == project_id,
                    ProjectMedia.id.in_(ids),
                    ProjectMedia.media_type == "image",
                )
            )
        }
        if ids
        else {}
    )
    if set(media) != ids:
        invalid("输入图片失效或不属于当前项目")
    if any(not item.source_verified for item in media.values()):
        invalid("历史图片缺少来源凭证，请重新上传原文件并更新输入")
    snapshot = [
        dict(
            reference_id=str(e.id),
            source_node_id=str(e.source_id),
            media_id=str(e.media_id),
            input=e.input,
            position=e.position,
            url=media[e.media_id].upload["url"],
        )
        for e in incoming
    ]
    references = [x for x in snapshot if x["input"] == "reference"]
    frames = {x["input"]: x["url"] for x in snapshot if x["input"] != "reference"}
    if frames and (node.kind != "video" or references or "first_frame" not in frames):
        invalid("首尾帧模式需要首帧，且不能同时连接 reference 图片")
    urls = list(dict.fromkeys(x["url"] for x in references))
    labels = {
        x["reference_id"]: (
            f"@图片{urls.index(x['url']) + 1}"
            if x["input"] == "reference"
            else ("首帧图片" if x["input"] == "first_frame" else "尾帧图片")
        )
        for x in snapshot
    }

    # Validate tags in the authored prompt; inserted source text is literal, not recursive.
    if "@{" in re.sub(r"@\{([^{}]*)\}", "", prompt):
        invalid("引用格式应为 @{连接 UUID}")
    used_text = set()

    def replace(match):
        edge = text_edges.get(match[1])
        if edge is not None:
            value = edge.text_snapshot or {}
            text = value.get("text")
            if nodes[edge.source_id].kind != "text" or not isinstance(text, str) or not text.strip():
                invalid("文本引用为空或已失效，请更新来源文本")
            if len(text) > 10000:
                invalid("引用文本超过10000字符")
            if match[1] not in used_text:
                snapshot.append(dict(
                    reference_id=match[1], source_node_id=str(edge.source_id), input="text",
                    position=edge.position, text_source=edge.text_source, **value,
                ))
                used_text.add(match[1])
            return text
        if match[1] not in labels:
            invalid("提示词引用已失效，请重新选择当前节点的输入")
        return labels[match[1]]

    # Bound expansion before constructing a potentially very large prompt.
    parts, cursor, length = [], 0, 0
    for match in re.finditer(r"@\{([^{}]*)\}", prompt):
        prefix, value = prompt[cursor:match.start()], replace(match)
        length += len(prefix) + len(value)
        if length > 10000:
            invalid("展开引用后的提示词不能超过10000字符")
        parts.extend((prefix, value))
        cursor = match.end()
    if length + len(prompt[cursor:]) > 10000:
        invalid("展开引用后的提示词不能超过10000字符")
    prompt = "".join([*parts, prompt[cursor:]])
    extra = content.generation.parameters.model_dump(exclude_none=True)
    allowed = {
        "text": {"temperature", "max_tokens"},
        "image": {"aspect_ratio", "resolution", "size", "n", "seed"},
        "video": {"aspect_ratio", "resolution", "duration", "seed", "generate_audio", "watermark"},
    }[node.kind]
    if set(extra) - allowed:
        invalid("生成参数与节点类型不匹配")
    if urls:
        extra["image_urls"] = urls
    scope = None
    if node.kind == "video":
        mode = "first_last_frame" if frames else "reference" if urls else "text_to_video"
        provider_mode = "image_to_video" if mode == "reference" else mode
        extra.update(generation_mode=mode, video_mode=provider_mode, capability=provider_mode)
        if frames:
            extra["first_frame_url"] = frames["first_frame"]
            if "last_frame" in frames:
                extra["last_frame_url"] = frames["last_frame"]
        validate_model_request(model, node.kind, prompt, extra)
        if is_seedance_model(model):
            scope = provider_scope()
        extra = await reviewed_video_extra(db, user_id, model, prompt, extra)
    validate_model_request(model, node.kind, prompt, extra)
    return prompt, extra, snapshot, scope
