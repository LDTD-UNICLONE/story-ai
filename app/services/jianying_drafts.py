import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import httpx
import pyJianYingDraft as draft
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.outbound_url import open_safe_http_response, trusted_oss_hosts
from app.integrations.oss import OssClient
from app.models.agent_review import AgentDelivery


PLACEHOLDER_ROOT = "__LINGJING_DRAFT_ROOT__"
SUPPORTED_JIANYING_VERSION = "10.8"
SUPPORTED_PLATFORMS = {"windows", "macos"}


async def build_and_upload_jianying_draft(
    delivery: AgentDelivery,
) -> Tuple[str, Dict[str, Any]]:
    extra = delivery.extra or {}
    platform = str(extra.get("platform") or "")
    version = str(extra.get("jianying_version") or "")
    if platform not in SUPPORTED_PLATFORMS or version != SUPPORTED_JIANYING_VERSION:
        raise AppException("不支持的剪映草稿平台或版本", code=40063, status_code=400)

    draft_name = _safe_draft_name(str(extra.get("draft_name") or "灵境漫剧"))
    shots = _manifest_shots(delivery.manifest or {})
    if not shots:
        raise AppException("剪映草稿没有可用视频", code=40977, status_code=409)

    with tempfile.TemporaryDirectory(prefix="agent-jianying-") as temp_dir:
        workspace = Path(temp_dir)
        drafts_root = workspace / "drafts"
        drafts_root.mkdir()
        script = draft.DraftFolder(str(drafts_root)).create_draft(
            draft_name,
            *_canvas_size(delivery.manifest or {}),
            fps=30,
        )
        draft_root = drafts_root / draft_name
        material_root = draft_root / "materials" / "video"
        material_root.mkdir(parents=True)
        paths = await _download_videos(shots, material_root)
        track = script.append_track(draft.TrackSpec(draft.TrackType.video, name="灵境分镜视频"))
        cursor = 0
        material_items = []
        for shot, path in zip(shots, paths):
            try:
                material = draft.VideoMaterial(str(path), material_name=path.name)
            except Exception as exc:
                raise AppException(
                    f"无法读取交付视频：{path.name}", code=42216, status_code=422
                ) from exc
            script.add_segment(
                draft.VideoSegment(material, draft.Timerange(cursor, material.duration)),
                track,
            )
            cursor += material.duration
            material_items.append(
                {
                    "episode_number": shot["episode_number"],
                    "group_number": shot["group_number"],
                    "storyboard_id": shot["storyboard_id"],
                    "video_history_id": shot["video_history_id"],
                    "filename": path.name,
                    "duration_microseconds": material.duration,
                }
            )
        script.save()
        _finalize_draft_files(
            draft_root,
            platform=platform,
            draft_name=draft_name,
            duration_microseconds=cursor,
        )
        package_manifest = {
            "format": "lingjing-jianying-draft",
            "format_version": 1,
            "platform": platform,
            "jianying_version": version,
            "draft_name": draft_name,
            "production_id": str(delivery.production_id),
            "project_id": str(delivery.project_id),
            "episode_count": len((delivery.manifest or {}).get("episodes") or []),
            "video_count": len(material_items),
            "duration_microseconds": cursor,
            "materials": material_items,
        }
        (draft_root / "manifest.json").write_text(
            json.dumps(package_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (draft_root / "README.txt").write_text(
            _readme(platform, draft_name),
            encoding="utf-8",
        )
        _write_relocation_script(draft_root, platform)
        archive = workspace / f"{draft_name}-{platform}.zip"
        _zip_draft(draft_root, archive)
        digest = _sha256(archive)
        with archive.open("rb") as fileobj:
            url, object_key = await run_in_threadpool(
                OssClient().upload_fileobj,
                fileobj,
                archive.name,
                f"agent-deliveries/{delivery.production_id}/jianying",
                "application/zip",
            )
        return url, {
            "object_key": object_key,
            "file_size": archive.stat().st_size,
            "sha256": digest,
            "video_count": len(material_items),
            "duration_microseconds": cursor,
            "primary_content_file": (
                "draft_content.json" if platform == "windows" else "draft_info.json"
            ),
        }


def compatibility_notes(platform: str) -> List[str]:
    notes = [
        "草稿格式目标版本为剪映专业版 10.8。",
        "解压后先运行包内路径重定位脚本，再将草稿目录导入剪映。",
    ]
    if platform == "windows":
        notes.append("Windows 包使用 draft_content.json 和 PowerShell 路径重定位脚本。")
    else:
        notes.append("macOS 包同时提供 draft_info.json 和 draft_content.json。")
    return notes


def _manifest_shots(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for episode in manifest.get("episodes") or []:
        episode_number = int(episode.get("episode_number") or 0)
        for index, shot in enumerate(episode.get("shots") or [], start=1):
            url = str(shot.get("video_url") or "").strip()
            if not url:
                continue
            result.append(
                {
                    **shot,
                    "video_url": url,
                    "episode_number": episode_number,
                    "group_number": int(shot.get("group_number") or index),
                }
            )
    return result


async def _download_videos(shots: Sequence[Dict[str, Any]], root: Path) -> List[Path]:
    allowed_hosts = trusted_oss_hosts()
    timeout = httpx.Timeout(
        settings.generated_media_connect_timeout_seconds,
        read=settings.generated_media_read_timeout_seconds,
    )
    total_size = 0
    max_size = settings.generated_media_download_max_size_mb * 1024 * 1024
    paths = []
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for shot in shots:
            filename = (
                f"E{int(shot['episode_number']):03d}_G{int(shot['group_number']):03d}.mp4"
            )
            path = root / filename
            response = await open_safe_http_response(
                client,
                shot["video_url"],
                allowed_hosts=allowed_hosts,
            )
            try:
                response.raise_for_status()
                with path.open("wb") as fileobj:
                    async for chunk in response.aiter_bytes():
                        total_size += len(chunk)
                        if total_size > max_size:
                            raise AppException(
                                "剪映草稿视频总大小超过限制", code=41302, status_code=413
                            )
                        fileobj.write(chunk)
            finally:
                await response.aclose()
            paths.append(path)
    return paths


def _finalize_draft_files(
    draft_root: Path,
    *,
    platform: str,
    draft_name: str,
    duration_microseconds: int,
) -> None:
    content_path = draft_root / "draft_content.json"
    content_data = json.loads(content_path.read_text(encoding="utf-8"))
    content_data = _replace_root(content_data, draft_root.as_posix())
    content = json.dumps(content_data, ensure_ascii=False, indent=2)
    content_path.write_text(content, encoding="utf-8")
    if platform == "macos":
        (draft_root / "draft_info.json").write_text(content, encoding="utf-8")

    meta_path = draft_root / "draft_meta_info.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(
        {
            "draft_name": draft_name,
            "draft_fold_path": PLACEHOLDER_ROOT,
            "draft_root_path": PLACEHOLDER_ROOT,
            "tm_duration": duration_microseconds,
        }
    )
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _replace_root(value: Any, source_root: str) -> Any:
    if isinstance(value, dict):
        return {key: _replace_root(item, source_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_root(item, source_root) for item in value]
    if isinstance(value, str):
        return value.replace(source_root, PLACEHOLDER_ROOT)
    return value


def _write_relocation_script(draft_root: Path, platform: str) -> None:
    if platform == "windows":
        script = """$root = (Resolve-Path $PSScriptRoot).Path.Replace('\\', '/')
$utf8 = New-Object System.Text.UTF8Encoding($false)
Get-ChildItem $PSScriptRoot -Filter 'draft_*.json' | ForEach-Object {
    $content = [IO.File]::ReadAllText($_.FullName).Replace('__LINGJING_DRAFT_ROOT__', $root)
    [IO.File]::WriteAllText($_.FullName, $content, $utf8)
}
Write-Host '草稿素材路径已更新，请将当前目录导入剪映专业版。'
"""
        (draft_root / "relocate_windows.ps1").write_text(script, encoding="utf-8")
        return
    script = """#!/bin/sh
set -eu
DRAFT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export DRAFT_ROOT
/usr/bin/perl -0pi -e 's{__LINGJING_DRAFT_ROOT__}{$ENV{DRAFT_ROOT}}g' \
  "$DRAFT_ROOT"/draft_*.json
echo '草稿素材路径已更新，请将当前目录导入剪映专业版。'
"""
    path = draft_root / "relocate_macos.command"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def _readme(platform: str, draft_name: str) -> str:
    script_name = "relocate_windows.ps1" if platform == "windows" else "relocate_macos.command"
    return (
        f"剪映草稿：{draft_name}\n"
        f"目标平台：{platform}\n"
        f"目标版本：剪映专业版 {SUPPORTED_JIANYING_VERSION}\n\n"
        f"1. 完整解压 ZIP。\n2. 运行 {script_name} 更新素材路径。\n"
        "3. 将整个草稿目录导入或复制到剪映专业版配置的草稿目录。\n"
        "4. 不要单独移动 materials 目录，否则视频会离线。\n"
    )


def _canvas_size(manifest: Dict[str, Any]) -> Tuple[int, int]:
    ratio = str(manifest.get("generation_ratio") or "16:9")
    resolution = str(manifest.get("video_resolution") or "1080p")
    long_side = {"480p": 854, "720p": 1280, "1080p": 1920, "4k": 3840}.get(
        resolution, 1920
    )
    try:
        left, right = (int(value) for value in ratio.split(":", 1))
    except (TypeError, ValueError):
        left, right = 16, 9
    if left <= 0 or right <= 0:
        left, right = 16, 9
    if left >= right:
        width = long_side
        height = round(long_side * right / left / 2) * 2
    else:
        height = long_side
        width = round(long_side * left / right / 2) * 2
    return max(2, width), max(2, height)


def _safe_draft_name(value: str) -> str:
    normalized = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", value.strip())
    normalized = normalized.strip(" .")
    return (normalized or "灵境漫剧")[:80]


def _zip_draft(draft_root: Path, archive: Path) -> None:
    with ZipFile(archive, "w") as zip_file:
        for path in sorted(draft_root.rglob("*")):
            if not path.is_file():
                continue
            relative = Path(draft_root.name) / path.relative_to(draft_root)
            compression = ZIP_STORED if path.suffix.lower() in {".mp4", ".mov", ".m4v"} else ZIP_DEFLATED
            zip_file.write(path, relative.as_posix(), compress_type=compression)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fileobj:
        for chunk in iter(lambda: fileobj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
