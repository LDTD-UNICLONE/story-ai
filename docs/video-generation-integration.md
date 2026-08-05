# 视频生成对接文档

更新时间：2026-06-30

本文档用于前端对接对话型视频生成和项目型分镜视频生成。火山方舟 Seedance 2.0 按接口文档区分三类互斥输入场景：文生视频、参考生成、首帧/首尾帧生成。后端会把前端入参统一整理为火山方舟 `content[]` 结构。

## 一、生成模式

| 标准值 | 中文含义 | 适用入口 | 素材要求 |
| --- | --- | --- | --- |
| `text_to_video` | 文生视频 | 对话型、项目型 | 只传文本提示词，不传图片、视频、音频、首尾帧 |
| `reference` | 参考生成 / 多模态参考 | 对话型、项目型 | 至少传参考图片或参考视频；参考音频不能单独使用 |
| `first_last_frame` | 首帧 / 首尾帧生成 | 对话型、项目型 | `first_frame_url` 必填；`last_frame_url` 可选 |
| `storyboard` | 故事版生成视频 | 仅项目型 | 使用当前分镜已生成的故事版图像和 `video_prompt` |

兼容别名：

| 前端可传 | 后端标准值 |
| --- | --- |
| `文生视频`、`文本生成视频`、`text`、`text2video`、`text-to-video`、`t2v` | `text_to_video` |
| `参考生成`、`多模态参考`、`多模态参考生成`、`参考图生成`、`参考视频生成`、`参考音频生成`、`reference_generation`、`reference_video`、`reference_audio`、`image_to_video`、`video_to_video`、`audio_to_video`、`multimodal_reference` | `reference` |
| `首帧生成`、`首帧模式`、`首尾帧生成`、`首尾帧模式`、`first_frame`、`first-last-frame`、`first_last` | `first_last_frame` |

## 二、对话型视频生成

接口：

```http
POST /api/v1/conversations/{conversation_id}/messages
```

请求体通用结构：

```json
{
  "content": "视频提示词",
  "ai_model_id": "可选，覆盖会话默认视频模型 UUID",
  "extra": {
    "generation_mode": "text_to_video",
    "resolution": "720p",
    "duration": 5,
    "ratio": "16:9",
    "return_last_frame": false
  }
}
```

常用字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `content` | string | 文本提示词；文生视频必填 |
| `extra.generation_mode` | string | 可选；不传时后端会按素材自动推断，无素材为 `text_to_video` |
| `extra.resolution` | string | `480p`、`720p`、`1080p`、`4k`；`4k` 仅 Seedance 2.0 标准模型支持 |
| `extra.duration` | integer | Seedance 2.0 支持 `4-15` 秒或 `-1` |
| `extra.ratio` | string | 火山支持 `adaptive`、`21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16` |
| `extra.return_last_frame` | boolean | 是否返回尾帧 |
| `extra.generate_audio` | boolean | 是否生成音频 |

### 2.1 文生视频

```json
{
  "content": "生成一段古风少年在雨中回头的视频，镜头缓慢推进",
  "extra": {
    "generation_mode": "text_to_video",
    "resolution": "720p",
    "duration": 5,
    "ratio": "16:9"
  }
}
```

后端发送给火山方舟：

```json
[
  {
    "type": "text",
    "text": "生成一段古风少年在雨中回头的视频，镜头缓慢推进"
  }
]
```

规则：文生视频不能传 `uploaded_images`、`images`、`videos`、`audios`、`media_items`、`content` 媒体项、`first_frame_url`、`last_frame_url`。

### 2.2 参考生成

上传参考图、参考视频或参考音频后，需要把 `POST /api/v1/uploads/file` 返回的 `data.url` 放进消息 `extra`。上传接口不会自动绑定到会话消息。

```json
{
  "content": "参考这些素材生成一段镜头稳定的视频",
  "extra": {
    "generation_mode": "reference",
    "resolution": "720p",
    "media_items": [
      {
        "type": "image_url",
        "image_url": { "url": "https://example.com/ref.png" },
        "role": "reference_image"
      },
      {
        "type": "video_url",
        "video_url": { "url": "https://example.com/ref.mp4" },
        "role": "reference_video"
      }
    ]
  }
}
```

扁平字段也支持：

```json
{
  "content": "参考图片生成一个动态镜头",
  "extra": {
    "generation_mode": "reference",
    "uploaded_images": ["https://example.com/ref.png"],
    "reference_video_url": "https://example.com/ref.mp4"
  }
}
```

参考视频判断逻辑：

- 对话型视频没有单独的 `reference_video` 生成模式。前端即使展示“参考视频生成”，请求也统一传 `generation_mode=reference`。
- 只要 `extra` 中存在 `reference_video_url`、`reference_video_urls`、`video_url`、`video_urls`、`uploaded_videos`，或 `media_items[].role=reference_video`，后端就会按“参考视频参与的多模态参考生成”处理。
- 如果不传 `generation_mode`，但传了上述参考视频字段，后端会自动推断为 `reference`。
- 历史兼容字段 `generation_mode=reference_video`、`video_to_video`、`video-to-video`、`参考视频生成` 会被后端归一为 `reference`；新前端建议直接传 `reference`。
- 参考视频生成入口必须按当前模型能力展示：模型支持 `videos` 或 `media_limits.videos > 0` 才能传参考视频。不支持时后端会前置返回“当前模型不支持参考视频生成”，不要继续提交到厂商。
- 参考视频最终提交给火山方舟时会变成 `content[].type=video_url` 且 `role=reference_video`。

参考音频判断逻辑：

- 对话型视频没有单独的 `reference_audio` 生成模式。前端即使展示“参考音频生成”，请求也统一传 `generation_mode=reference`。
- 只要 `extra` 中存在 `reference_audio_url`、`reference_audio_urls`、`audio_url`、`audio_urls`、`uploaded_audios`，或 `media_items[].role=reference_audio`，后端就会按“参考音频参与的多模态参考生成”处理。
- 火山方舟要求参考音频不能单独使用；同一请求必须至少还有 1 张参考图或 1 个参考视频，否则后端返回 400。
- 如果不传 `generation_mode`，但传了参考音频字段，后端会自动推断为 `reference`；如果只有音频，没有图片或视频，仍然是不合法请求。
- 历史兼容字段 `generation_mode=reference_audio`、`audio_to_video`、`audio-to-video`、`参考音频生成` 会被后端归一为 `reference`；新前端建议直接传 `reference`。
- 参考音频生成入口必须按当前模型能力展示：模型支持 `audio_url` 或 `media_limits.audios/audio > 0` 才能传参考音频。
- 参考音频最终提交给火山方舟时会变成 `content[].type=audio_url` 且 `role=reference_audio`。
- `generate_audio` 是控制输出视频是否生成同步声音的布尔参数，不等于参考音频输入。

如果前端直接保存上传响应对象，也可以这样传：

```json
{
  "content": "参考上传图片生成视频",
  "extra": {
    "generation_mode": "reference",
    "uploaded_files": [
      {
        "url": "https://oss.example.com/story/uploads/ref.png",
        "content_type": "image/png",
        "file_type": "image"
      }
    ]
  }
}
```

火山方舟角色映射：

| 素材 | `content.type` | `role` |
| --- | --- | --- |
| 参考图 | `image_url` | `reference_image` |
| 参考视频 | `video_url` | `reference_video` |
| 参考音频 | `audio_url` | `reference_audio` |

规则：参考图片最多 9 张，参考视频最多 3 个，参考音频最多 3 个；音频不能单独使用，必须同时有参考图片或参考视频。火山 Seedance 2.0 参考视频支持 `mp4/mov`，视频编码 `H.264/H.265`，单个时长 `[2, 15.2]` 秒，所有视频总时长不超过 15.2 秒，单个不超过 200 MB，FPS `[24, 60]`，宽高比 `[0.4, 2.5]`。火山 Seedance 2.0 参考音频支持 `wav/mp3`，单段时长 `[2, 15]` 秒，所有音频总时长不超过 15 秒，单段大小不超过 15 MB。

### 2.3 首帧 / 首尾帧生成

只传首帧：

```json
{
  "content": "以首帧为起点，人物缓慢转身，镜头轻微推进",
  "extra": {
    "generation_mode": "first_frame",
    "resolution": "720p",
    "first_frame_url": "https://example.com/first.png"
  }
}
```

同时传首尾帧：

```json
{
  "content": "从首帧自然过渡到尾帧，中间保持人物身份、服装和场景一致",
  "extra": {
    "generation_mode": "first_last_frame",
    "resolution": "720p",
    "first_frame_url": "https://example.com/first.png",
    "last_frame_url": "https://example.com/last.png",
    "return_last_frame": true
  }
}
```

后端发送给火山方舟：

```json
[
  {
    "type": "text",
    "text": "从首帧自然过渡到尾帧，中间保持人物身份、服装和场景一致"
  },
  {
    "type": "image_url",
    "image_url": { "url": "https://example.com/first.png" },
    "role": "first_frame"
  },
  {
    "type": "image_url",
    "image_url": { "url": "https://example.com/last.png" },
    "role": "last_frame"
  }
]
```

规则：`first_frame_url` 必填，`last_frame_url` 可选；不能混入参考图、参考视频或参考音频。

## 三、项目型分镜视频生成

接口：

```http
POST /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/video-generation
```

请求体通用结构：

```json
{
  "ai_model_id": "视频模型 UUID",
  "generation_mode": "reference",
  "resolution": "720p",
  "return_last_frame": false,
  "prompt": "可选用户补充要求",
  "character_ids": ["人物 UUID"],
  "scene_ids": ["场景 UUID"],
  "prop_ids": ["道具 UUID"],
  "uploaded_images": ["https://example.com/ref.png"],
  "first_frame_url": null,
  "last_frame_url": null,
  "extra": {
    "duration": 5,
    "ratio": "16:9"
  }
}
```

项目型字段说明：

| 字段 | 说明 |
| --- | --- |
| `generation_mode=text_to_video` | 只使用当前分镜字段和 `prompt` 拼接文本提示词，不携带参考素材 |
| `generation_mode=reference` | 使用 `uploaded_images`、人物资产图、场景资产图、道具资产图，以及 `extra` 中的参考视频/音频 |
| `generation_mode=first_last_frame` | 只使用 `first_frame_url` 和可选 `last_frame_url` |
| `generation_mode=storyboard` | 只使用当前分镜已生成的故事版图像和 `video_prompt` |
| `resolution` | `480p`、`720p`、`1080p`、`4k`；模型不支持会返回 400 |
| `return_last_frame` | 返回尾帧后会写入任务和分镜 `extra` |
| `extra.duration` | 不传时后端优先使用分镜 `duration_suggestion` |
| `extra.ratio` / `extra.aspect_ratio` | 不传时后端默认使用项目生成比例 |

### 3.1 项目文生视频

```json
{
  "ai_model_id": "视频模型 UUID",
  "generation_mode": "text_to_video",
  "resolution": "720p",
  "prompt": "镜头更慢，雨水更明显",
  "extra": {
    "duration": 5,
    "ratio": "16:9"
  }
}
```

后端会根据分镜字段拼接视频提示词，并清理所有参考图、参考视频、参考音频、首尾帧字段。

### 3.2 项目参考生成

```json
{
  "ai_model_id": "视频模型 UUID",
  "generation_mode": "reference",
  "resolution": "720p",
  "character_ids": ["人物 UUID"],
  "scene_ids": ["场景 UUID"],
  "prop_ids": ["道具 UUID"],
  "uploaded_images": ["https://example.com/ref.png"],
  "extra": {
    "duration": 5,
    "ratio": "adaptive",
    "reference_video_url": "https://example.com/ref.mp4"
  }
}
```

规则：参考生成至少需要参考图片或参考视频。若要不带任何素材生成，请改用 `text_to_video`。

### 3.3 项目首帧 / 首尾帧生成

```json
{
  "ai_model_id": "视频模型 UUID",
  "generation_mode": "first_last_frame",
  "resolution": "720p",
  "return_last_frame": true,
  "first_frame_url": "https://example.com/first.png",
  "last_frame_url": "https://example.com/last.png",
  "extra": {
    "duration": 5,
    "ratio": "16:9"
  }
}
```

规则：后端会清空 `uploaded_images`、人物/场景/道具参考图、`extra.content`、`extra.media`、`extra.media_items`、参考视频和参考音频，只提交首帧和可选尾帧。

### 3.4 项目故事版生成视频

```json
{
  "ai_model_id": "视频模型 UUID",
  "generation_mode": "storyboard",
  "resolution": "720p",
  "return_last_frame": false,
  "extra": {
    "duration": 5,
    "ratio": "16:9"
  }
}
```

前置条件：当前分镜必须已有 `video_prompt`，并且必须已有故事版图像结果。该模式只使用当前分镜故事版图像，不混入上传图、资产图、首尾帧或 `extra.content/media/media_items`。

## 四、首尾帧兼容字段

首帧 URL 兼容：

```text
first_frame_url, first_frame, firstFrameUrl, firstFrame,
first_image_url, firstImageUrl,
start_frame_url, start_frame, startFrameUrl, startFrame,
start_image_url, startImageUrl,
reference_first_frame_url, reference_start_frame_url
```

尾帧 URL 兼容：

```text
last_frame_url, last_frame, lastFrameUrl, lastFrame,
last_image_url, lastImageUrl,
end_frame_url, end_frame, endFrameUrl, endFrame,
end_image_url, endImageUrl,
ending_frame_url, endingFrameUrl,
tail_frame_url, tailFrameUrl,
reference_last_frame_url, reference_end_frame_url
```

## 五、4K 与计费

- `4k` 仅 Seedance 2.0 标准模型支持，Seedance 2.0 Fast 不支持。
- 不传 `resolution` 时默认 `720p`。
- 4K 无视频参考：`60` 积分/秒。
- 4K 有视频参考：`80` 积分/秒。
- 是否“有视频参考”按参考视频字段判断，参考图片、首帧、尾帧不属于视频参考。

## 六、轮询

提交成功后两类接口都会返回本地 `task_record_id`。前端只需要轮询本地任务：

```http
GET /api/v1/conversations/{conversation_id}/generation-tasks/{task_record_id}
GET /api/v1/task-records/{task_record_id}
```

轮询间隔优先使用响应中的 `next_poll_seconds` 或响应头 `X-Next-Poll-Seconds`。当状态为 `success`、`failed` 或 `stop_polling=true` 时停止轮询。
