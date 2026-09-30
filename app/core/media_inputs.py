"""媒体字段别名与 URL 解包；不执行厂商校验、网络探测或业务资产选择。"""

from typing import Any, Dict, List, Optional


FIRST_FRAME_URL_KEYS = (
    "first_frame_url",
    "first_frame",
    "firstFrameUrl",
    "firstFrame",
    "first_image_url",
    "firstImageUrl",
    "start_frame_url",
    "start_frame",
    "startFrameUrl",
    "startFrame",
    "start_image_url",
    "startImageUrl",
    "reference_first_frame_url",
    "reference_start_frame_url",
)

LAST_FRAME_URL_KEYS = (
    "last_frame_url",
    "last_frame",
    "lastFrameUrl",
    "lastFrame",
    "last_image_url",
    "lastImageUrl",
    "end_frame_url",
    "end_frame",
    "endFrameUrl",
    "endFrame",
    "end_image_url",
    "endImageUrl",
    "ending_frame_url",
    "endingFrameUrl",
    "tail_frame_url",
    "tailFrameUrl",
    "reference_last_frame_url",
    "reference_end_frame_url",
)

FIRST_FRAME_ROLES = {
    "first_frame",
    "firstFrame",
    "start_frame",
    "startFrame",
    "reference_first_frame",
    "referenceFirstFrame",
    "reference_start_frame",
    "referenceStartFrame",
}

LAST_FRAME_ROLES = {
    "last_frame",
    "lastFrame",
    "end_frame",
    "endFrame",
    "ending_frame",
    "endingFrame",
    "tail_frame",
    "tailFrame",
    "reference_last_frame",
    "referenceLastFrame",
    "reference_end_frame",
    "referenceEndFrame",
}

REFERENCE_IMAGE_URL_KEYS = (
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
)

REFERENCE_VIDEO_URL_KEYS = (
    "videos",
    "video",
    "video_url",
    "video_urls",
    "videoUrl",
    "videoUrls",
    "uploaded_videos",
    "uploadedVideos",
    "reference_video",
    "reference_video_url",
    "reference_videos",
    "reference_video_urls",
    "referenceVideo",
    "referenceVideoUrl",
    "referenceVideos",
    "referenceVideoUrls",
)

REFERENCE_AUDIO_URL_KEYS = (
    "audios",
    "audio",
    "audio_url",
    "audio_urls",
    "audioUrl",
    "audioUrls",
    "uploaded_audios",
    "uploadedAudios",
    "reference_audio",
    "reference_audio_url",
    "reference_audios",
    "reference_audio_urls",
    "referenceAudio",
    "referenceAudioUrl",
    "referenceAudios",
    "referenceAudioUrls",
)

GENERIC_UPLOAD_MEDIA_KEYS = (
    "file",
    "files",
    "file_list",
    "file_url",
    "file_urls",
    "fileList",
    "fileUrl",
    "fileUrls",
    "upload",
    "upload_file",
    "upload_files",
    "upload_list",
    "uploadFile",
    "uploadFiles",
    "uploadList",
    "uploads",
    "uploaded_file",
    "uploaded_files",
    "uploadedFile",
    "uploadedFiles",
    "attachment",
    "attachments",
    "attachment_url",
    "attachment_urls",
    "attachmentUrl",
    "attachmentUrls",
)


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_media_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("url", "image_url", "video_url", "audio_url", "file_url", "oss_url"):
            nested = value.get(key)
            if isinstance(nested, str):
                return nested
            if isinstance(nested, dict) and isinstance(nested.get("url"), str):
                return nested["url"]
    return None


def extract_uploaded_media_url(value: Any) -> str:
    """在直接 URL 字段之后，递归解包上传接口返回的 data/response/file/upload。"""
    url = extract_media_url(value)
    if url is not None:
        return url
    if isinstance(value, dict):
        for key in ("data", "response", "file", "upload"):
            nested = value.get(key)
            if isinstance(nested, dict):
                url = extract_uploaded_media_url(nested)
                if url:
                    return url
    return ""


def extract_upload_media_type(value: Dict[str, Any]) -> str:
    for key in ("file_type", "media_type", "content_type", "mime_type", "type"):
        item = value.get(key)
        if item not in (None, ""):
            return str(item).strip().lower()
    for key in ("data", "response", "file", "upload"):
        nested = value.get(key)
        if isinstance(nested, dict):
            media_type = extract_upload_media_type(nested)
            if media_type:
                return media_type
    return ""


def drop_frame_url_keys(extra: Dict[str, Any], keep: Optional[set[str]] = None) -> None:
    keep = keep or set()
    for key in (*FIRST_FRAME_URL_KEYS, *LAST_FRAME_URL_KEYS):
        if key not in keep:
            extra.pop(key, None)
