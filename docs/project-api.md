# 项目型接口文档

基础路径：`/api/v1`

所有接口都需要登录，请在请求头中传：

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

## 项目型工作流

推荐流程：

1. 创建项目：`POST /projects`
2. 创建章节：`POST /projects/{project_id}/chapters`
3. 可选：章节文本处理：`POST /projects/{project_id}/chapters/{chapter_id}/processing`
4. 可选：分析人物/场景/道具：`POST /projects/{project_id}/chapters/{chapter_id}/asset-analyses/{asset_type}`
5. 管理人物、场景、道具资产
6. 生成资产图像：`POST /projects/{project_id}/{asset_kind}/{asset_id}/image-generation`
7. 分镜制作：`POST /projects/{project_id}/chapters/{chapter_id}/storyboards/analyze`
8. 可选：合并或再拆分分镜
9. 分镜细化字段生成：`POST /projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/refine`
10. 可选：故事板提示词生成：`POST /projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/storyboard-prompt-generation`
11. 可选：故事版图像生成：`POST /projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/image-generation`
12. 生成分镜视频：`POST /projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/video-generation`
12. 异步任务使用 `GET /task-records/{task_record_id}` 查询结果
13. 轮询间隔使用响应体 `next_poll_seconds` 或响应头 `X-Next-Poll-Seconds`
14. 收到 429 时使用响应头 `Retry-After` 或响应体 `data.retry_after_seconds` 延迟重试

说明：

| 类型 | 是否异步 | 查询方式 |
| --- | --- | --- |
| 章节文本处理 | 异步 | `/task-records/{task_record_id}` |
| 资产分析 | 异步 | `/task-records/{task_record_id}`，完成后刷新资产列表 |
| 分镜制作/细化/提示词生成 | 异步 | `/task-records/{task_record_id}`，完成后刷新分镜列表 |
| 资产图像生成 | 异步 | `/task-records/{task_record_id}`，完成后刷新对应资产详情 |
| 故事版图像生成 | 异步 | `/task-records/{task_record_id}`，完成后刷新分镜详情或故事版生成历史 |
| 分镜视频生成 | 异步 | `/task-records/{task_record_id}`，完成后刷新分镜详情 |

## 项目

### GET `/projects`

查询项目列表。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| keyword | string | 否 | - | 项目名称关键词，最长 64 |
| page | integer | 否 | 1 | 页码 |
| page_size | integer | 否 | 20 | 每页数量，1-100 |

成功响应 `data`：

```json
{
  "items": [
    {
      "id": "项目 UUID",
      "user_id": "用户 UUID",
      "style_id": "风格 UUID",
      "name": "仙侠短剧项目",
      "cover": "https://example.com/cover.png",
      "description": "项目描述",
      "generation_ratio": "16:9",
      "is_enabled": true,
      "style": {
        "id": "风格 UUID",
        "name": "国风写实"
      },
      "created_at": "2026-05-28T10:00:00+08:00",
      "updated_at": "2026-05-28T10:00:00+08:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

### POST `/projects`

创建项目。

请求体：

```json
{
  "name": "仙侠短剧项目",
  "cover": "https://example.com/cover.png",
  "description": "项目描述",
  "generation_ratio": "16:9",
  "style_id": "风格 UUID"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| name | string | 是 | 项目名称，1-128 字 |
| cover | string | 否 | 封面 URL，最长 512 |
| description | string | 否 | 项目描述 |
| generation_ratio | string | 是 | `21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16` |
| style_id | UUID | 是 | 项目风格 ID |

### GET `/projects/{project_id}`

查询项目详情。

### PATCH `/projects/{project_id}`

更新项目。请求体字段均可选：

```json
{
  "name": "新项目名称",
  "cover": "https://example.com/new-cover.png",
  "description": "新的描述",
  "generation_ratio": "9:16",
  "style_id": "风格 UUID"
}
```

### DELETE `/projects/{project_id}`

软删除项目。

## 章节

### GET `/projects/{project_id}/chapters`

查询章节列表。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| page | integer | 否 | 1 | 页码 |
| page_size | integer | 否 | 20 | 每页数量，1-100 |

### POST `/projects/{project_id}/chapters`

创建章节。

请求体：

```json
{
  "title": "第一章 青石村",
  "content": "章节正文",
  "sort_order": 0,
  "processing_prompt": "可选处理提示词"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| title | string | 是 | 章节标题，1-128 字 |
| content | string | 是 | 章节正文 |
| sort_order | integer | 否 | 排序值，默认 0 |
| processing_prompt | string | 否 | 章节处理提示词 |

成功响应：`data` 为 `ProjectChapterOut`。

### GET `/projects/{project_id}/chapters/{chapter_id}`

查询章节详情。

### PATCH `/projects/{project_id}/chapters/{chapter_id}`

更新章节。请求体字段均可选：

```json
{
  "title": "第一章 新标题",
  "content": "新的章节正文",
  "processed_content": "手动修订后的章节处理结果",
  "sort_order": 1,
  "processing_prompt": "新的处理提示词"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| title | string | 否 | 章节标题，1-128 字 |
| content | string | 否 | 章节原文正文；修改后若未同时传 `processed_content`，后端会清空旧处理结果并把 `process_status` 置为 `draft` |
| processed_content | string/null | 否 | 章节处理结果，支持用户手动修订；传入非空内容时 `process_status` 会置为 `success` |
| sort_order | integer | 否 | 排序值 |
| processing_prompt | string | 否 | 章节处理规则提示词；修改后若未同时传 `processed_content`，后端会清空旧处理结果并把 `process_status` 置为 `draft` |

### DELETE `/projects/{project_id}/chapters/{chapter_id}`

软删除章节。

## 章节文本处理

### POST `/projects/{project_id}/chapters/{chapter_id}/processing`

提交章节文本处理任务。

请求体：

```json
{
  "ai_model_id": "文本模型 UUID",
  "processing_prompt": "将章节整理为影视化分镜前文本",
  "extra": {
    "temperature": 0.3
  }
}
```

处理规则：

- 不传 `processing_prompt` 时，后端使用内置 `chapter_text_cleaning.md` 系统提示词，并把章节 `content` 写入 `{{input_text}}`。
- 传入 `processing_prompt` 时，后端会把该内容作为模型的 `system` 消息，把章节原文作为 `user` 消息提交给模型。
- `extra` 只用于模型参数，后端会过滤 `messages`、`prompt`、`content`、`input`、`message`、`system_prompt`，避免前端覆盖章节原文和系统规则。

成功响应 `data`：

```json
{
  "chapter": {
    "id": "章节 UUID",
    "process_status": "pending"
  },
  "task_record_id": "任务 UUID",
  "points_cost": 10
}
```

前端应使用 `task_record_id` 查询 `/task-records/{task_record_id}`。

## 资产分析

### POST `/projects/{project_id}/chapters/{chapter_id}/asset-analyses/{asset_type}`

从章节中分析人物、场景或道具。

路径参数：

| 参数 | 可选值 |
| --- | --- |
| asset_type | `character`、`scene`、`prop` |

请求体：

```json
{
  "ai_model_id": "文本模型 UUID",
  "analysis_prompt": "重点提取主要人物和视觉特征",
  "extra": {}
}
```

成功响应 `data`：

```json
{
  "task_record_id": "任务 UUID",
  "asset_type": "character",
  "status": "pending",
  "points_cost": 10
}
```

任务成功后刷新对应资产列表。

## 资产公共查询

### GET `/projects/{project_id}/assets/options`

获取人物、场景、道具选择项，常用于生成视频时选择参考资产。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| asset_type | string | 是 | - | `character`、`scene`、`prop` |
| keyword | string | 否 | - | 名称关键词 |
| limit | integer | 否 | 100 | 1-300 |

成功响应 `data`：

```json
{
  "items": [
    {
      "id": "资产 UUID",
      "asset_type": "character",
      "name": "沈砚",
      "reference_image": "https://example.com/image.png",
      "source_chapter_id": "章节 UUID"
    }
  ],
  "total": 1
}
```

## 人物资产

### GET `/projects/{project_id}/characters`

查询人物列表。

查询参数：`keyword`、`page`、`page_size`。

### GET `/projects/{project_id}/characters/options`

查询人物选择项。查询参数：`keyword`、`limit`。

### POST `/projects/{project_id}/characters`

创建人物。

请求体：

```json
{
  "name": "沈砚",
  "aliases": ["阿砚"],
  "identity": "青石村少年",
  "gender": "男",
  "age": "16",
  "appearance": "清瘦，眉眼坚毅",
  "personality": "隐忍坚韧",
  "relationship": "主角",
  "costume": "粗布短衫",
  "description": "贫寒凡人少年",
  "prompt": "男性少年，清瘦坚毅",
  "reference_image": "https://example.com/ref.png",
  "source_chapter_id": "章节 UUID",
  "source_content": "原文片段",
  "extra": {}
}
```

### GET `/projects/{project_id}/characters/{character_id}`

查询人物详情。

### PATCH `/projects/{project_id}/characters/{character_id}`

更新人物。请求体字段同创建接口，均可选。

### DELETE `/projects/{project_id}/characters/{character_id}`

软删除人物。

### POST `/projects/{project_id}/characters/{character_id}/image-generation`

提交人物图像生成任务。

请求体：

```json
{
  "ai_model_id": "图像模型 UUID",
  "generation_mode": "general",
  "prompt": "补充：正面半身像",
  "extra": {}
}
```

成功响应 `data`：

```json
{
  "task_record_id": "任务 UUID",
  "asset_type": "character",
  "asset_id": "人物 UUID",
  "status": "pending",
  "points_cost": 2
}
```

任务成功后，后端会把本次图像结果写入生成历史，并默认选中为当前资产的 `reference_image`。

资产图像生成最终提交给模型的 `prompt` 由后端拼接生成，包含：

- `画面风格`：项目绑定风格提示词 `project.style.prompt`。
- `模式提示词`：`generation_mode` 对应的常量模式提示词，例如 `general.md` 或 `profile_card.md`。
- `资产提示词`：对应资产的图像提示词，优先取资产 `prompt` 字段；为空时兜底使用 `description`，再为空使用 `name`。
- 固定约束：`请严格围绕该资产生成图像，不要添加与资产无关的主体内容。`

通用模式 `general` 和资料卡模式 `profile_card` 使用相同拼接结构，区别只在于读取的常量提示词文件不同。请求体 `prompt` 当前不参与最终模型提示词拼接。

#### Comfly 绘图模型 `extra` 规则

当 `ai_model_id` 对应模型为 `vendor=comfly` 且 `model_type=image` 时，后端会按 Comfly 绘图模型文档收紧提交给厂商的参数。

该规则同样适用于人物、场景、道具三个资产图像生成接口。

请求中的 `prompt` 当前不会拼入最终资产图像提示词。最终提交给模型的 `prompt` 由后端根据项目风格、模式提示词和资产提示词生成，不能为空。

图片生成 `POST /v1/images/generations` 允许的 body 参数：

| extra 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| size | string | 否 | 图片尺寸，例如 `1024x1024`；后端也会根据项目比例和模型能力自动适配 |
| aspect_ratio | string | 否 | 图片比例，例如 `1:1`、`16:9`、`9:16`；通常由项目 `generation_ratio` 自动带入 |
| image / image_url / images / image_urls | string 或 string[] | 否 | 参考图 URL，会统一整理为 `image` URL 数组 |
| n | integer | 否 | 生成数量，`1-10` |
| quality | string | 否 | 仅透传 Comfly 支持的 `auto`、`high`、`medium`、`low`、`standard`、`hd`；项目内部 `1k/2k/3k/4k` 只用于尺寸适配，不会作为 `quality` 透传 |
| response_format | string | 否 | `url` 或 `b64_json` |
| style | string | 否 | `vivid` 或 `natural` |
| user | string | 否 | 终端用户标识，非空时透传 |

图片编辑 `POST /v1/images/edits` 使用 multipart，支持：

| extra 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| image / image_url / images / image_urls | string 或 string[] | 是 | 待编辑图片 URL，后端下载后作为 multipart `image` 上传 |
| mask / mask_url / mask_urls | string 或 string[] | 否 | 蒙版 URL，后端下载后作为 multipart `mask` 上传 |
| size / aspect_ratio / image_size | string | 否 | 尺寸或比例参数，按模型能力适配 |
| n | integer | 否 | 生成数量，`1-10` |
| quality | string | 否 | 同图片生成 |
| response_format | string | 否 | `url` 或 `b64_json` |
| user | string | 否 | 终端用户标识，非空时透传 |

异步参数不会放入 Comfly body，会作为 URL query 提交：

| extra 字段 | 类型 | 说明 |
| --- | --- | --- |
| async / is_async | boolean 或布尔字符串 | 后端固定使用 `async=true`；如果显式传 `false` 会返回 400 |
| webhook | string | 非空时添加 `webhook` |

路由规则：

| 条件 | 厂商接口 |
| --- | --- |
| 默认 | `/v1/images/generations` |
| `generation_mode` 仍参与业务提示词，不直接决定厂商接口 | `/v1/images/generations` |
| `extra.image_mode=edit`、`extra.capability=edit`，或传入 `mask` | `/v1/images/edits` |

注意：

- 未列入上述白名单的 `extra` 字段不会提交给 Comfly 绘图接口。
- 空字符串可选参数会被后端丢弃。
- `n`、`response_format`、`quality`、`style`、`async/is_async` 类型或取值不合法时会返回 400。

## 场景资产

### GET `/projects/{project_id}/scenes`

查询场景列表。查询参数：`keyword`、`page`、`page_size`。

### GET `/projects/{project_id}/scenes/options`

查询场景选择项。查询参数：`keyword`、`limit`。

### POST `/projects/{project_id}/scenes`

创建场景。

请求体：

```json
{
  "name": "青石村",
  "location": "山脚村落",
  "time_of_day": "清晨",
  "environment": "青石小路，薄雾，远山",
  "atmosphere": "清冷朴素",
  "description": "主角生活的村庄",
  "prompt": "古代山村，青石路，晨雾",
  "reference_image": "https://example.com/ref.png",
  "source_chapter_id": "章节 UUID",
  "source_content": "原文片段",
  "extra": {}
}
```

### GET `/projects/{project_id}/scenes/{scene_id}`

查询场景详情。

### PATCH `/projects/{project_id}/scenes/{scene_id}`

更新场景。请求体字段同创建接口，均可选。

### DELETE `/projects/{project_id}/scenes/{scene_id}`

软删除场景。

### POST `/projects/{project_id}/scenes/{scene_id}/image-generation`

提交场景图像生成任务。请求体同人物图像生成。

## 道具资产

### GET `/projects/{project_id}/props`

查询道具列表。查询参数：`keyword`、`page`、`page_size`。

### GET `/projects/{project_id}/props/options`

查询道具选择项。查询参数：`keyword`、`limit`。

### POST `/projects/{project_id}/props`

创建道具。

请求体：

```json
{
  "name": "破旧木剑",
  "category": "武器",
  "appearance": "木质粗糙，有裂痕",
  "function": "练剑用",
  "description": "主角早期使用的练习木剑",
  "prompt": "破旧木剑，粗糙裂纹",
  "reference_image": "https://example.com/ref.png",
  "source_chapter_id": "章节 UUID",
  "source_content": "原文片段",
  "extra": {}
}
```

### GET `/projects/{project_id}/props/{prop_id}`

查询道具详情。

### PATCH `/projects/{project_id}/props/{prop_id}`

更新道具。请求体字段同创建接口，均可选。

### DELETE `/projects/{project_id}/props/{prop_id}`

软删除道具。

### POST `/projects/{project_id}/props/{prop_id}/image-generation`

提交道具图像生成任务。请求体同人物图像生成。

## 项目资产生成历史

人物、场景、道具图像生成成功后都会写入生成历史。用户可以查询某个资产的历史图像，并选择其中一张作为当前资产参考图。

### GET `/projects/{project_id}/assets/{asset_type}/{asset_id}/generation-history`

查询资产生成历史。`asset_type` 支持 `character`、`scene`、`prop`。查询参数：`page`、`page_size`。

### POST `/projects/{project_id}/assets/{asset_type}/{asset_id}/generation-history/{history_id}/select`

选择某条历史图像作为当前资产参考图。选择成功后会更新对应人物、场景或道具的 `reference_image`。

请求体：

```json
{
  "result_url": "https://example.com/image.png"
}
```

`result_url` 可不传；不传时使用该历史记录的默认 `result_url`。

## 分镜

分镜制作主流程：

```text
预处理文本 + 资产库
  ↓
步骤 1：分镜制作
输出 storyboard_units，只决定分成几镜
  ↓
步骤 2：分镜细化字段生成
针对某一条分镜输出 storyboard_execution_item，只决定这一镜怎么拍，不生成 video_prompt
  ↓
可选：故事板提示词生成
基于当前分镜输出 image_prompt、video_prompt、duration_suggestion、negative_prompt、ending_frame 等故事版字段
  ↓
可选：故事版图像生成
固定使用 gpt-image-2，根据 image_prompt 和参考资产生成故事版参考图
  ↓
步骤 3：生成视频
reference / first_last_frame 模式按分镜细化字段拼接最终视频提示词；storyboard 模式使用故事版参考图和 video_prompt 生成视频
```

字段命名说明：

- `image_prompt` 保留为数据库兼容字段。
- `image_prompt` 由独立的故事板提示词接口生成，用于故事版图像生成。
- `video_prompt` 由独立的故事板提示词接口生成，用于 `generation_mode=storyboard` 的视频生成。
- `reference` 和 `first_last_frame` 视频生成时，后端会把分镜细化字段拼成一段自然语言提交给视频模型。

### GET `/projects/{project_id}/chapters/{chapter_id}/storyboards`

查询章节分镜列表。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| page | integer | 否 | 1 | 页码 |
| page_size | integer | 否 | 50 | 每页数量，1-200 |

成功响应 `data`：

```json
{
  "items": [
    {
      "id": "分镜 UUID",
      "project_id": "项目 UUID",
      "chapter_id": "章节 UUID",
      "user_id": "用户 UUID",
      "ai_model_id": "文本模型 UUID",
      "shot_number": 1,
      "title": "沈砚测灵失败",
      "source_content": "原文片段",
      "event_goal": "展示测试结果",
      "scene_name": "仙门山门",
      "scene_state": "山门前人群聚集",
      "characters": ["沈砚", "仙师"],
      "props": ["测灵石"],
      "action": "沈砚接受测灵，测灵石没有亮。",
      "shot_size": "中景",
      "camera_angle": "三分之二侧角度",
      "camera_movement": "固定观察后轻微压近",
      "screen_execution": "沈砚站在测灵石前，测灵石成为画面焦点。",
      "character_action": "沈砚把手放上测灵石。",
      "character_expression": "沉默专注。",
      "dialogue": "“无灵根。”",
      "sound_effect": "人群低声。",
      "atmosphere": "山门前安静压抑。",
      "image_prompt": null,
      "video_prompt": null,
      "duration_suggestion": "8秒",
      "production_focus": "保持人物和测灵石关系。",
      "negative_prompt": "避免道具漂移。",
      "ending_frame": "画面最后停留在沈砚看向测灵石的中近景。",
      "split_reason": "测灵结果改变主角处境，是独立事件节点。",
      "extra": {},
      "is_enabled": true,
      "created_at": "2026-05-28T10:00:00+08:00",
      "updated_at": "2026-05-28T10:00:00+08:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 50
}
```

### POST `/projects/{project_id}/chapters/{chapter_id}/storyboards`

用户手动新增一个分镜。

默认追加到当前章节分镜末尾；如果传入 `shot_number`，后端会按该序号插入并自动重排所有启用分镜的 `shot_number`；如果传入 `insert_after_storyboard_id`，则优先插入到指定分镜之后。

请求体：

```json
{
  "insert_after_storyboard_id": "可选，插入到某个分镜后面",
  "shot_number": 3,
  "title": "少年推门而出",
  "source_content": "原文片段",
  "event_goal": "让角色主动离开村庄",
  "scene_name": "青石村",
  "scene_state": "晨雾笼罩青石路",
  "characters": ["沈砚"],
  "props": ["破旧木剑"],
  "action": "推门走出",
  "dialogue": "我一定要走出去。",
  "screen_execution": "薄雾中少年推开木门，迈入青石路。",
  "video_prompt": null,
  "extra": {}
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| insert_after_storyboard_id | UUID | 否 | 插入到指定分镜后面；优先级高于 `shot_number` |
| shot_number | integer | 否 | 插入位置，从 1 开始；不传则追加到末尾 |
| title | string | 否 | 分镜标题，默认 `新分镜` |
| source_content | string | 否 | 原文片段，默认空字符串 |
| event_goal | string | 否 | 分镜叙事目标 |
| scene_name | string | 否 | 主要场景 |
| scene_state | string | 否 | 场景状态或环境状态 |
| characters | string[] | 否 | 当前画面人物 |
| props | string[] | 否 | 当前画面叙事道具 |
| action | string | 否 | 当前分镜核心动作 |
| shot_size | string | 否 | 景别 |
| camera_angle | string | 否 | 拍摄角度 |
| camera_movement | string | 否 | 运镜设计 |
| screen_execution | string | 否 | 画面执行 |
| character_action | string | 否 | 角色动作执行 |
| character_expression | string | 否 | 角色表情或可见状态 |
| dialogue | string | 否 | 原文台词、旁白或内心独白 |
| sound_effect | string | 否 | 音效或环境声基础设计 |
| atmosphere | string | 否 | 画面氛围参考 |
| image_prompt | string | 否 | 故事板关键定格图提示词 |
| video_prompt | string | 否 | 兼容字段；分镜细化步骤不再生成，视频生成时由后端按分镜字段实时拼接最终提示词 |
| duration_suggestion | string | 否 | 建议时长 |
| production_focus | string | 否 | 制作重点 |
| negative_prompt | string | 否 | 负面规避词 |
| ending_frame | string | 否 | 视频最后停留画面 |
| split_reason | string | 否 | 拆分理由或新增说明 |
| extra | object | 否 | 附加信息 |

成功响应 `data` 为新增后的分镜详情，结构同分镜详情接口。

### POST `/projects/{project_id}/chapters/{chapter_id}/storyboards/analyze`

步骤 1：提交分镜制作任务，只决定“分成几镜”。

该步骤只生成事件分镜骨架，不生成镜头语言、图像提示词、视频提示词、时长、秒数或画幅。

请求体：

```json
{
  "ai_model_id": "文本模型 UUID",
  "analysis_prompt": "按连续事件拆分分镜，只输出事件分镜骨架",
  "extra": {
    "temperature": 0.2
  }
}
```

处理规则：

- 不传 `analysis_prompt` 时，后端使用内置 `storyboard_analysis.md`，并把章节 `processed_content`、人物资产、场景资产、道具资产写入提示词。
- 传入 `analysis_prompt` 时，后端会把该内容作为模型的 `system` 消息；章节 `processed_content` 和资产数据会作为 `user` 消息提交，前端不需要也不能自行拼接 `messages`。
- `extra` 只用于模型参数，后端会过滤 `messages`、`prompt`、`content`、`input`、`message`、`system_prompt`，避免覆盖分镜分析输入。

成功响应：

```json
{
  "task_record_id": "任务 UUID",
  "status": "pending",
  "points_cost": 10
}
```

任务成功后刷新分镜列表。

步骤 1 输出字段会落到分镜列表中：

| 字段 | 说明 |
| --- | --- |
| shot_number | 分镜序号，从 1 开始 |
| title | 分镜事件标题 |
| source_content | 直接摘取的原文片段 |
| event_goal | 当前分镜叙事目标 |
| scene_name | 主要可视觉化场景 |
| characters | 当前画面实际出现人物 |
| props | 当前画面实际出现且有叙事意义的道具 |
| action | 当前分镜核心行动 |
| dialogue | 当前分镜原文台词、旁白或内心独白 |
| split_reason | 拆分理由 |

### POST `/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/refine`

步骤 2：提交单条分镜细化字段生成任务，只决定“这一镜怎么拍”，不生成这一镜的视频模型提示词。

该步骤只基于路径中的 `storyboard_id` 对应分镜进行细化，不处理同章节其他分镜。它不会重新拆分、合并或排序分镜，不生成 `image_prompt` 或故事板关键帧图提示词。

请求体：

```json
{
  "ai_model_id": "文本模型 UUID",
  "extra": {}
}
```

说明：分镜细化字段生成固定使用后端系统提示词，前端不需要传额外提示词。
该接口不传入、不读取项目风格，只使用当前分镜内容和人物、场景、道具资产库。

提示词内容规则：

- 制作重点、负面规避词等内容只使用中文。
- 不使用“他、她、它、他们、对方、那人、这个、那个”等代名词。
- 人物、场景、道具优先使用资产库中的名称。

成功响应同分镜制作任务。

步骤 2 会更新以下字段：

| 字段 | 说明 |
| --- | --- |
| action | 当前分镜核心动作 |
| scene_state | 场景状态或环境状态，优先用于视频生成提示词 |
| shot_size | 景别 |
| camera_angle | 拍摄角度 |
| camera_movement | 运镜设计，先写具体运镜手法，再描述运动过程 |
| screen_execution | 画面执行与空间调度 |
| character_action | 角色动作执行 |
| character_expression | 角色表情或可见状态 |
| dialogue | 原文台词，可按镜头节奏拆分并标注简短时间段 |
| sound_effect | 音效或环境声基础设计，可按镜头节奏标注简短时间段 |
| atmosphere | 画面氛围参考，视频生成时优先于 `sound_effect` |
| duration_suggestion | 配合画面意境和台词长度的时长建议 |
| production_focus | 制作重点 |
| negative_prompt | 负面规避词 |
| ending_frame | 视频最后停留画面，用于稳定分镜衔接 |

### POST `/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/storyboard-prompt-generation`

可选步骤：提交单条分镜故事板提示词生成任务。

该接口只基于当前分镜生成故事版图像提示词和故事版视频提示词。它不会重新拆分分镜，不会改变分镜数量。

后端传入系统提示词的当前分镜 `storyboard_unit` 严格只有以下字段：

```json
{
  "shot_number": 1,
  "title": "分镜标题",
  "source_content": "分镜原文内容"
}
```

同时后端会传入当前项目的人物、场景、道具资产库，供模型绑定资产名称。

请求体：

```json
{
  "ai_model_id": "文本模型 UUID",
  "extra": {}
}
```

前置条件：

| 条件 | 说明 |
| --- | --- |
| 当前分镜存在 | 路径中的 `storyboard_id` 必须有效 |

成功响应同分镜制作任务。

任务成功后会更新：

| 字段 | 说明 |
| --- | --- |
| image_prompt | 故事版图像提示词，格式为“镜头一/镜头二/镜头三” |
| video_prompt | 故事版视频提示词，格式包含“通用要求、镜头一、镜头二、镜头三、最后停留画面” |
| duration_suggestion | 视频建议总时长，例如 `12秒` |
| negative_prompt | 负面规避 |
| ending_frame | 最后停留画面 |

`image_prompt` 格式示例：

```text
镜头一（4秒）：中景，沈砚站在仙门山门前，测灵石置于沈砚和仙师之间。
镜头二（4秒）：中近景，测灵石没有亮起，仙师站在测灵石旁宣布结果。
镜头三（4秒）：近景，沈砚低头停在测灵石前，突出测试失败后的处境。
```

`video_prompt` 格式示例：

```text
通用要求：当前分镜人物、场景、道具必须与资产一致。
镜头一（4秒）：缓慢推进运镜……
镜头二（4秒）：固定镜头……
镜头三（4秒）：极其缓慢的推进运镜……
最后停留画面：画面稳定停留在……
```

### POST `/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/image-generation`

生成当前分镜的故事版参考图。

请求体：

```json
{
  "aspect_ratio": "16:9",
  "prompt": "补充：光线更柔和",
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

- 该接口不传 `ai_model_id`，后端固定使用已启用的 `gpt-image-2` 图像模型。
- `aspect_ratio` 只允许 `16:9`、`9:16`、`1:1`、`4:3`、`3:4`、`3:2`、`2:3`、`21:9`，默认 `16:9`。
- `uploaded_images` 只传 URL 字符串数组。
- `character_ids`、`scene_ids`、`prop_ids` 可选；不传时后端会按分镜中的人物、场景、道具名称自动匹配资产。
- `extra` 只传图像模型允许参数，例如 `quality`、`n`、`response_format`。

故事版图像提示词由后端拼接，字段来源：

| 拼接项 | 来源 |
| --- | --- |
| 整体画面风格 | `project.style.prompt` |
| 当前分镜标题 | `storyboard.title` |
| 当前镜头图像提示词 | `storyboard.image_prompt`；为空时使用 `screen_execution -> action -> source_content` |
| 绑定人物 | `storyboard.characters` |
| 绑定场景 | `storyboard.scene_name` |
| 绑定道具 | `storyboard.props` |
| 负面规避 | `storyboard.negative_prompt` |
| 用户补充要求 | 请求体 `prompt` |

成功响应：

```json
{
  "task_record_id": "任务 UUID",
  "storyboard_id": "分镜 UUID",
  "aspect_ratio": "16:9",
  "status": "pending",
  "points_cost": 2,
  "next_poll_seconds": 10
}
```

任务成功后，后端会把图像结果写入：

```text
storyboard.extra.image_generation_result
storyboard.extra.image_generation_result_urls
storyboard.extra.image_generation_history_id
```

### POST `/projects/{project_id}/chapters/{chapter_id}/storyboards/merge`

合并两个或多个分镜。适用于用户认为相邻或多个分镜属于同一连续事件的情况。

请求体：

```json
{
  "storyboard_ids": ["分镜 UUID 1", "分镜 UUID 2"],
  "title": "沈砚测灵失败",
  "source_content": "合并后的原文片段，可不传",
  "event_goal": "展示测试结果",
  "scene_name": "仙门山门",
  "characters": ["沈砚", "仙师"],
  "props": ["测灵石"],
  "action": "沈砚接受测灵，测灵石没有亮，仙师宣布无灵根。",
  "dialogue": "“无灵根。”",
  "split_reason": "用户判断所选分镜属于同一连续事件，合并为一个分镜。",
  "extra": {}
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| storyboard_ids | UUID[] | 是 | 至少 2 个分镜 ID |
| title | string | 否 | 不传则自动生成合并标题 |
| source_content | string | 否 | 不传则拼接原分镜原文 |
| event_goal | string | 否 | 不传则合并原分镜叙事目标 |
| scene_name | string | 否 | 不传则使用第一个有效场景 |
| characters | string[] | 否 | 不传则去重合并人物 |
| props | string[] | 否 | 不传则去重合并道具 |
| action | string | 否 | 不传则拼接原分镜核心动作 |
| dialogue | string | 否 | 不传则拼接原分镜台词 |
| split_reason | string | 否 | 合并理由 |
| extra | object | 否 | 附加信息 |

成功响应 `data` 为合并后的完整分镜列表，结构同分镜列表接口。

### POST `/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/split`

将一个分镜再次拆分为两个或多个分镜。适用于用户认为某一条分镜内部事件过多，需要重新拆开。

请求体：

```json
{
  "units": [
    {
      "title": "沈砚接受测灵",
      "source_content": "原文片段 1",
      "event_goal": "推进人物行动",
      "scene_name": "仙门山门",
      "characters": ["沈砚", "仙师"],
      "props": ["测灵石"],
      "action": "沈砚把手放上测灵石。",
      "dialogue": "",
      "split_reason": "测灵动作成为独立事件节点。",
      "extra": {}
    },
    {
      "title": "仙师宣布结果",
      "source_content": "原文片段 2",
      "event_goal": "展示测试结果",
      "scene_name": "仙门山门",
      "characters": ["沈砚", "仙师"],
      "props": ["测灵石"],
      "action": "测灵石没有亮，仙师宣布无灵根。",
      "dialogue": "“无灵根。”",
      "split_reason": "结果改变人物处境，需要单独成镜。",
      "extra": {}
    }
  ]
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| units | object[] | 是 | 至少 2 条拆分后的分镜 |
| units[].title | string | 是 | 分镜标题 |
| units[].source_content | string | 是 | 对应原文片段 |
| units[].event_goal | string | 否 | 叙事目标 |
| units[].scene_name | string | 否 | 主要场景 |
| units[].characters | string[] | 否 | 实际出现人物 |
| units[].props | string[] | 否 | 叙事道具 |
| units[].action | string | 是 | 核心行动 |
| units[].dialogue | string | 否 | 原文台词 |
| units[].split_reason | string | 否 | 拆分理由 |
| units[].extra | object | 否 | 附加信息 |

成功响应 `data` 为拆分后的完整分镜列表，结构同分镜列表接口。

### GET `/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}`

查询分镜详情。

### PATCH `/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}`

更新分镜。

请求体字段均可选：

```json
{
  "shot_number": 1,
  "title": "少年推门而出",
  "source_content": "原文片段",
  "scene_name": "青石村",
  "scene_state": "晨雾笼罩青石路",
  "shot_size": "中景",
  "camera_angle": "平视",
  "camera_movement": "缓慢推进",
  "screen_execution": "薄雾中少年推开木门",
  "characters": ["沈砚"],
  "props": ["破旧木剑"],
  "action": "推门",
  "character_action": "握紧木剑",
  "character_expression": "坚定",
  "dialogue": "我一定要走出去。",
  "sound_effect": "木门吱呀声",
  "atmosphere": "木门开启后雾气轻微流动，空间安静。",
  "image_prompt": null,
  "video_prompt": null,
  "duration_suggestion": "5秒",
  "production_focus": "人物表情和晨雾层次",
  "negative_prompt": "避免字幕、标志、水印、人物变脸、肢体畸形。",
  "ending_frame": "画面最后停留在沈砚站在门外的中景。",
  "extra": {}
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| shot_number | integer | 分镜序号 |
| title | string | 分镜标题 |
| source_content | string | 原文片段 |
| event_goal | string | 分镜叙事目标 |
| scene_name | string | 主要场景 |
| scene_state | string | 场景状态或环境状态，优先用于视频生成提示词 |
| characters | string[] | 当前画面人物 |
| props | string[] | 当前画面叙事道具 |
| action | string | 当前分镜核心动作 |
| shot_size | string | 景别，步骤 2 生成 |
| camera_angle | string | 拍摄角度，步骤 2 生成 |
| camera_movement | string | 运镜设计，步骤 2 生成 |
| screen_execution | string | 画面执行，步骤 2 生成 |
| character_action | string | 角色动作执行，步骤 2 生成 |
| character_expression | string | 角色表情或可见状态，步骤 2 生成 |
| dialogue | string | 原文台词、旁白或内心独白 |
| sound_effect | string | 音效或环境声基础设计，可作为画面氛围参考 |
| atmosphere | string | 画面氛围参考，优先用于视频生成提示词 |
| production_focus | string | 制作重点 |
| negative_prompt | string | 负面规避词 |
| ending_frame | string | 视频最后停留画面，用于稳定分镜衔接 |
| image_prompt | string | 故事板关键定格图提示词，由 `/storyboard-prompt-generation` 生成 |
| video_prompt | string | 兼容字段；当前分镜细化步骤不再生成，视频生成时由后端按分镜字段实时拼接最终提示词 |
| split_reason | string | 拆分理由 |
| extra | object | 附加信息 |

### DELETE `/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}`

软删除分镜。

## 分镜视频生成

### POST `/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/video-generation`

步骤 3：提交分镜视频生成任务。

该步骤把当前分镜内容、参考图、比例参数、模型参数等送入视频模型。若请求体中传入 `prompt`，会作为用户补充要求追加到当前分镜的视频生成提示中。

`generation_mode=text_to_video`、`generation_mode=reference` 和 `generation_mode=first_last_frame` 时，后端最终传给视频模型的文本会按自然语言拼接，不直接把字段标签机械堆叠。拼接优先级如下：

| 模块 | 字段来源 |
| --- | --- |
| 生成模式约束 | 按 `generation_mode` 自动生成；首尾帧模式会强调首帧作为起始画面、尾帧作为结束画面，中间只补自然过渡 |
| 项目风格 | `project.style.prompt` |
| 视频建议时长 | `duration_suggestion` |
| 画面开场 | 上一分镜 `ending_frame` > `screen_execution` > `action` |
| 场景与空间 | `scene_name` + `scene_state` |
| 人物与道具 | 由画面开场、画面执行、角色动作及参考图共同约束 |
| 角色动作 | `character_action` > `action` |
| 表情与情绪变化 | `character_expression` |
| 镜头语言 | `shot_size`、`camera_angle`、`camera_movement` |
| 环境动态 / 氛围 | `atmosphere` |
| 声音与节奏 | `sound_effect`，仅作为动作节奏和氛围参考 |
| 台词处理 | `dialogue`，仅作为嘴型、停顿、视线和表演节奏参考，不生成字幕、气泡文字或屏幕文字 |
| 制作重点 | `production_focus` |
| 结尾画面 | `ending_frame` |
| 负面规避 | `negative_prompt` |
| 用户补充 | 请求体 `prompt` |

最终文本结构示例：

```text
请根据当前分镜和参考图生成一段连续镜头视频，保持分镜剧情、人物、场景、道具和参考图一致，不新增主要人物、场景或关键道具。

整体画面风格为：{project.style.prompt}

视频建议时长：{duration_suggestion}。所有动作、运镜、表情、台词、声音节奏、氛围变化和结尾画面都必须在该时长内自然完成，画面中不得出现任何时间文字。

画面发生在{scene_name}，场景状态为{scene_state}。

视频开场画面：{previous.ending_frame 优先；没有则 screen_execution；再没有则 action}

画面执行：{screen_execution}

角色动作：{character_action 或 action}

角色表情与可见状态：{character_expression}

景别为{shot_size}，拍摄角度为{camera_angle}，运镜方式为：{camera_movement}。

画面氛围参考：{atmosphere}

声音与节奏参考：{sound_effect}。声音与节奏只作为动作节奏和氛围参考，不生成字幕文字、声音文字或可视化音效文字。

角色按原文台词进行说话表演：{dialogue}。台词只用于嘴型、停顿、视线和表演节奏参考，画面中不得出现字幕、气泡文字、台词文字或任何屏幕文字。

制作重点：{production_focus}

视频最后停留在：{ending_frame}

避免出现：{negative_prompt}

用户补充要求：{prompt}。用户补充要求只能补充当前镜头的表现方式，不得覆盖当前分镜剧情、人物资产、场景资产、道具资产、参考图一致性、视频建议时长、制作重点和负面规避要求。
```

不同 `generation_mode` 的开头约束：

| generation_mode | 生成约束 |
| --- | --- |
| text_to_video | 根据当前分镜文本生成连续镜头，不依赖任何参考图、参考视频或参考音频 |
| reference | 根据当前分镜和参考图生成连续镜头，保持剧情、人物、场景、道具和参考图一致；所有 `uploaded_images` 都按普通参考图处理 |
| first_last_frame | 根据已提供的首帧图和尾帧图生成连续镜头；首帧图必须作为起始画面，尾帧图必须作为结束画面，中间只补自然连贯的动作、运镜和焦点变化 |
| storyboard | 根据当前分镜故事版参考图和 `video_prompt` 生成连续视频，保持故事版图像中的人物、服装、场景、道具、构图和空间关系 |

`generation_mode=storyboard` 时，最终文本固定按以下结构拼接：

```text
请根据当前分镜的故事版参考图、连续视频画面提示词、整体画面风格和用户补充要求，生成一段连续视频。
当前视频必须以故事版参考图为主要视觉依据，保持故事版参考图中的人物形象、服装、场景空间、道具位置、构图关系、画面氛围和镜头顺序一致。
整体画面风格：{project.style.prompt}
视频建议总时长：{duration_suggestion}
连续视频画面提示词：{video_prompt}
原文台词参考：{dialogue}
生成要求：
1. 必须参考故事版图像生成视频，故事版图像中的人物、服装、场景、道具、构图和空间关系优先保持一致。
2. 严格按照 video_prompt 中的“通用要求、镜头一、镜头二、镜头三、最后停留画面”进行视频生成。
3. 每个镜头的秒数必须与 video_prompt 中括号秒数一致。
4. 所有镜头总时长必须等于 {duration_suggestion}。
5. 镜头之间必须保持人物身份、服装、发型、场景空间、道具位置、道具状态、动作方向和视线方向连续。
6. 每个镜头只表现对应故事版图像的动态画面，不新增当前分镜之外的新剧情。
7. 可以根据 video_prompt 让故事版图像中的人物产生自然动作、表情变化、视线变化、轻微运镜和焦点变化。
8. 不得改变故事版图像中的主体身份、人物服装、主要场景、关键道具和主要构图关系。
9. 台词只用于人物嘴型、停顿、视线和表演节奏参考，不生成字幕、气泡文字或台词文字。
10. 画面中不得出现字幕、气泡文字、台词文字、屏幕文字、水印、标志或界面元素。
11. 视频最后必须停留在 video_prompt 中的“最后停留画面”。
用户补充要求：{prompt}
用户补充要求只能补充当前视频表现方式，例如动作强度、节奏、氛围、镜头运动或画面质感；不得覆盖当前分镜剧情、故事版参考图、人物资产、场景资产、道具资产、视频建议总时长、镜头连续性和负面规避要求。
负面规避：{negative_prompt}
```

请求体：

```json
{
  "ai_model_id": "视频模型 UUID",
  "generation_mode": "reference",
  "resolution": "720p",
  "return_last_frame": true,
  "prompt": "补充：镜头更慢，情绪更克制",
  "character_ids": ["人物 UUID"],
  "scene_ids": ["场景 UUID"],
  "prop_ids": ["道具 UUID"],
  "uploaded_images": ["https://example.com/uploaded.png"],
  "first_frame_url": null,
  "last_frame_url": null,
  "extra": {}
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| ai_model_id | UUID | 是 | 视频模型 ID |
| generation_mode | string | 否 | `text_to_video`、`reference`、`first_last_frame`、`storyboard`，默认 `reference` |
| resolution | string | 否 | 清晰度，`480p`、`720p`、`1080p`、`4k`，默认 `720p`；火山模型按模型能力校验，`4k` 仅 Seedance 2.0 标准模型支持 |
| return_last_frame | boolean | 否 | 是否要求模型返回尾帧，默认 `false`；火山引擎 Seedance 视频模型支持该参数 |
| prompt | string | 否 | 用户补充提示词 |
| character_ids | UUID[] | 否 | 参考人物资产 |
| scene_ids | UUID[] | 否 | 参考场景资产 |
| prop_ids | UUID[] | 否 | 参考道具资产 |
| uploaded_images | string[] | 否 | 上传的参考图 URL；`reference` 模式下全部按普通参考图处理 |
| first_frame_url | string | 否 | 首帧图 URL，主要用于 `first_last_frame` 模式 |
| last_frame_url | string | 否 | 尾帧图 URL，主要用于 `first_last_frame` 模式 |
| extra | object | 否 | 模型扩展参数，只传当前模型 `capabilities.request_keys` 中存在的字段 |

首尾帧兼容输入：

- `first_frame_url` 兼容 `firstFrameUrl`、`first_frame`、`firstFrame`、`start_frame_url`、`startFrameUrl`、`reference_first_frame_url`、`reference_start_frame_url` 等别名。
- `last_frame_url` 兼容 `lastFrameUrl`、`last_frame`、`lastFrame`、`end_frame_url`、`endFrameUrl`、`ending_frame_url`、`tailFrameUrl`、`reference_last_frame_url`、`reference_end_frame_url` 等别名。
- 也可以在请求体顶层或 `extra` 中通过 `media_items`、`media`、`content` 传带角色的图片项，`role=first_frame` 表示首帧，`role=last_frame` 表示尾帧。
- 推荐前端优先使用标准字段 `first_frame_url` 和 `last_frame_url`；媒体项形式主要用于兼容通用上传组件。

首尾帧媒体项示例：

```json
{
  "ai_model_id": "视频模型 UUID",
  "generation_mode": "first_last_frame",
  "resolution": "720p",
  "extra": {
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

输入来源建议：

| 输入 | 来源 |
| --- | --- |
| screen_execution / character_action / camera_movement / atmosphere / sound_effect / duration_suggestion 等 | 步骤 2 生成，后端用这些结构化字段拼接当前分镜的视频模型提示词 |
| character_ids | 角色资产参考图 |
| scene_ids | 场景资产参考图 |
| prop_ids | 道具资产参考图 |
| uploaded_images | 用户上传的额外参考图 |
| first_frame_url / last_frame_url | 明确指定首尾帧参考图 |
| extra.media_items / extra.media / extra.content | 兼容通用媒体项；首尾帧模式只读取 `role=first_frame` 和 `role=last_frame` 的图片项 |
| extra.ratio / extra.aspect_ratio | 视频比例参数，不传时默认使用项目比例 |
| extra.duration / extra.duration_seconds / extra.seconds | 视频时长参数；不传时自动使用分镜 `duration_suggestion` |
| resolution | 视频清晰度参数，默认 `720p` |
| return_last_frame | 返回尾帧参数；返回后后端会把尾帧图片转存 OSS，并写入任务和分镜 extra |

火山 Seedance 2.0 对接规则：

- 创建任务使用方舟 `POST /api/v3/contents/generations/tasks`，查询任务使用 `GET /api/v3/contents/generations/tasks/{task_id}`。
- `generation_mode=text_to_video` 会按文生视频提交，只发送文本提示词，不携带参考图、参考视频、参考音频、首帧或尾帧。
- `generation_mode=reference` 会按多模态参考生成提交，参考图片 role 为 `reference_image`，参考视频 role 为 `reference_video`，参考音频 role 为 `reference_audio`；至少需要参考图片或参考视频。
- `generation_mode=storyboard` 也按多模态参考生成提交，但只收集当前分镜故事版图像作为 `reference_image`；不会混入上传图、人物资产图、场景资产图、道具资产图、首尾帧图或 `extra.content/media/media_items`。
- `generation_mode=first_last_frame` 只提交首帧和可选尾帧：`first_frame_url` -> `role=first_frame`，`last_frame_url` -> `role=last_frame`；该模式会清空 `uploaded_images`、人物/场景/道具参考图、`extra.images`、`extra.image_urls`、`extra.content`、`extra.media`、`extra.media_items`、参考视频和参考音频，避免被当作多模态参考生成。
- 火山首尾帧模式必须传 `first_frame_url`；不能只传尾帧，也不能和多模态参考图片、参考视频、参考音频混用。
- 多模态参考限制：图片最多 9 张，视频最多 3 个，音频最多 3 个；音频不能单独使用，必须同时有图片或视频。
- 4K 仅 Seedance 2.0 标准模型支持；Seedance 2.0 Fast 不支持 `1080p/4k`，显式传入会返回 400。
- 火山比例支持 `adaptive`、`21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16`；不传时按模型默认和项目比例逻辑处理。
- 火山可透传参数包括 `callback_url`、`duration`、`execution_expires_after`、`generate_audio`、`priority`、`ratio`、`resolution`、`return_last_frame`、`safety_identifier`、`seed`、`tools`、`watermark`。
- Seedance 2.0 不支持 `frames`、`camera_fixed`、`service_tier`、`draft`、`draft_task_id`，传入会被后端拒绝。

Comfly 视频模型对接规则：

- 创建任务使用 Comfly 统一异步接口 `POST /v2/videos/generations`，查询任务使用 `GET /v2/videos/generations/{task_id}`。
- Comfly 视频提交成功必须返回 `task_id`，否则后端按模型响应格式错误处理。
- 前端不要硬编码参数，必须用 `GET /models/options?model_type=video` 返回的 `capabilities.fields` 渲染表单。
- 只提交当前模型 `capabilities.request_keys` 中存在的参数。
- `capabilities.media_limits.images` 存在时，参考图数量不得超过该限制。
- `generation_mode=first_last_frame` 时，后端会把图片输入固定整理为 `[首帧, 尾帧]` 顺序提交给 Comfly 的 `images`，不会保留普通参考图、参考视频、参考音频或自定义 `content/media/media_items`，避免首尾帧被误当成普通多图参考。
- 常见模型族：
  - `sora2`：`aspect_ratio`、`duration`、`hd`、`images`、`character_url`、`character_timestamps`、`notify_hook`、`watermark`、`private`。
  - `veo`：`aspect_ratio`、`duration`、`enhance_prompt`、`enable_upsample`、`images`。
  - `wan`：`images`、`audio_url`、`size`、`resolution`、`prompt_extend`、`negative_prompt`、`seed`、`watermark`、`duration`。
  - `seedance`：`ratio`、`duration`、`resolution`、`images`、`generate_audio`、`return_last_frame`、`camerafixed`、`seed`、`watermark`。
  - `grok-video`：`ratio`、`resolution`、`duration`、`images`。

故事版模式参考图规则：

- `generation_mode=storyboard` 只读取当前分镜已生成的故事版图像。
- 图像来源为 `storyboard.extra.image_generation_result`、`storyboard.extra.storyboard_image_result` 或 `storyboard.extra.image_generation_result_urls`。
- 该模式不会混入 `uploaded_images`、人物资产图、场景资产图、道具资产图、首帧图、尾帧图或 `extra.content/media/media_items`。
- 如果当前分镜没有 `video_prompt`，后端返回：`请先生成故事板提示词，再使用故事版生成视频`。
- 如果当前分镜没有故事版图像，后端返回：`请先生成故事版图像后再使用故事版生成视频`。
- 前端应在没有 `video_prompt` 或故事版图像时禁用 `storyboard` 生成按钮。

时长对接规则：

- 若 `extra` 中显式传了时长，后端会规范化为整数秒并写入 `duration`、`duration_seconds`、`seconds`。
- 若 `extra` 未传时长，后端会读取当前分镜的 `duration_suggestion`。
- `duration_suggestion` 支持 `4-6秒`、`约5秒`、`4.5秒`、`五到七秒` 等格式。
- 后端会取建议时长中的最大值，并向上取整；例如 `4-6秒` 会提交 `6`，`4.5秒` 会提交 `5`。
- 最终提交给视频模型和计费使用同一个规范化秒数；火山模型最低 4 秒，其他模型最低 5 秒，最高 15 秒。
- 故事版生成视频的时长和比例仍按多模态参考逻辑处理：比例默认项目 `generation_ratio`，时长优先 `extra` 显式值，否则使用分镜 `duration_suggestion`。

视频计费规则：

- 提交任务时即按实际视频参数预估扣费，任务成功后会用同一套规则结算。
- 计费公式为：`规范化秒数 * 当前模型对应的每秒单价`。
- `generation_mode=reference` 表示参考图生成，不等于参考视频生成。
- 只有 `extra` 中包含 `video`、`video_url`、`video_urls`、`videos`、`reference_video`、`reference_videos` 等真实视频输入时，才按参考视频单价计费。
- 仅传 `uploaded_images`、`character_ids`、`scene_ids`、`prop_ids`、`first_frame_url`、`last_frame_url` 时，按无参考视频单价计费。

成功响应：

```json
{
  "task_record_id": "任务 UUID",
  "storyboard_id": "分镜 UUID",
  "generation_mode": "reference",
  "resolution": "720p",
  "return_last_frame": true,
  "status": "pending",
  "points_cost": 20
}
```

若 `return_last_frame=true` 且模型返回尾帧，后端会把尾帧图片转存 OSS。完成后可在任务或分镜的 `extra` 中读取：

```json
{
  "video_generation_last_frame_url": "https://oss.example.com/story/image/last-frame.png",
  "video_generation_extra": {
    "display_last_frame_urls": ["https://oss.example.com/story/image/last-frame.png"],
    "oss_last_frame_urls": ["https://oss.example.com/story/image/last-frame.png"]
  }
}
```

视频生成成功后，后端会把视频结果写入分镜生成历史，并默认选中为当前分镜视频结果。

### GET `/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/generation-history`

查询当前分镜的视频生成历史。响应结构同资产生成历史，固定 `target_type=storyboard`、`media_type=video`。

### POST `/projects/{project_id}/chapters/{chapter_id}/storyboards/{storyboard_id}/generation-history/{history_id}/select`

选择某条历史视频作为当前分镜视频结果。选择成功后会更新分镜 `extra.video_generation_result`，若历史记录包含尾帧，也会同步 `extra.video_generation_last_frame_url`。

请求体：

```json
{
  "result_url": "https://example.com/video.mp4"
}
```

`result_url` 可不传；不传时使用该历史记录的默认 `result_url`。

## 通用任务记录查询

建议轮询间隔：

| 任务类型 | 建议间隔 |
| --- | --- |
| 文本类任务 | 以 `next_poll_seconds` 或 `X-Next-Poll-Seconds` 为准；兜底不低于 `5s` |
| 图像生成 / 资产图像生成 / 故事版图像生成 | 以 `next_poll_seconds` 或 `X-Next-Poll-Seconds` 为准；兜底不低于 `5s` |
| 视频生成 / 分镜视频生成 | 以 `next_poll_seconds` 或 `X-Next-Poll-Seconds` 为准；兜底不低于 `10s` |

### GET `/task-records/{task_record_id}`

所有项目型异步任务都使用该接口查询状态。该接口只读取本地任务记录，不直接请求外部模型 provider。

成功响应 `data` 常用字段：

```json
{
  "id": "任务 UUID",
  "business_type": "project",
  "business_id": "项目 UUID",
  "generation_type": "asset_image_generate",
  "status": "running",
  "title": "人物资产图像生成：沈砚",
  "result": null,
  "points_cost": 2,
  "extra": {
    "next_poll_seconds": 10
  },
  "stop_polling": false,
  "next_poll_seconds": 10,
  "created_at": "2026-05-28T10:00:00+08:00",
  "updated_at": "2026-05-28T10:00:02+08:00"
}
```

响应头：

| Header | 说明 |
| --- | --- |
| Cache-Control | 固定 `no-store`，任务状态不要使用浏览器缓存 |
| X-Next-Poll-Seconds | 当任务仍在 `pending/running` 时返回下一次建议轮询秒数 |

任务状态：

| status | 说明 | 前端处理 |
| --- | --- | --- |
| pending | 等待执行 | 继续轮询 |
| running | 执行中 | 继续轮询 |
| success | 成功 | 停止轮询，刷新业务详情/列表 |
| failed | 失败 | 停止轮询，展示 `result` 或 `extra.failed_reason` |

前端轮询协议：

| 规则 | 说明 |
| --- | --- |
| 单任务单 poller | 同一个 `task_record_id` 同一时间只能有一个轮询器 |
| 必须先校验任务 ID | `task_record_id` 为空、`null`、`undefined`、`None` 时禁止启动轮询 |
| 使用链式 `setTimeout` | 不要使用固定 `setInterval` 高频调用 |
| 请求未完成不发下一次 | 上一次请求完成后，再按后端建议安排下一次 |
| 使用后端间隔 | 优先 body `next_poll_seconds`，其次 header `X-Next-Poll-Seconds`；兜底文本/图像 5 秒、视频 10 秒 |
| 处理 429 | 读取 header `Retry-After` 或 body `data.retry_after_seconds`，延迟后再试 |
| 停止条件 | `stop_polling=true` 或 `status` 为 `success/failed` |
| 清理条件 | 页面切换、组件卸载、弹窗关闭、任务完成、用户取消时必须清理定时器和中断请求 |
| 任务完成后刷新业务资源 | 图像生成刷新资产详情；视频生成刷新分镜详情；分析任务刷新列表 |

### POST `/task-records/batch`

批量查询任务状态。当前页面同时存在多个进行中的图像/视频任务时，前端应优先使用该接口，避免为每个任务创建一个独立轮询器。

请求体：

```json
{
  "ids": ["任务 UUID", "任务 UUID"]
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| ids | UUID[] | 是 | 1-50 个任务 ID；只返回当前用户自己的任务 |

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

说明：

- `data.next_poll_seconds` 是本批次未完成任务中的最小建议间隔。
- `data.stop_polling=true` 表示本批次任务都已结束。
- 响应头同样返回 `Cache-Control: no-store` 和 `X-Next-Poll-Seconds`。

前端伪代码：

```ts
const pollers = new Map<string, { timer?: number; controller?: AbortController }>();

function stopTaskPolling(taskRecordId: string) {
  const poller = pollers.get(taskRecordId);
  if (!poller) return;
  if (poller.timer) window.clearTimeout(poller.timer);
  poller.controller?.abort();
  pollers.delete(taskRecordId);
}

function startTaskPolling(taskRecordId: string, request: () => Promise<Response>, onDone: (data: any) => void) {
  if (!taskRecordId || taskRecordId === "None" || taskRecordId === "null" || taskRecordId === "undefined") return;
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
        const retryAfter = Number(response.headers.get("Retry-After")) || payload?.data?.retry_after_seconds || 5;
        poller.timer = window.setTimeout(run, retryAfter * 1000);
        return;
      }

      const data = payload.data;
      if (data.stop_polling || data.status === "success" || data.status === "failed") {
        stopTaskPolling(taskRecordId);
        onDone(data);
        return;
      }

      const next = data.next_poll_seconds || Number(response.headers.get("X-Next-Poll-Seconds")) || 10;
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

## 整剧生产 Agent

当前阶段提供整剧生产实例的创建和状态控制。启动任务后只会创建
`source_analysis` 生产步骤；模型任务和 Celery 解析 Worker 在后续版本接入。

### POST `/projects/{project_id}/agent-productions`

创建整剧生产实例。同一项目重复提交内容完全相同的剧本时复用原始剧本文档，
但会创建新的生产实例。

```json
{
  "source_type": "text",
  "content": "整部剧本文本",
  "mode": "supervised",
  "max_points": 10000,
  "production_spec": {
    "text_model_id": "文本模型 UUID",
    "image_model_id": "图像模型 UUID",
    "video_model_id": "视频模型 UUID",
    "target_episode_count": 20,
    "target_episode_duration_seconds": 90,
    "default_shot_duration_seconds": 5,
    "video_resolution": "720p",
    "generate_audio": false,
    "retry_limit": 1,
    "pilot_episode_count": 1
  }
}
```

三个模型必须存在、已启用且类型分别为 `text`、`image` 和 `video`。

### GET `/projects/{project_id}/agent-productions`

分页查询项目下的整剧任务。支持 `status`、`page` 和 `page_size` 查询参数。

### GET `/agent-productions/{production_id}`

查询整剧任务详情，返回原始剧本元数据、生产步骤和确认点，不返回完整剧本文本。

### POST `/agent-productions/{production_id}/start`

启动整剧任务并创建唯一的 `source_analysis` 步骤。重复调用不会重复创建步骤。

### POST `/agent-productions/{production_id}/pause`

暂停后续调度。重复暂停为幂等操作。

### POST `/agent-productions/{production_id}/resume`

恢复处于 `paused` 或 `partially_failed` 状态的任务。不能用该接口绕过
`waiting_approval` 确认点。

### POST `/agent-productions/{production_id}/cancel`

取消后续调度，并把尚未开始或仍在排队的生产步骤标记为 `skipped`。已经执行的
外部任务不会在本接口中被强制删除。

## 常见错误

| HTTP 状态码 | 说明 |
| --- | --- |
| 400 | 参数或业务状态不合法 |
| 401 | 未登录或 token 无效 |
| 404 | 项目、章节、资产、分镜或任务不存在 |
| 422 | 参数校验失败 |
| 429 | 请求过于频繁、积分不足或待处理任务过多 |
| 500/502 | 服务或模型 provider 异常 |
