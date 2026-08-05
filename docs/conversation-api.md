# 对话型接口文档

基础路径：`/api/v1`

所有接口除特别说明外都需要登录，请在请求头中传：

```http
Authorization: Bearer <access_token>
```

统一响应格式：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "timestamp": "2026-05-28T10:00:00+08:00"
}
```

## 对话类型

| conversation_type | 说明         | 任务模式                                                             |
| ----------------- | ------------ | -------------------------------------------------------------------- |
| text              | 文本对话     | 异步多轮对话；后端维护完整成功轮次，返回`task_record_id`             |
| image             | 图像生成对话 | 异步生成，返回`task_record_id`，前端轮询任务状态                     |
| video             | 视频生成对话 | 异步生成，返回`task_record_id`，前端轮询任务状态                     |

## 推荐调用流程

1. 创建会话：`POST /conversations`
2. 发送消息：`POST /conversations/{conversation_id}/messages`
3. 如果返回 `task_record_id` 且 `task_status` 为 `pending` 或 `running`，查询任务状态
4. 对话型优先轮询：`GET /conversations/{conversation_id}/generation-tasks/{task_record_id}`
5. 通用任务也可轮询：`GET /task-records/{task_record_id}`
6. 轮询间隔优先使用响应里的 `next_poll_seconds`，其次使用响应头 `X-Next-Poll-Seconds`
7. 收到 429 时按响应头 `Retry-After` 或响应体 `data.retry_after_seconds` 延迟
8. `stop_polling=true` 或状态为 `success/failed` 后停止轮询
9. 页面切换或刷新后，调用消息列表或任务记录接口恢复状态

## 会话列表

### GET `/conversations`

查询当前用户的会话列表。

查询参数：

| 参数      | 类型    | 必填 | 默认值 | 说明            |
| --------- | ------- | ---- | ------ | --------------- |
| page      | integer | 否   | 1      | 页码，最小 1    |
| page_size | integer | 否   | 20     | 每页数量，1-100 |

成功响应 `data`：

```json
{
  "items": [
    {
      "id": "会话 UUID",
      "user_id": "用户 UUID",
      "title": "仙侠分镜测试",
      "conversation_type": "image",
      "ai_model_id": "模型 UUID",
      "is_enabled": true,
      "created_at": "2026-05-28T10:00:00+08:00",
      "updated_at": "2026-05-28T10:00:00+08:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

## 创建会话

### POST `/conversations`

请求体：

```json
{
  "title": "仙侠图像生成",
  "ai_model_id": "模型 UUID",
  "conversation_type": "image"
}
```

字段说明：

| 字段              | 类型   | 必填 | 说明                                  |
| ----------------- | ------ | ---- | ------------------------------------- |
| title             | string | 是   | 会话标题，1-128 字                    |
| ai_model_id       | UUID   | 是   | 默认使用的模型 ID                     |
| conversation_type | string | 否   | `text`、`image`、`video`，默认 `text` |

成功响应：`data` 为 `ConversationOut`。

## 会话详情

### GET `/conversations/{conversation_id}`

返回指定会话详情。`conversation_id` 必须属于当前用户。

成功响应：`data` 为 `ConversationOut`。

## 更新会话

### PATCH `/conversations/{conversation_id}`

请求体：

```json
{
  "title": "新的标题"
}
```

成功响应：`data` 为更新后的 `ConversationOut`。

## 删除会话

### DELETE `/conversations/{conversation_id}`

软删除会话。

成功响应：`data` 为删除后的 `ConversationOut`。

## 消息列表

### GET `/conversations/{conversation_id}/messages`

查询会话消息。响应头会设置 `Cache-Control: no-store`。

查询参数：

| 参数      | 类型    | 必填 | 默认值 | 说明            |
| --------- | ------- | ---- | ------ | --------------- |
| page      | integer | 否   | 1      | 页码            |
| page_size | integer | 否   | 50     | 每页数量，1-200 |
| order     | string  | 否   | desc   | `asc` 或 `desc` |

成功响应 `data`：

```json
{
  "items": [
    {
      "id": "消息 UUID",
      "conversation_id": "会话 UUID",
      "user_id": "用户 UUID",
      "role": "assistant",
      "content": "生成结果或任务状态提示",
      "message_type": "image",
      "extra": {
        "task_record_id": "任务 UUID",
        "task_status": "success"
      },
      "ai_model_id": "模型 UUID",
      "turn_id": "本轮 UUID；旧消息可能为 null",
      "sequence_no": 2,
      "status": "success",
      "client_message_id": null,
      "created_at": "2026-05-28T10:00:00+08:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 50
}
```

## 删除单条消息

### DELETE `/conversations/{conversation_id}/messages/{message_id}`

删除当前用户指定会话中的一条历史消息。删除后，该消息不会再出现在消息列表中。

路径参数：

| 参数            | 类型 | 必填 | 说明                              |
| --------------- | ---- | ---- | --------------------------------- |
| conversation_id | UUID | 是   | 会话 ID，必须属于当前用户         |
| message_id      | UUID | 是   | 消息 ID，必须属于该会话和当前用户 |

成功响应：`data` 为被删除的 `ConversationMessageOut`。

说明：

- 该接口是删除单条消息，不会删除整个会话。
- 如果删除的是带 `task_record_id` 的助手消息，并且关联任务仍为 `pending/running`，后端会先中断该任务并退回积分，再删除消息。
- 删除已完成任务的历史消息不会删除任务记录，也不会删除已生成的媒体文件。

## 发送消息

### POST `/conversations/{conversation_id}/messages`

文本、图像、视频对话统一使用该接口提交用户消息。

请求体：

```json
{
  "content": "继续完善刚才的女主角设定",
  "client_message_id": "web-20260804-0001",
  "ai_model_id": "可选，覆盖会话默认模型 UUID",
  "extra": {
    "temperature": 0.7
  }
}
```

字段说明：

| 字段              | 类型   | 必填 | 说明                                                               |
| ----------------- | ------ | ---- | ------------------------------------------------------------------ |
| content           | string | 是   | 用户输入内容                                                       |
| client_message_id | string | 否   | 文本消息幂等键，1-128字符；同一会话内建议每次发送生成一个唯一值    |
| ai_model_id       | UUID   | 否   | 本次消息使用的模型 ID                                              |
| extra             | object | 否   | 模型附加参数，会随任务记录保存；文本会话不能提交`extra.messages`   |

### 文本多轮规则

只对`conversation_type=text`生效：

- 每次新消息生成一个`turn_id`，用户消息和助手消息使用同一个`turn_id`。
- `sequence_no`是会话内稳定顺序；不要使用消息UUID判断先后。
- 用户消息写入后状态为`success`；助手消息按`pending → running → success/failed`变化。
- 下一轮只携带已完成且助手状态为`success`的完整问答轮次；待执行、执行中和失败回答不会进入模型上下文。
- 同一个文本会话同一时间只允许一个`pending/running`生成任务。重复发送不同消息返回HTTP 409。
- 相同`client_message_id`、内容、模型和`extra`重复提交时，返回原用户消息、助手消息和任务，不重复扣积分或创建任务。
- 相同`client_message_id`被用于不同内容、模型或`extra`时返回HTTP 409，错误码`40997`。
- 文本历史完全由后端构建。提交`extra.messages`返回HTTP 400，错误码`40013`。

并发冲突响应中的`data`：

```json
{
  "active_task_record_id": "正在执行的任务 UUID",
  "assistant_message_id": "对应助手消息 UUID"
}
```

### Comfly 聊天模型 `extra` 规则

当会话使用 `vendor=comfly` 且模型类型为 `text` 时，后端会按 Comfly `POST /v1/chat/completions` 文档收紧提交给厂商的参数。

可用能力：

| extra 字段                              | 类型               | 必填 | 说明                                                  |
| --------------------------------------- | ------------------ | ---- | ----------------------------------------------------- |
| capability / chat_mode                  | string             | 否   | `chat`、`analyze_image`、`analyze_video`；默认 `chat` |
| system_prompt                           | string             | 否   | 作为`system` 消息；为空时不传                         |
| image / image_url / images / image_urls | string 或 string[] | 否   | 图片 URL；图片分析、图像编辑时必填                    |
| video / video_url / videos / video_urls | string 或 string[] | 否   | 视频 URL；视频分析时必填                              |

允许透传给 Comfly Chat 的参数：

| 字段                                                       | 说明                |
| ---------------------------------------------------------- | ------------------- |
| temperature / top_p / presence_penalty / frequency_penalty | 必须是数字          |
| n / seen                                                   | 必须是整数          |
| stop                                                       | 字符串或字符串数组  |
| max_tokens                                                 | 自动限制在`1-65536` |
| response_format / logit_bias / user / tools / tool_choice  | 直接透传            |

注意：

- Comfly 文档中的图片分析和视频分析都使用 `content[].type=image_url`，因此视频 URL 也会被后端组织成 `image_url.url` 结构提交。
- 不允许客户端自定义`messages`；后端根据已落库的完整成功轮次生成厂商请求。
- `stream` 必须是 boolean；当前前端建议不传，后端默认按非流式调用。若模型返回 SSE，后端会聚合文本内容入库。
- 未列入上述白名单的 `extra` 字段不会提交给 Comfly Chat。

文本聊天示例：

```json
{
  "content": "概括这段剧情",
  "extra": {
    "capability": "chat",
    "system_prompt": "你是短剧编剧助手",
    "temperature": 0.7,
    "max_tokens": 2000
  }
}
```

视频分析示例：

```json
{
  "content": "分析这个视频中的人物动作",
  "extra": {
    "capability": "analyze_video",
    "video_url": "https://example.com/demo.mp4"
  }
}
```

### Comfly 绘图模型 `extra` 规则

当会话使用 `vendor=comfly` 且模型类型为 `image` 时，后端会按 Comfly 绘图模型文档收紧提交给厂商的参数。

图片生成 `POST /v1/images/generations` 允许的 body 参数：

| extra 字段                              | 类型               | 必填 | 说明                                                                                                                                  |
| --------------------------------------- | ------------------ | ---- | ------------------------------------------------------------------------------------------------------------------------------------- |
| size                                    | string             | 否   | 图片尺寸，例如`1024x1024`；后端也会根据模型和比例自动适配                                                                             |
| aspect_ratio                            | string             | 否   | 图片比例，例如`1:1`、`16:9`、`9:16`                                                                                                   |
| image / image_url / images / image_urls | string 或 string[] | 否   | 参考图 URL，会统一整理为`image` URL 数组                                                                                              |
| n                                       | integer            | 否   | 生成数量，`1-10`                                                                                                                      |
| quality                                 | string             | 否   | 仅透传 Comfly 支持的`auto`、`high`、`medium`、`low`、`standard`、`hd`；系统内部 `1k/2k/3k/4k` 只用于尺寸适配，不会作为 `quality` 透传 |
| response_format                         | string             | 否   | `url` 或 `b64_json`                                                                                                                   |
| style                                   | string             | 否   | `vivid` 或 `natural`                                                                                                                  |
| user                                    | string             | 否   | 终端用户标识，非空时透传                                                                                                              |

图片编辑 `POST /v1/images/edits` 使用 multipart，支持：

| extra 字段                              | 类型               | 必填 | 说明                                                 |
| --------------------------------------- | ------------------ | ---- | ---------------------------------------------------- |
| image / image_url / images / image_urls | string 或 string[] | 是   | 待编辑图片 URL，后端下载后作为 multipart`image` 上传 |
| mask / mask_url / mask_urls             | string 或 string[] | 否   | 蒙版 URL，后端下载后作为 multipart`mask` 上传        |
| size / aspect_ratio / image_size        | string             | 否   | 尺寸或比例参数，按模型能力适配                       |
| n                                       | integer            | 否   | 生成数量，`1-10`                                     |
| quality                                 | string             | 否   | 同图片生成                                           |
| response_format                         | string             | 否   | `url` 或 `b64_json`                                  |
| user                                    | string             | 否   | 终端用户标识，非空时透传                             |

异步参数：

| extra 字段       | 类型                 | 说明                                                    |
| ---------------- | -------------------- | ------------------------------------------------------- |
| async / is_async | boolean 或布尔字符串 | 后端固定使用`async=true`；如果显式传 `false` 会返回 400 |
| webhook          | string               | 非空时后端向 Comfly URL query 添加`webhook`             |

注意：

- `content` 会作为 Comfly 绘图接口的 `prompt`，不能为空。

### 对话型视频生成 `extra` 规则

当会话类型为 `video` 时，`POST /conversations/{conversation_id}/messages` 的 `extra` 可传视频生成参数。前端必须先通过 `GET /models/options?model_type=video` 获取模型的 `capabilities`，只展示 `capabilities.fields` 中存在的参数，只提交 `capabilities.request_keys` 中存在的扩展参数。

请求示例：

```json
{
  "content": "生成一段古风少年在雨中回头的视频",
  "extra": {
    "generation_mode": "reference",
    "resolution": "720p",
    "duration": 5,
    "ratio": "16:9",
    "uploaded_images": ["https://example.com/reference.png"]
  }
}
```

参考图上传流程：

1. 调用 `POST /api/v1/uploads/file` 上传图片。
2. 取上传响应里的 `data.url`。
3. 发送视频消息时，把该 URL 放进 `extra.uploaded_images`、`extra.image_urls` 或 `extra.media_items`。

示例：

```json
{
  "content": "参考上传图片生成一段人物转身视频",
  "extra": {
    "generation_mode": "reference",
    "uploaded_images": ["这里填 POST /uploads/file 返回的 data.url"],
    "duration": 5,
    "ratio": "16:9",
    "resolution": "720p"
  }
}
```

注意：`POST /uploads/file` 只负责上传文件，不会自动把图片绑定到某个会话消息。生成接口必须显式传参考图 URL。
后端也兼容把 `uploaded_images`、`uploadedImages`、`fileList`、`uploadedFiles` 等字段放在消息请求体顶层，但推荐统一放在 `extra` 内，便于前端状态管理。

参考视频上传流程：

1. 调用 `POST /api/v1/uploads/file` 上传视频。
2. 确认上传响应里的 `data.file_type` 为 `video`，取 `data.url`。
3. 发送视频消息时，把该 URL 放进 `extra.uploaded_videos`、`extra.video_url`、`extra.video_urls`、`extra.reference_video_url` 或 `extra.media_items`。
4. 上传接口只负责存文件，不会自动创建消息或绑定会话；没有第 3 步，后端会认为没有参考视频。

前端上传控件建议：视频入口不要写死 `accept="image/*"`，可使用 `accept="video/mp4,video/quicktime,.mp4,.mov"`。

示例：

```json
{
  "content": "参考这个视频的动作节奏和镜头运动，生成一段新视频",
  "extra": {
    "generation_mode": "reference",
    "reference_video_url": "这里填 POST /uploads/file 返回的 data.url",
    "duration": 5,
    "ratio": "16:9",
    "resolution": "720p"
  }
}
```

注意：参考视频生成不是单独的 `generation_mode`。前端仍然传 `generation_mode=reference`，后端根据参考视频字段判断这是“参考视频参与的多模态参考生成”。

参考音频上传流程：

1. 调用 `POST /api/v1/uploads/file` 上传音频。
2. 取上传响应里的 `data.url`。
3. 发送视频消息时，把该 URL 放进 `extra.uploaded_audios`、`extra.audio_url`、`extra.audio_urls`、`extra.reference_audio_url` 或 `extra.media_items`。
4. 同一个请求里必须同时传参考图片或参考视频；火山方舟 Seedance 2.0 不支持只有文本 + 音频或纯音频输入。

示例：

```json
{
  "content": "参考这段配乐的节奏，并结合参考图生成一段人物奔跑视频",
  "extra": {
    "generation_mode": "reference",
    "uploaded_images": [
      "这里填参考图的 data.url"
    ],
    "reference_audio_url": "这里填参考音频的 data.url",
    "duration": 5,
    "ratio": "16:9",
    "resolution": "720p"
  }
}
```

注意：参考音频生成也不是单独的 `generation_mode`。前端可以把入口文案写成“参考音频生成”，但接口仍然传 `generation_mode=reference`，后端根据参考音频字段判断这是“参考音频参与的多模态参考生成”。只传参考音频会返回 400，需要同时传参考图或参考视频。

常用字段：

| extra 字段                                 | 类型            | 必填 | 说明                                                                                   |
| ------------------------------------------ | --------------- | ---- | -------------------------------------------------------------------------------------- |
| generation_mode                            | string          | 否   | `text_to_video`、`reference` 或 `first_last_frame`；不传时后端按素材自动推断           |
| resolution                                 | string          | 否   | 清晰度，`480p`、`720p`、`1080p`、`4k`；具体可用值以当前模型 `capabilities.fields` 为准 |
| duration                                   | integer/string  | 否   | 视频时长；若当前模型`capabilities.durations` 存在，必须从该枚举中选择                  |
| ratio                                      | string          | 否   | 画面比例；仅当前模型`request_keys` 包含 `ratio` 时提交                                 |
| aspect_ratio                               | string          | 否   | 画面比例；仅当前模型`request_keys` 包含 `aspect_ratio` 时提交                          |
| uploaded_images                            | string[]        | 否   | 参考图 URL；仅`reference` 模式使用                                                     |
| uploaded_videos                            | string[]        | 否   | 参考视频 URL；仅`reference` 模式使用                                                   |
| video_url / video_urls                     | string/string[] | 否   | 参考视频 URL；仅`reference` 模式使用                                                   |
| reference_video_url / reference_video_urls | string/string[] | 否   | 参考视频 URL；推荐用于明确表达参考视频                                                 |
| uploaded_audios                            | string[]        | 否   | 参考音频 URL；仅`reference` 模式使用，不能单独使用                                     |
| audio_url / audio_urls                     | string/string[] | 否   | 参考音频 URL；仅`reference` 模式使用，不能单独使用                                     |
| reference_audio_url / reference_audio_urls | string/string[] | 否   | 参考音频 URL；推荐用于明确表达参考音频；必须同时传参考图或参考视频                     |
| first_frame_url                            | string          | 否   | 首尾帧模式首帧图                                                                       |
| last_frame_url                             | string          | 否   | 首尾帧模式尾帧图                                                                       |
| media_items / media / content              | array           | 否   | 可选媒体项数组；后端会按`role` 归一化为参考图、参考视频、参考音频或首尾帧              |
| return_last_frame                          | boolean         | 否   | 支持的模型可返回尾帧                                                                   |
| 其他字段                                   | -               | 否   | 只能来自当前模型`capabilities.request_keys`                                            |

处理规则：

判断逻辑：

| 输入情况                                                                                        | 后端标准模式       | 厂商侧含义                   | 前端建议                                     |
| ----------------------------------------------------------------------------------------------- | ------------------ | ---------------------------- | -------------------------------------------- |
| 有`first_frame_url` 或 `media_items[].role=first_frame`                                         | `first_last_frame` | 首帧/首尾帧生成              | 展示首帧、尾帧输入；清空普通参考图和参考视频 |
| 有`reference_video_url`、`video_url`、`uploaded_videos` 或 `media_items[].role=reference_video` | `reference`        | 参考视频参与的多模态参考生成 | 展示参考视频上传；不要传首尾帧字段           |
| 有`uploaded_images`、`image_urls` 或 `media_items[].role=reference_image`                       | `reference`        | 参考图参与的多模态参考生成   | 展示参考图上传；不要传首尾帧字段             |
| 有`reference_audio_url`、`audio_url`、`uploaded_audios` 或 `media_items[].role=reference_audio`，并且同时有参考图或参考视频 | `reference` | 参考音频参与的多模态参考生成 | 展示参考音频上传；仍需至少一个参考图或参考视频 |
| 只有`audio_url` / `reference_audio_url` / `uploaded_audios`                                     | 不合法             | 参考音频不能单独使用         | 必须同时传参考图或参考视频                   |
| 没有任何参考素材和首尾帧字段                                                                    | `text_to_video`    | 文生视频                     | 只展示文本、时长、比例、清晰度等参数         |

- 不传 `generation_mode` 时：有首尾帧字段推断为 `first_last_frame`；有参考图、参考视频或参考音频字段推断为 `reference`；否则推断为 `text_to_video`。
- 如果显式传 `generation_mode=reference_video`、`reference-video`、`video_to_video`、`video-to-video`、`参考视频生成`，后端会按兼容别名归一为 `reference`。
- 如果显式传 `generation_mode=reference_audio`、`reference-audio`、`audio_to_video`、`audio-to-video`、`参考音频生成`，后端也会按兼容别名归一为 `reference`，但仍要求同时传参考图或参考视频。
- 不传 `resolution` 时后端默认使用 `720p`。
- 火山方舟视频模型会按模型能力校验清晰度；例如 Seedance 2.0 Fast 不支持 `1080p/4k`，显式传入会返回 400。
- 后端会在提交厂商前按当前模型 `capabilities` 校验输入组合；例如当前模型不支持 `videos`/`media_limits.videos` 时，传参考视频会直接返回“当前模型不支持参考视频生成”，不会再提交到厂商接口。
- 前端展示入口时也必须按当前模型能力控制：参考视频入口要求当前模型支持视频输入；参考音频入口要求支持 `audio_url` 或 `media_limits.audios/audio`；首尾帧入口要求支持 `first_last_frame` 且支持图片输入。
- `generation_mode=text_to_video` 时只允许文本提示词，不能传参考图片、参考视频、参考音频或首尾帧图片。
- `generation_mode=reference` 时，参考图可以传 `uploaded_images`、`uploadedImages`、`images`、`image_urls`、`imageUrls`、`reference_images`，也可以在 `media_items`、`media` 或 `content` 中传 `role=reference_image` 的 `image_url` 项。后端会统一收敛为 `images/image_urls`。
- `generation_mode=reference` 时，参考视频可以传 `uploaded_videos`、`uploadedVideos`、`video_url`、`video_urls`、`videoUrl`、`videoUrls`、`reference_video_url`、`reference_video_urls`，也可以在 `media_items`、`media` 或 `content` 中传 `role=reference_video` 的 `video_url` 项。后端会统一收敛为 `videos/video_urls`。
- `generation_mode=reference` 时，参考音频可以传 `uploaded_audios`、`uploadedAudios`、`audio_url`、`audio_urls`、`audioUrl`、`audioUrls`、`reference_audio_url`、`reference_audio_urls`，也可以在 `media_items`、`media` 或 `content` 中传 `role=reference_audio` 的 `audio_url` 项。后端会统一收敛为 `audios/audio_urls`。
- 如果前端把上传响应对象整体传入，也可以放到 `extra.uploaded_files`、`extra.files` 或 `extra.attachments`；后端会根据 `file_type/content_type` 识别图片、视频、音频。
- `generation_mode=reference` 时，至少需要参考图片或参考视频；参考音频不能单独使用。
- 火山 Seedance 2.0 参考视频要求：格式为 `mp4/mov`，视频编码 `H.264/H.265`，音频编码 `AAC/MP3`，单个时长文档标注 `[2, 15]` 秒，Ark 实际允许到 `15.2` 秒；最多 3 个参考视频，所有参考视频总时长不超过 `15.2` 秒，单个不超过 200 MB，FPS `[24, 60]`，宽高比 `[0.4, 2.5]`，宽高边长 `[300, 6000]px`，总像素数 `[640*640, 3326*2494]`。
- 火山 Seedance 2.0 参考音频要求：格式为 `wav` 或 `mp3`，单段时长 `[2, 15]` 秒，最多 3 段，总时长不超过 15 秒，单段大小不超过 15 MB。
- 参考视频计费会按“有视频参考”计算；参考图、参考音频、首帧、尾帧不属于视频参考。
- `generation_mode=reference` 时，后端会丢弃 `first_frame_url`、`last_frame_url` 以及其 camelCase/别名字段，避免误触首尾帧生成。
- `generation_mode=first_last_frame` 时，`first_frame_url` 必填，`last_frame_url` 可选；也支持 `firstFrameUrl`、`lastFrameUrl`、`start_frame_url`、`end_frame_url` 等别名。
- `generation_mode=first_last_frame` 时，也可以用 `media_items`、`media` 或 `content` 传带角色的图片项，`role=first_frame` 表示首帧，`role=last_frame` 表示尾帧。
- 首尾帧模式会被后端统一整理成 `first_frame_url`、`last_frame_url` 和 `media_items=[role=first_frame, role=last_frame]`，并清空 `uploaded_images`、`images`、`image_urls`、`reference_images`、`media`、`content` 等外部参考输入。
- Comfly 视频模型最终接收的图片数组会固定按“首帧在前、尾帧在后”的顺序提交，避免被当作普通多图参考生成。
- 火山 Seedance 多模态参考模式下，参考图会提交为 `content[].role=reference_image`，参考视频提交为 `reference_video`，参考音频提交为 `reference_audio`。
- 火山 Seedance 首尾帧模式下，`first_frame_url` 必填；传入 `last_frame_url` 时会提交为 `role=last_frame`。该模式不会混入 `uploaded_images`、`images`、`image_urls` 或 `reference_images`。
- 火山 Seedance 首帧 / 首尾帧模式不能和多模态参考图、参考视频、参考音频混用。
- 火山 Seedance 画面比例支持 `adaptive`、`21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16`。
- 火山 Seedance 可透传的扩展参数包括 `callback_url`、`execution_expires_after`、`generate_audio`、`priority`、`return_last_frame`、`safety_identifier`、`seed`、`tools`、`watermark`；`frames`、`camera_fixed`、`service_tier`、`draft`、`draft_task_id` 会被拒绝。
- Comfly 视频模型使用统一异步接口，提交成功必须返回 `task_id`；前端无需关心厂商任务 ID，只轮询本地 `task_record_id`。
- Comfly 视频模型族参数以 `capabilities` 为准：`sora2` 使用 `aspect_ratio`，`veo` 使用 `aspect_ratio`，`wan` 使用 `resolution/size`，`seedance` 使用 `ratio`，`grok-video` 使用 `ratio`。
- 后端会在扣积分前完成清晰度归一化，因此预扣积分和最终结算使用同一清晰度。
- `image_mode=edit`、`capability=edit`，或传入 `mask` 时，会走图片编辑接口。
- 未列入模型能力白名单的 `extra` 字段不会提交给视频模型接口。

首尾帧模式示例，直接传 URL 字段：

```json
{
  "content": "从画面 A 自然过渡到画面 B",
  "extra": {
    "generation_mode": "first_last_frame",
    "resolution": "720p",
    "first_frame_url": "https://example.com/first.png",
    "last_frame_url": "https://example.com/last.png",
    "duration": 5,
    "ratio": "16:9",
    "return_last_frame": true
  }
}
```

首尾帧模式示例，使用带角色的媒体项：

```json
{
  "content": "从首帧动作自然发展到尾帧构图",
  "extra": {
    "generation_mode": "first_last_frame",
    "media_items": [
      {
        "type": "image_url",
        "image_url": {
          "url": "https://example.com/first.png"
        },
        "role": "first_frame"
      },
      {
        "type": "image_url",
        "image_url": {
          "url": "https://example.com/last.png"
        },
        "role": "last_frame"
      }
    ],
    "duration": 5,
    "ratio": "16:9"
  }
}
```

前端模型参数展示建议：

- 切换视频模型时，立即清空不属于新模型 `capabilities.request_keys` 的旧参数。
- `generation_mode=reference`：根据用户选择展示参考图、参考视频或参考音频上传；参考图推荐传 `uploaded_images`，参考视频推荐传 `reference_video_url` 或 `uploaded_videos`，参考音频推荐传 `reference_audio_url` 或 `uploaded_audios`，不要展示首尾帧输入。
- 只选择参考音频时，前端应阻止提交或提示用户再上传参考图/参考视频；后端也会返回 400。
- `generation_mode=first_last_frame`：展示 `first_frame_url` 和 `last_frame_url`，隐藏并清空参考图、参考视频和参考音频字段。
- `capabilities.media_limits.images` 存在时，上传参考图数量不得超过该值。
- `capabilities.media_limits.videos` 存在时，上传参考视频数量不得超过该值。
- `capabilities.media_limits.audios` 存在时，上传参考音频数量不得超过该值。
- `capabilities.fields[].options` 存在时必须使用下拉或分段选择，不允许自由输入。

成功响应 `data`：

```json
{
  "user_message": {
    "id": "用户消息 UUID",
    "role": "user",
    "content": "继续完善刚才的女主角设定",
    "message_type": "text",
    "extra": {},
    "ai_model_id": "模型 UUID",
    "turn_id": "文本会话轮次 UUID",
    "sequence_no": 1,
    "status": "success",
    "client_message_id": "web-20260804-0001",
    "created_at": "2026-05-28T10:00:00+08:00"
  },
  "assistant_message": {
    "id": "助手消息 UUID",
    "role": "assistant",
    "content": "任务已提交，正在生成中",
    "message_type": "text",
    "extra": {
      "task_record_id": "任务 UUID",
      "task_status": "pending"
    },
    "ai_model_id": "模型 UUID",
    "turn_id": "与用户消息相同的轮次 UUID",
    "sequence_no": 2,
    "status": "pending",
    "client_message_id": null,
    "created_at": "2026-05-28T10:00:00+08:00"
  },
  "points_cost": 2,
  "task_record_id": "任务 UUID",
  "task_status": "pending"
}
```

说明：

| 场景                          | 前端处理                                     |
| ----------------------------- | -------------------------------------------- |
| `task_record_id` 为空         | 说明接口已同步得到最终内容，可刷新消息列表   |
| `task_status=pending/running` | 使用`task_record_id` 轮询任务状态            |
| 页面切换后                    | 重新拉消息列表，或用任务记录接口恢复任务状态 |

## 重试失败的文本轮次

### POST `/conversations/{conversation_id}/messages/{user_message_id}/retry`

仅用于`conversation_type=text`，路径中的`user_message_id`必须是失败轮次的用户消息ID。请求体为空。

重试规则：

- 不重复创建用户消息，也不改变原`client_message_id`。
- 新建一个助手消息和任务，继续使用原`turn_id`，并分配新的`sequence_no`。
- 原失败任务已经退回的积分不会恢复；重试会按照本次模型调用重新扣费。
- 如果会话中已有文本任务正在执行，返回HTTP 409，错误码`40996`。
- 如果该轮次最新回答不是`failed`，返回HTTP 409，错误码`40995`。
- 迁移前没有`turn_id`的旧消息不能按轮次重试，返回HTTP 409，错误码`40999`。

成功响应与发送消息接口相同，`message`为`重试已提交`。

## 查询生成任务状态

### GET `/conversations/{conversation_id}/generation-tasks/{task_record_id}`

查询对话生成任务。该接口只查询本地任务状态，不直接请求外部模型 provider。

查询参数：

| 参数         | 类型    | 必填 | 默认值 | 说明                                        |
| ------------ | ------- | ---- | ------ | ------------------------------------------- |
| wait_seconds | integer | 否   | 0      | 兼容旧前端参数；当前不会长阻塞等待，最大 15 |

成功响应 `data`：

```json
{
  "task_record_id": "任务 UUID",
  "conversation_id": "会话 UUID",
  "assistant_message_id": "助手消息 UUID",
  "status": "running",
  "task_status": "running",
  "content": "任务处理中",
  "result": null,
  "message": "任务处理中",
  "failed_reason": null,
  "extra": {
    "next_poll_seconds": 10
  },
  "assistant_message": null,
  "stop_polling": false,
  "next_poll_seconds": 10,
  "created_at": "2026-05-28T10:00:00+08:00",
  "updated_at": "2026-05-28T10:00:02+08:00"
}
```

状态说明：

| status  | 说明             | 是否继续轮询 |
| ------- | ---------------- | ------------ |
| pending | 已创建，等待执行 | 是           |
| running | 执行中           | 是           |
| success | 成功             | 否           |
| failed  | 失败             | 否           |

响应头：

| Header              | 说明                                                 |
| ------------------- | ---------------------------------------------------- |
| Cache-Control       | 固定`no-store`，任务状态不要使用浏览器缓存           |
| X-Next-Poll-Seconds | 当任务仍在`pending/running` 时返回下一次建议轮询秒数 |

前端轮询协议：

| 规则                 | 说明                                                                                            |
| -------------------- | ----------------------------------------------------------------------------------------------- |
| 单任务单 poller      | 同一个`task_record_id` 同一时间只能有一个轮询器                                                 |
| 必须先校验任务 ID    | `task_record_id` 为空、`null`、`undefined`、`None` 时禁止启动轮询                               |
| 使用链式`setTimeout` | 不要使用固定`setInterval` 高频调用                                                              |
| 请求未完成不发下一次 | 上一次请求 resolve/reject 后，再按后端建议安排下一次                                            |
| 使用后端间隔         | 优先 body`next_poll_seconds`，其次 header `X-Next-Poll-Seconds`；兜底文本/图像 5 秒、视频 10 秒 |
| 处理 429             | 读取 header`Retry-After` 或 body `data.retry_after_seconds`，延迟后再试                         |
| 停止条件             | `stop_polling=true` 或 `status/task_status` 为 `success/failed`                                 |
| 清理条件             | 页面切换、组件卸载、弹窗关闭、任务完成、用户取消时必须清理定时器和中断请求                      |

前端伪代码：

```ts
const pollers = new Map<
  string,
  { timer?: number; controller?: AbortController }
>();

function stopTaskPolling(taskRecordId: string) {
  const poller = pollers.get(taskRecordId);
  if (!poller) return;
  if (poller.timer) window.clearTimeout(poller.timer);
  poller.controller?.abort();
  pollers.delete(taskRecordId);
}

function startTaskPolling(
  taskRecordId: string,
  request: () => Promise<Response>,
  onDone: (data: any) => void,
) {
  if (
    !taskRecordId ||
    taskRecordId === "None" ||
    taskRecordId === "null" ||
    taskRecordId === "undefined"
  )
    return;
  if (pollers.has(taskRecordId)) return;
  pollers.set(taskRecordId, {});

  const run = async () => {
    const poller = pollers.get(taskRecordId);
    if (!poller) return;
    poller.controller = new AbortController();

    try {
      const response = await request();
      const payload = await response.json();

      if (response.status === 429) {
        const retryAfter =
          Number(response.headers.get("Retry-After")) ||
          payload?.data?.retry_after_seconds ||
          5;
        poller.timer = window.setTimeout(run, retryAfter * 1000);
        return;
      }

      const data = payload.data;
      if (
        data.stop_polling ||
        data.status === "success" ||
        data.status === "failed"
      ) {
        stopTaskPolling(taskRecordId);
        onDone(data);
        return;
      }

      const next =
        data.next_poll_seconds ||
        Number(response.headers.get("X-Next-Poll-Seconds")) ||
        10;
      poller.timer = window.setTimeout(run, next * 1000);
    } catch (error) {
      if (!pollers.has(taskRecordId)) return;
      poller.timer = window.setTimeout(run, 5000);
    }
  };

  run();
}
```

注意：该接口只读本地任务状态，不会触发 provider 查询或 OSS 转存。外部 provider 结果由后台 Celery reconcile 更新到任务记录，Celery Beat 会补偿丢失的回收消息；用户关闭页面不会中断后台任务。已取得 provider `task_id` 的任务不会因通用本地超时而自动退积分。

## 旧 provider 任务查询

### GET `/conversations/{conversation_id}/tasks/{task_id}`

兼容旧前端通过 provider `task_id` 查询任务的接口。当前实现只读取本地任务记录，不直接请求外部 provider，不触发 OSS 转存。

成功响应 `data`：

```json
{
  "task_id": "provider task id",
  "task_status": "running",
  "content": "",
  "extra": {
    "task_record_id": "任务 UUID",
    "record_extra": {}
  }
}
```

新前端优先使用 `/generation-tasks/{task_record_id}`。

## 通用任务记录查询

对话型任务也可以通过通用任务接口查询：

建议轮询间隔：

| 任务类型     | 建议间隔                                                              |
| ------------ | --------------------------------------------------------------------- |
| 文本对话     | 以`next_poll_seconds` 或 `X-Next-Poll-Seconds` 为准；兜底不低于 `5s`  |
| 图像生成对话 | 以`next_poll_seconds` 或 `X-Next-Poll-Seconds` 为准；兜底不低于 `5s`  |
| 视频生成对话 | 以`next_poll_seconds` 或 `X-Next-Poll-Seconds` 为准；兜底不低于 `10s` |

### GET `/task-records/{task_record_id}`

返回字段会包含：

| 字段              | 说明                                     |
| ----------------- | ---------------------------------------- |
| status            | `pending/running/success/failed`         |
| result            | 成功结果或失败原因                       |
| extra             | provider 状态、消息 ID、下一次轮询建议等 |
| stop_polling      | 是否停止轮询                             |
| next_poll_seconds | 下一次建议轮询间隔                       |

响应头同样会返回：

| Header              | 说明               |
| ------------------- | ------------------ |
| Cache-Control       | `no-store`         |
| X-Next-Poll-Seconds | 下一次建议轮询秒数 |

该接口也只读本地任务记录，不触发外部 provider 查询或 OSS 转存。

### POST `/task-records/batch`

批量查询任务状态。当前页面同时存在多个进行中的图像/视频任务时，前端应优先使用该接口，避免为每个任务创建一个独立轮询器。

请求体：

```json
{
  "ids": ["任务 UUID", "任务 UUID"]
}
```

成功响应 `data`：

```json
{
  "items": [
    {
      "id": "任务 UUID",
      "status": "running",
      "stop_polling": false,
      "next_poll_seconds": 10
    }
  ],
  "total": 1,
  "stop_polling": false,
  "next_poll_seconds": 10
}
```

`data.next_poll_seconds` 是本批次未完成任务中的最小建议间隔；`data.stop_polling=true` 表示本批次任务都已结束。

## 常见错误

| HTTP 状态码 | 说明                         |
| ----------- | ---------------------------- |
| 400         | 请求参数或业务状态不合法     |
| 401         | 未登录或 token 无效          |
| 404         | 会话、消息或任务不存在       |
| 409         | 文本会话任务冲突、幂等键冲突或失败轮次不可重试 |
| 422         | 参数校验失败                 |
| 429         | 请求过于频繁或待处理任务过多 |
| 500/502     | 服务或模型 provider 异常     |
