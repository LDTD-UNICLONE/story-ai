# 前端接口对接文档

本文档按当前后端代码整理，面向前端开发使用。字段以本文档为准，不展示历史兼容字段，不建议前端发送未列出的字段。

## 1. 通用约定

### 1.1 基础路径

所有接口均挂载在：

```text
/api/v1
```

### 1.2 响应结构

所有业务成功响应统一为：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "timestamp": "2026-06-03T15:00:00+08:00"
}
```

前端只判断：

- `code === 0`：成功
- `code !== 0`：失败，展示 `message`
- 异步生成类任务不要只看提交接口成功，要继续轮询任务状态
- 前端轮询只用于展示状态。用户关闭或切换页面后，后端 Celery reconcile 和 Beat 仍会继续查询第三方结果、保存 OSS 并结算积分

### 1.3 任务状态

前端只使用以下业务状态：

| status | 含义 | 是否继续轮询 |
| --- | --- | --- |
| `pending` | 待执行 | 是 |
| `running` | 执行中 | 是 |
| `success` | 成功 | 否 |
| `failed` | 失败 | 否 |

提交生成任务后，响应里可能带：

- `next_poll_seconds`
- 响应头 `X-Next-Poll-Seconds`

前端轮询间隔优先级：

1. 响应头 `X-Next-Poll-Seconds`
2. 响应体 `data.next_poll_seconds`
3. 兜底：文本 5 秒，图像 10 秒，视频 20 秒

### 1.4 列表顺序

| 数据 | 接口 | 顺序 |
| --- | --- | --- |
| 会话列表 | `GET /conversations` | `created_at desc` |
| 会话消息 | `GET /conversations/{id}/messages` | 默认 `desc`，聊天窗口建议传 `order=asc` |
| 任务记录 | `GET /task-records` | `created_at desc` |
| 项目列表 | `GET /projects` | `created_at desc` |
| 项目资产列表 | `GET /projects/{project_id}/characters|scenes|props` | `created_at desc` |
| 分镜列表 | `GET /projects/{project_id}/chapters/{chapter_id}/storyboards` | `shot_number asc, created_at asc` |
| 生成历史 | `generation-history` 系列接口 | `created_at desc` |
| 模型选择 | `GET /models/options` | `vendor asc, nickname asc` |

### 1.5 通用上传

```http
POST /api/v1/uploads/file
Content-Type: multipart/form-data
```

FormData：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `file` | 是 | 本地文件对象 |
| `category` | 否 | OSS 目录分类，例如 `conversation/reference-video` |

后端支持图片、视频、音频和普通文件上传，不只支持图片。上传成功返回：

```json
{
  "url": "https://oss.example.com/story/conversation/reference-video/xxx.mp4",
  "object_key": "story/conversation/reference-video/xxx.mp4",
  "filename": "reference.mp4",
  "content_type": "video/mp4",
  "size": 123456,
  "file_type": "video"
}
```

`file_type` 取值：

| file_type | 常见格式 |
| --- | --- |
| `image` | png、jpg、jpeg、webp、gif、bmp、tiff、heic、heif |
| `video` | mp4、mov、webm、m4v、mkv、avi、mpeg、mpg、3gp |
| `audio` | mp3、wav、m4a、aac、ogg、flac |
| `file` | pdf、txt、docx、xlsx、zip 等普通文件 |

前端注意：

- 通用上传组件不要写死 `accept="image/*"`。
- 视频参考上传建议：`accept="image/*,video/*,audio/*"`；如果是单独视频入口，可用 `accept="video/mp4,video/quicktime,.mp4,.mov"`。
- 上传视频成功后，建议检查 `data.file_type === "video"`；再把 `data.url` 放入视频生成请求的 `extra.reference_video_url`、`extra.uploaded_videos` 或 `extra.media_items`。
- 上传音频后，把返回的 `data.url` 放入 `extra.reference_audio_url`、`extra.uploaded_audios` 或 `extra.media_items`，并同时传参考图或参考视频。
- 上传接口只返回可访问 URL，不会创建会话消息，也不会自动触发生成任务。
- 文件大小上限以后端 `MAX_UPLOAD_SIZE_MB` 和 Nginx `client_max_body_size` 中较小者为准；当前部署建议统一为 300 MB。

## 2. 模型选择与动态参数

### 2.1 获取可用模型

```http
GET /api/v1/models/options?model_type=text
GET /api/v1/models/options?model_type=image
GET /api/v1/models/options?model_type=video
```

响应 `data[]`：

```json
{
  "id": "uuid",
  "nickname": "模型显示名",
  "model_id": "doubao-seedance-2-0-fast-260128",
  "vendor": "volcengine_ark",
  "model_type": "video",
  "points_cost": 0,
  "model_multiplier": "1.0000",
  "cache_multiplier": "1.0000",
  "completion_multiplier": "1.0000",
  "platform_multiplier": "1.0000",
  "capabilities": {
    "provider": "volcengine_ark",
    "model_family": "doubao-seedance",
    "modes": ["text_to_video", "multimodal_reference", "image_to_video", "first_last_frame"],
    "fields": [],
    "request_keys": [],
    "media_limits": {},
    "defaults": {},
    "ratios": [],
    "resolutions": [],
    "supports_async_task": true
  }
}
```

### 2.2 前端动态表单规则

前端选择模型后，必须以该模型的 `capabilities` 渲染参数：

- 只展示 `capabilities.fields` 中存在的参数。
- 只提交 `capabilities.request_keys` 中存在的扩展参数。
- 参考图、参考视频数量使用 `capabilities.media_limits` 限制。
- 下拉选项优先使用字段上的 `options`。
- 默认值优先使用 `capabilities.defaults`。
- 不要按模型名称在前端硬编码参数，除非只是做 UI 分组标签。

`fields[]` 字段结构：

```json
{
  "name": "resolution",
  "type": "select",
  "label": "分辨率",
  "required": false,
  "options": ["480p", "720p"],
  "multiple": false
}
```

前端控件建议：

| type | 控件 |
| --- | --- |
| `select` | 下拉或 segmented control |
| `integer` | 数字输入框 |
| `boolean` | Switch |
| `string` | 单行输入 |
| `url` / `file` | 上传后回填 URL |
| `file_list` | 多图/多文件上传列表 |
| `array` | JSON 数组编辑器或专用控件 |

## 3. 对话型生成

### 3.1 创建会话

```http
POST /api/v1/conversations
```

请求：

```json
{
  "title": "新会话",
  "ai_model_id": "uuid",
  "conversation_type": "text"
}
```

`conversation_type` 只允许：

```text
text | image | video
```

后端会校验 `ai_model_id` 的 `model_type` 必须与 `conversation_type` 一致。

### 3.2 发送消息

```http
POST /api/v1/conversations/{conversation_id}/messages
```

通用请求：

```json
{
  "content": "用户输入内容",
  "ai_model_id": "uuid",
  "extra": {}
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `content` | 是 | 用户输入文本 |
| `ai_model_id` | 否 | 不传则使用会话当前模型；传入后会切换会话模型 |
| `extra` | 否 | 模型参数，只传本文档允许字段 |

响应：

```json
{
  "user_message": {},
  "assistant_message": {
    "content": "任务已提交，正在生成中",
    "extra": {
      "task_status": "pending",
      "task_record_id": "uuid"
    }
  },
  "points_cost": 1,
  "task_record_id": "uuid",
  "task_status": "pending"
}
```

### 3.3 文本对话 extra

文本对话只建议前端提交以下字段：

```json
{
  "temperature": 0.7,
  "top_p": 1,
  "max_tokens": 4096,
  "presence_penalty": 0,
  "frequency_penalty": 0,
  "stop": ["string"],
  "response_format": { "type": "text" },
  "tools": [],
  "tool_choice": "auto",
  "image_urls": ["https://example.com/a.png"]
}
```

规则：

- `messages` 不由前端传，后端会按会话历史自动拼接。
- 如需图片理解，只传 `image_urls`。
- 不传空字符串、空数组、空对象参数。
- `stream` 不建议前端传；当前任务模式按非流式处理。

### 3.4 图像对话 extra

图像对话用于普通图像生成或图像编辑。前端只展示当前图像模型支持的参数；无模型能力字段时，使用以下最小集：

```json
{
  "image_mode": "generation",
  "aspect_ratio": "16:9",
  "quality": "2k",
  "n": 1,
  "response_format": "url"
}
```

图像编辑时：

```json
{
  "image_mode": "edit",
  "image_urls": ["https://example.com/input.png"],
  "mask_url": "https://example.com/mask.png",
  "aspect_ratio": "1:1",
  "quality": "2k",
  "n": 1
}
```

严格规则：

- `image_mode` 只传 `generation` 或 `edit`。
- `response_format` 只传 `url` 或 `b64_json`。
- `quality` 只传当前模型支持的值；常见值为 `1k`、`2k`、`3k`、`4k`。
- `n` 为 1-10 的整数。
- 参考图字段统一用 `image_urls`。

### 3.5 视频对话 extra

对话型视频支持三种生成方式：

```text
text_to_video | reference | first_last_frame
```

#### 文生视频

```json
{
  "generation_mode": "text_to_video",
  "resolution": "720p",
  "duration": 5,
  "ratio": "16:9",
  "return_last_frame": false
}
```

#### 多模态参考生成

先调用 `POST /api/v1/uploads/file` 上传参考图、参考视频或参考音频，取响应 `data.url` 后再提交到消息 `extra`。上传接口不会自动绑定到会话消息。
如果前端上传视频后没有把 `data.url` 放入参考视频字段，后端会认为没有参考视频；推荐字段是 `reference_video_url`。

```json
{
  "generation_mode": "reference",
  "resolution": "720p",
  "uploaded_images": ["https://example.com/ref.png"],
  "duration": 5,
  "ratio": "16:9",
  "return_last_frame": false
}
```

参考视频生成仍然属于 `reference` 模式，推荐这样传：

```json
{
  "generation_mode": "reference",
  "resolution": "720p",
  "reference_video_url": "https://example.com/ref.mp4",
  "duration": 5,
  "ratio": "16:9",
  "return_last_frame": false
}
```

如果使用媒体项数组，参考视频必须带 `role=reference_video`：

```json
{
  "generation_mode": "reference",
  "resolution": "720p",
  "media_items": [
    {
      "type": "video_url",
      "video_url": {
        "url": "https://example.com/ref.mp4"
      },
      "role": "reference_video"
    }
  ],
  "duration": 5,
  "ratio": "16:9"
}
```

参考音频生成也属于 `reference` 模式，并且必须同时传参考图或参考视频：

```json
{
  "generation_mode": "reference",
  "resolution": "720p",
  "uploaded_images": ["https://example.com/ref.png"],
  "reference_audio_url": "https://example.com/ref.mp3",
  "duration": 5,
  "ratio": "16:9",
  "return_last_frame": false
}
```

如果使用媒体项数组，参考音频必须带 `role=reference_audio`：

```json
{
  "generation_mode": "reference",
  "resolution": "720p",
  "media_items": [
    {
      "type": "image_url",
      "image_url": {
        "url": "https://example.com/ref.png"
      },
      "role": "reference_image"
    },
    {
      "type": "audio_url",
      "audio_url": {
        "url": "https://example.com/ref.mp3"
      },
      "role": "reference_audio"
    }
  ],
  "duration": 5,
  "ratio": "16:9"
}
```

#### 首尾帧生成

```json
{
  "generation_mode": "first_last_frame",
  "resolution": "720p",
  "first_frame_url": "https://example.com/first.png",
  "last_frame_url": "https://example.com/last.png",
  "duration": 5,
  "ratio": "16:9",
  "return_last_frame": true
}
```

严格规则：

- `generation_mode=first_last_frame` 时，`first_frame_url` 必填。
- `generation_mode=text_to_video` 时，不传任何参考图、参考视频、参考音频、首尾帧或媒体项。
- `generation_mode=reference` 时，不传 `first_frame_url`、`last_frame_url`。
- `generation_mode=reference` 时，至少要传参考图片或参考视频；参考音频不能单独使用。
- 参考图统一传 `uploaded_images`。
- 参考视频推荐传 `reference_video_url`；多视频可传 `uploaded_videos` 或 `video_urls`。
- 参考视频字段只传 URL 字符串，不要只传上传组件本地 `File` 对象；如需传上传响应对象，放在 `uploaded_files/files/attachments` 中并保留 `url` 和 `file_type`。
- 前端可以把按钮文案写成“参考视频生成”，但接口仍传 `generation_mode=reference`。后端会根据参考视频字段判断是否属于参考视频生成逻辑。
- 展示“参考视频生成”入口前必须检查当前视频模型能力：`capabilities.request_keys` 包含 `videos`，或 `capabilities.media_limits.videos > 0`，才允许传参考视频；否则会返回“当前模型不支持参考视频生成”。
- 参考视频上传前建议校验 `mp4/mov`、`H.264/H.265`、单个 2-15.2 秒、最多 3 个、总时长不超过 15.2 秒、单个不超过 200 MB、FPS 24-60、宽高比 0.4-2.5、宽高边长 300-6000px。
- 参考音频推荐传 `reference_audio_url`；多音频可传 `uploaded_audios` 或 `audio_urls`。
- 前端可以把按钮文案写成“参考音频生成”，但接口仍传 `generation_mode=reference`。后端会根据参考音频字段判断是否属于参考音频生成逻辑。
- 展示“参考音频生成”入口前必须检查当前视频模型能力：`capabilities.request_keys` 包含 `audio_url`，或 `capabilities.media_limits.audios/audio > 0`，才允许传参考音频。
- 只有参考音频是不合法请求；必须同时传 `uploaded_images`、`reference_video_url`、`uploaded_videos` 或对应 `media_items`。
- 参考音频上传前建议校验 `wav/mp3`、单段 2-15 秒、最多 3 段、总时长不超过 15 秒。
- `generate_audio` 只是控制输出视频是否生成同步声音，不是参考音频上传字段。
- 其它视频参数必须来自当前模型 `capabilities.request_keys`。

## 4. 项目型资产图像生成

人物、场景、道具三个接口请求体一致。

```http
POST /api/v1/projects/{project_id}/characters/{character_id}/image-generation
POST /api/v1/projects/{project_id}/scenes/{scene_id}/image-generation
POST /api/v1/projects/{project_id}/props/{prop_id}/image-generation
```

请求：

```json
{
  "ai_model_id": "uuid",
  "generation_mode": "general",
  "prompt": "可选用户补充要求",
  "extra": {
    "quality": "2k",
    "n": 1
  }
}
```

`generation_mode` 只建议展示：

| 值 | 含义 |
| --- | --- |
| `general` | 通用模式 |
| `profile_card` | 资料卡模式 |

后端拼接逻辑：

```text
画面风格：项目绑定风格提示词
模式提示词：asset_image_generation/{asset_type}/{generation_mode}.md
资产提示词：资产 prompt；如无则 description；如无则 name
请严格围绕该资产生成图像，不要添加与资产无关的主体内容。
```

严格规则：

- 前端不传 `aspect_ratio`，后端使用项目 `generation_ratio`。
- 前端不传资产字段，后端按 `asset_id` 读取资产。
- `generation_mode` 只传 `general` 或 `profile_card`。
- `extra` 只传图像模型允许参数，例如 `quality`、`n`、`response_format`。

响应：

```json
{
  "task_record_id": "uuid",
  "asset_type": "character",
  "asset_id": "uuid",
  "status": "pending",
  "points_cost": 1,
  "next_poll_seconds": 10
}
```

## 5. 故事版图像生成

```http
POST /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/image-generation
```

请求：

```json
{
  "aspect_ratio": "16:9",
  "prompt": "可选用户补充要求",
  "character_ids": ["uuid"],
  "scene_ids": ["uuid"],
  "prop_ids": ["uuid"],
  "uploaded_images": ["https://example.com/ref.png"],
  "extra": {
    "quality": "2k",
    "n": 1
  }
}
```

严格规则：

- 不传 `ai_model_id`。后端固定使用启用状态的 `gpt-image-2` 图像模型。
- `aspect_ratio` 只允许：

```text
16:9 | 9:16 | 1:1 | 4:3 | 3:4 | 3:2 | 2:3 | 21:9
```

- 参考资产 ID 可选；不传时后端会按分镜中的人物、场景、道具名称自动匹配资产。
- `uploaded_images` 只传 URL 字符串数组。
- `extra` 只放图像模型允许参数，不放旧字段。

后端会从当前分镜读取：

```text
title
image_prompt
characters
scene_name
props
negative_prompt
```

如果 `image_prompt` 为空，会按顺序使用：

```text
screen_execution -> action -> source_content
```

响应：

```json
{
  "task_record_id": "uuid",
  "storyboard_id": "uuid",
  "aspect_ratio": "16:9",
  "status": "pending",
  "points_cost": 1,
  "next_poll_seconds": 10
}
```

## 6. 故事版视频生成

```http
POST /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/video-generation
```

请求：

```json
{
  "ai_model_id": "uuid",
  "generation_mode": "reference",
  "resolution": "720p",
  "return_last_frame": false,
  "prompt": "可选用户补充要求",
  "character_ids": ["uuid"],
  "scene_ids": ["uuid"],
  "prop_ids": ["uuid"],
  "uploaded_images": ["https://example.com/ref.png"],
  "first_frame_url": null,
  "last_frame_url": null,
  "extra": {
    "duration": 5,
    "ratio": "16:9"
  }
}
```

### 6.1 generation_mode

| 值 | 含义 | 参考图来源 |
| --- | --- | --- |
| `reference` | 多模态参考生成 | `uploaded_images` + 选择的人物/场景/道具资产参考图 |
| `text_to_video` | 文生视频 | 不使用参考图，只使用当前分镜文本 |
| `first_last_frame` | 首尾帧生成 | `first_frame_url` / `last_frame_url` |
| `storyboard` | 故事版生成视频 | 当前分镜已生成的故事版图像 |

严格规则：

- `resolution` 只传当前模型支持的值：`480p`、`720p`、`1080p`、`4k`；`4k` 仅 Seedance 2.0 标准模型支持。
- `generation_mode=text_to_video` 时，不传 `uploaded_images`、`character_ids`、`scene_ids`、`prop_ids`、`first_frame_url`、`last_frame_url`。
- `generation_mode=first_last_frame` 时，`first_frame_url` 必填。
- `generation_mode=storyboard` 时：
  - 前端必须先完成故事版提示词生成，使分镜有 `video_prompt`。
  - 前端必须先完成故事版图像生成，使分镜 `extra.image_generation_result` 或 `extra.storyboard_image_result` 有值。
  - 不传 `uploaded_images`、`character_ids`、`scene_ids`、`prop_ids` 作为视频参考图。
  - 后端只取当前分镜已生成的故事版图像作为参考图。
- 其它模型参数放入 `extra`，并且必须来自当前视频模型 `capabilities.request_keys`。

### 6.2 时长与比例

后端处理：

- 比例默认来自项目 `generation_ratio`。
- 如果 `extra.duration`、`extra.seconds`、`extra.duration_seconds` 等存在，会解析为秒数。
- 如果未传显式时长，会使用分镜 `duration_suggestion`。
- 时长会限制在 5-15 秒；火山方舟视频最低 4 秒。
- 故事版生成视频的时长和比例仍按多模态参考逻辑处理。

前端建议：

- 对视频模型展示 `capabilities.fields` 中的 `duration`、`ratio/aspect_ratio`、`resolution`。
- 如果字段有 `options`，必须使用下拉，不允许自由输入。
- 如果 `capabilities.durations` 存在，只允许选其中的值。

响应：

```json
{
  "task_record_id": "uuid",
  "storyboard_id": "uuid",
  "generation_mode": "reference",
  "resolution": "720p",
  "return_last_frame": false,
  "status": "pending",
  "points_cost": 1,
  "next_poll_seconds": 20
}
```

## 7. 故事版提示词生成

```http
POST /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/storyboard-prompt-generation
```

请求：

```json
{
  "ai_model_id": "uuid",
  "extra": {}
}
```

后端生成故事版图像提示词和视频提示词，前端提交后轮询任务记录即可。生成成功后重新拉取分镜详情或分镜列表，读取：

```text
image_prompt
video_prompt
duration_suggestion
negative_prompt
ending_frame
```

当前分镜传给提示词模型的核心字段只有：

```json
{
  "shot_number": 1,
  "title": "分镜标题",
  "source_content": "分镜原文内容"
}
```

## 8. 任务轮询接口

### 8.1 单任务轮询

```http
GET /api/v1/task-records/{task_record_id}
```

响应 `data`：

```json
{
  "id": "uuid",
  "business_type": "project",
  "generation_type": "storyboard_video",
  "status": "running",
  "title": "分镜视频生成：xxx",
  "result": "模型任务仍在生成中：task_xxx",
  "points_cost": 1,
  "extra": {},
  "created_at": "2026-06-03T15:00:00+08:00",
  "stop_polling": false,
  "next_poll_seconds": 20
}
```

### 8.2 批量轮询

```http
POST /api/v1/task-records/batch
```

请求：

```json
{
  "ids": ["uuid"]
}
```

限制：

- `ids` 最少 1 个，最多 50 个。

响应：

```json
{
  "items": [],
  "total": 1,
  "stop_polling": false,
  "next_poll_seconds": 20
}
```

前端建议：

- 页面上同时有多个生成任务时，使用批量轮询。
- 当 `stop_polling=true` 时停止该轮询。
- 任务成功后按任务类型重新拉取对应业务详情：
  - 资产图：拉资产详情或生成历史。
  - 故事版图：拉分镜详情或故事版生成历史。
  - 故事版视频：拉分镜详情或故事版生成历史。
  - 对话：拉消息列表或会话生成任务详情。

## 9. 生成历史选择

### 9.1 资产图像历史

```http
GET /api/v1/projects/{project_id}/assets/{asset_type}/{asset_id}/generation-history
```

`asset_type` 只允许：

```text
character | scene | prop
```

选择历史图：

```http
POST /api/v1/projects/{project_id}/assets/{asset_type}/{asset_id}/generation-history/{history_id}/select
```

请求：

```json
{
  "result_url": "https://example.com/image.png"
}
```

### 9.2 故事版历史

```http
GET /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/generation-history?media_type=image
GET /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/generation-history?media_type=video
```

选择历史：

```http
POST /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/generation-history/{history_id}/select?media_type=image
POST /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/generation-history/{history_id}/select?media_type=video
```

请求：

```json
{
  "result_url": "https://example.com/result.mp4"
}
```

## 10. 视频模型参数速查

前端不要硬编码这些表用于提交，只用于理解和 UI 兜底。实际展示以 `/models/options` 返回的 `capabilities` 为准。

### 10.1 火山方舟视频

`vendor=volcengine_ark`

常见能力：

| 参数 | 值 |
| --- | --- |
| `ratio` | `adaptive`, `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16` |
| `resolution` | 由模型决定，常见 `480p`, `720p`, `1080p`, `4k`；`4k` 仅 Seedance 2.0 标准模型支持 |
| `duration` | 整数 |
| `generate_audio` | boolean |
| `return_last_frame` | boolean |
| `watermark` | boolean |
| `media_limits.images` | 9 |

### 10.2 Comfly Sora2

`model_family=sora2`

| 参数 | 值 |
| --- | --- |
| `aspect_ratio` | `16:9`, `9:16` |
| `duration` | `4`, `8`, `10`, `12`, `15`, `25` |
| `hd` | boolean，仅 `sora-2-pro` 支持 |
| `images` | 最多 1 张 |
| `character_url` | URL |
| `character_timestamps` | array |
| `notify_hook` | URL |
| `watermark` | boolean |
| `private` | boolean |

### 10.3 Comfly Veo

`model_family=veo`

| 参数 | 值 |
| --- | --- |
| `aspect_ratio` | `16:9`, `9:16` |
| `duration` | integer |
| `enhance_prompt` | boolean |
| `enable_upsample` | boolean |
| `images` | 最多 3 张 |

### 10.4 Comfly Wan

`model_family=wan`

| 参数 | 值 |
| --- | --- |
| `images` | 最多 2 张 |
| `audio_url` | URL |
| `size` | string |
| `resolution` | `480P`, `720P`, `1080P` |
| `prompt_extend` | boolean |
| `negative_prompt` | string |
| `seed` | 0-2147483647 |
| `watermark` | boolean |
| `duration` | integer |

### 10.5 Comfly Seedance

`model_family=seedance`

| 参数 | 值 |
| --- | --- |
| `ratio` | `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16`, `9:21`, `keep_ratio`, `adaptive` |
| `duration` | `5`, `10` |
| `resolution` | `480p`, `720p`, `1080p` |
| `images` | 最多 2 张 |
| `generate_audio` | boolean |
| `return_last_frame` | boolean |
| `camerafixed` | boolean |
| `seed` | 0-2147483647 |
| `watermark` | boolean |

### 10.6 Comfly Grok Video

`model_family=grok`

| 参数 | 值 |
| --- | --- |
| `ratio` | `2:3`, `3:2`, `1:1`, `16:9`, `9:16` |
| `resolution` | `720P`, `1080P` |
| `duration` | `6`, `10` |
| `images` | 最多 7 张 |

## 11. 前端开发建议

1. 模型选择必须先按 `model_type` 过滤：文本会话只展示 `text`，图像会话和资产图只展示 `image`，视频只展示 `video`。
2. 模型切换后立即重建参数表单，清空不属于新模型 `request_keys` 的旧参数。
3. 生成方式切换时也要清空互斥字段：
   - `reference`：清空 `first_frame_url`、`last_frame_url`。
   - `first_last_frame`：清空 `uploaded_images`。
   - `storyboard`：清空所有外部参考图选择，只展示“使用当前故事版图像”。
4. 参数提交前做一次白名单过滤：只保留当前模型 `request_keys` 中的字段。
5. 所有 URL 字段只提交后端可访问的 HTTP(S) URL。
6. 项目型接口不要把旧 UI 字段混入 body。故事版相关请求 schema 已设置禁止额外字段。
7. 任务卡片显示建议：
   - `pending`：排队中，展示队列位置可读取 `extra.queue_snapshot`。
   - `running`：生成中，展示 `next_poll_seconds`。
   - `success`：展示结果并刷新业务详情。
   - `failed`：展示 `result` 或 `extra.failed_reason`。
8. 聊天消息页面建议用 `GET /messages?order=asc` 渲染时间线；历史列表和任务列表保持倒序。
9. 故事版生成视频的 `storyboard` 模式按钮，应在没有 `video_prompt` 或没有故事版图像时禁用，并给出提示。
10. 管理端模型表单需要展示倍率字段：
    - `model_multiplier`
    - `cache_multiplier`
    - `completion_multiplier`
    - `platform_multiplier`
