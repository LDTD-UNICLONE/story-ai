# 故事版生成接口文档

基础路径：`/api/v1`

本文档只覆盖故事版提示词生成、故事版图像生成、故事版生成视频三条接口。通用响应、任务轮询、模型选择规则见 `frontend-integration-api.md`。

## 1. 前端流程

推荐顺序：

1. 拉取分镜详情：`GET /projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}`
2. 生成故事版提示词：`POST /storyboard-prompt-generation`
3. 轮询任务：`GET /task-records/{task_record_id}`
4. 刷新分镜详情，确认 `image_prompt`、`video_prompt`、`duration_suggestion`、`negative_prompt` 已生成
5. 生成故事版图像：`POST /image-generation`
6. 轮询任务并刷新分镜详情，确认 `extra.image_generation_result` 或 `extra.storyboard_image_result` 有值
7. 选择视频模型，生成故事版视频：`POST /video-generation`，`generation_mode=storyboard`

## 2. 故事版提示词生成

```http
POST /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/storyboard-prompt-generation
```

请求体：

```json
{
  "ai_model_id": "文本模型 UUID",
  "extra": {}
}
```

后端传入模型的当前分镜字段严格只有：

```json
{
  "shot_number": 1,
  "title": "分镜标题",
  "source_content": "分镜原文内容"
}
```

成功响应：

```json
{
  "task_record_id": "任务 UUID",
  "status": "pending",
  "points_cost": 1,
  "next_poll_seconds": 5
}
```

任务成功后刷新分镜，读取：

```text
image_prompt
video_prompt
duration_suggestion
negative_prompt
ending_frame
```

## 3. 故事版图像生成

```http
POST /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/image-generation
```

请求体：

```json
{
  "aspect_ratio": "16:9",
  "prompt": "可选用户补充要求",
  "character_ids": ["人物 UUID"],
  "scene_ids": ["场景 UUID"],
  "prop_ids": ["道具 UUID"],
  "uploaded_images": ["https://example.com/ref.png"],
  "extra": {
    "quality": "2k",
    "n": 1
  }
}
```

严格规则：

- 不传 `ai_model_id`，后端固定使用启用状态的 `gpt-image-2`。
- `aspect_ratio` 只允许 `16:9`、`9:16`、`1:1`、`4:3`、`3:4`、`3:2`、`2:3`、`21:9`。
- `uploaded_images` 只传 URL 字符串数组。
- `character_ids`、`scene_ids`、`prop_ids` 可选；不传时后端按分镜名称自动匹配资产。
- `extra` 只传图像模型允许参数。

后端提示词拼接字段：

| 拼接项 | 来源 |
| --- | --- |
| 整体画面风格 | `project.style.prompt` |
| 当前分镜标题 | `storyboard.title` |
| 当前镜头图像提示词 | `storyboard.image_prompt`，为空时使用 `screen_execution -> action -> source_content` |
| 绑定人物 | `storyboard.characters` |
| 绑定场景 | `storyboard.scene_name` |
| 绑定道具 | `storyboard.props` |
| 参考资产 | 资产 ID 或名称匹配得到的人物、场景、道具 |
| 负面规避 | `storyboard.negative_prompt` |
| 用户补充要求 | 请求体 `prompt` |

成功响应：

```json
{
  "task_record_id": "任务 UUID",
  "storyboard_id": "分镜 UUID",
  "aspect_ratio": "16:9",
  "status": "pending",
  "points_cost": 1,
  "next_poll_seconds": 5
}
```

任务成功后刷新分镜，故事版图像在：

```text
extra.image_generation_result
extra.storyboard_image_result
extra.image_generation_result_urls
```

## 4. 故事版生成视频

```http
POST /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/video-generation
```

请求体：

```json
{
  "ai_model_id": "视频模型 UUID",
  "generation_mode": "storyboard",
  "resolution": "720p",
  "return_last_frame": false,
  "prompt": "可选用户补充要求",
  "character_ids": [],
  "scene_ids": [],
  "prop_ids": [],
  "uploaded_images": [],
  "first_frame_url": null,
  "last_frame_url": null,
  "extra": {
    "duration": 12,
    "ratio": "16:9"
  }
}
```

严格规则：

- `generation_mode` 必须传 `storyboard`。
- 必须先生成故事版提示词，即分镜存在 `video_prompt`。
- 必须先生成故事版图像，即分镜存在 `extra.image_generation_result` 或 `extra.storyboard_image_result`。
- `storyboard` 模式只使用当前分镜故事版图像作为参考图。
- 不要传外部参考图：`uploaded_images` 保持空数组。
- 不要传资产参考：`character_ids`、`scene_ids`、`prop_ids` 保持空数组。
- 不要传首尾帧：`first_frame_url`、`last_frame_url` 保持 `null`。
- `extra` 只传当前视频模型 `capabilities.request_keys` 中允许的字段。

如果缺少前置数据，后端会返回：

```text
请先生成故事板提示词，再使用故事版生成视频
请先生成故事版图像后再使用故事版生成视频
```

故事版视频提示词拼接：

```text
整体画面风格：{project.style.prompt}
视频建议总时长：{duration_suggestion}
连续视频画面提示词：{video_prompt}
原文台词参考：{dialogue}
用户补充要求：{payload.prompt}
负面规避：{negative_prompt}
```

时长和比例：

- 比例默认项目 `generation_ratio`。
- 显式时长优先使用 `extra.duration`。
- 未传显式时长时使用分镜 `duration_suggestion`。
- 火山模型最低 4 秒，其他模型最低 5 秒，最高 15 秒。
- 如果视频模型返回 `capabilities.durations`，前端只能展示这些枚举值。

成功响应：

```json
{
  "task_record_id": "任务 UUID",
  "storyboard_id": "分镜 UUID",
  "generation_mode": "storyboard",
  "resolution": "720p",
  "return_last_frame": false,
  "status": "pending",
  "points_cost": 1,
  "next_poll_seconds": 10
}
```

## 5. 生成历史

故事版图像历史：

```http
GET /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/generation-history?media_type=image
```

故事版视频历史：

```http
GET /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/generation-history?media_type=video
```

选择历史结果：

```http
POST /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/generation-history/{history_id}/select?media_type=image
POST /api/v1/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/generation-history/{history_id}/select?media_type=video
```

请求体：

```json
{
  "result_url": "https://example.com/result.png"
}
```

