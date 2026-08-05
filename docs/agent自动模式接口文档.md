# Agent 自动模式接口文档

| 项目 | 内容 |
| --- | --- |
| 文档版本 | 1.1 |
| 更新日期 | 2026-08-04 |
| API 前缀 | `/api/v1` |
| 适用模式 | `mode=automatic` |
| 鉴权 | `Authorization: Bearer <access_token>` |

本文档以当前后端代码为准，描述 Agent 四步工作流中自动模式的创建、状态查询、自动调度边界、逐集视频生产和交付接口。其他文档与本文冲突时，以当前代码及本文档为准。

## 1. 当前自动化边界

自动模式与审核模式共用同一套四步状态、项目数据和业务接口，不创建第二套项目或资产数据。

| 步骤 | `step_code` | 当前自动行为 | 当前仍需调用的确认接口 |
| --- | --- | --- | --- |
| 1 | `script_processing` | 点击开始后自动执行分集分析和资产分析 | `POST /script-package/confirm` |
| 2 | `asset_confirmation` | 资产可生成图片、上传图片和管理 | `POST /core-assets/confirm` |
| 3 | `storyboard_generation` | 确认资产后自动并发提交可处理分集的分镜分析 | 逐集或逐分镜组提交视频生成 |
| 4 | `video_editing` | 分镜完成的集自动开放审片页面 | 单集确认和交付仍使用现有接口 |

必须注意：

1. 当前代码尚未自动调用剧本包确认和资产确认接口，因此自动模式还不是从上传到成片完全无人值守。
2. 自动模式要求账户当前至少有 100 积分，并在创建、保存自动模式草稿配置和启动时校验。
3. 自动模式创建时必须提前选择视频模型；审核模式创建时禁止提交视频模型。
4. 新版四步工作流的视频必须按集或单个分镜组生成，不能使用旧版全剧视频批量接口。
5. 逐集视频生成请求目前仍要求提交 `video_model_id` 和 `video_resolution`。自动模式前端应直接使用项目中已保存的值，不再要求用户重复选择。
6. 自动模式不允许补充剧本，调用补充接口返回 `40982`。
7. 创建时不设置失败重试次数、试播集数、积分预算、目标集数、单集时长或默认分镜时长。
8. 文本模型固定使用 `gpt-5.5`，图像模型固定使用 `gpt-image-2`；二者在启动任务时由后端写入生产快照。

## 2. 通用协议

成功响应：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "timestamp": "2026-08-04T10:00:00+08:00"
}
```

失败响应：

```json
{
  "code": 40990,
  "message": "前置步骤尚未完成",
  "data": {
    "requested_step": 3,
    "required_step": 2,
    "current_step": 2,
    "required_status": "completed"
  },
  "timestamp": "2026-08-04T10:00:00+08:00"
}
```

- HTTP `422`：JSON 字段、枚举、UUID 或条件必填校验失败。
- HTTP `400`：multipart 条件字段或业务输入错误。
- HTTP `404`：项目、模型、资产或分集不存在。
- HTTP `409`：步骤门禁、版本冲突、幂等冲突或当前状态不允许操作。
- `expected_*_version` 必须来自最近一次查询响应；发生冲突后重新查询。
- `idempotency_key` 为 8～128 字符。同一动作重试复用原值，不同内容必须使用新值。
- 只在响应 `should_poll=true` 时按 `next_poll_seconds` 轮询。

## 3. 创建自动模式项目

### 3.1 积分准入条件

```http
GET /api/v1/points/balance
Authorization: Bearer <access_token>
```

```json
{
  "points_balance": 100
}
```

自动模式要求 `points_balance >= 100`：

- 创建文本或文件自动模式项目时校验。
- 保存 `mode=automatic` 的草稿配置时校验，包括从审核模式切换和继续修改自动模式配置。
- 正式调用 `/start` 时再次校验，防止创建后余额下降。
- 100 积分只是准入门槛，不会预扣、冻结或一次性消费。
- 后续文本、图像和视频任务继续按照实际模型调用逐笔扣费。
- 审核模式不执行该 100 积分准入校验。

余额不足返回 HTTP `400`、业务码 `40003`。前端应提示充值，不能循环重试。

### 3.2 获取可用视频模型

```http
GET /api/v1/models/options?model_type=video
```

创建请求中的 `video_model_id` 使用模型记录的 `id`，不是供应商侧的 `model_id`。服务端要求模型存在、已启用且 `model_type=video`。

### 3.3 粘贴剧本创建

```http
POST /api/v1/agent-productions/from-text
Content-Type: application/json
Authorization: Bearer <access_token>
```

请求：

```json
{
  "name": "雨夜旧宅",
  "content": "完整剧本文本……",
  "style_id": "11111111-1111-1111-1111-111111111111",
  "generation_ratio": "9:16",
  "video_resolution": "1080p",
  "mode": "automatic",
  "video_model_id": "77777777-7777-7777-7777-777777777777"
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 否 | 1～128 字；为空时从正文首行生成 |
| `content` | 是 | 去除首尾空白后不能为空 |
| `style_id` | 是 | 启用的整体风格 ID |
| `generation_ratio` | 是 | `21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16` |
| `video_resolution` | 是 | `480p`、`720p`、`1080p`、`4k` |
| `mode` | 是 | 固定为 `automatic` |
| `video_model_id` | 是 | 启用的视频模型记录 UUID |

JSON 使用严格字段校验。不得提交 `text_model_id`、`image_model_id`、重试次数、积分预算、试播配置、目标集数或时长字段。

### 3.4 上传文件创建

```http
POST /api/v1/agent-productions/from-file
Content-Type: multipart/form-data
Authorization: Bearer <access_token>
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `file` | 是 | `.txt`、`.md`、`.docx`、`.pdf`；文件名和扩展名不能为空 |
| `name` | 否 | Agent 项目名称 |
| `style_id` | 是 | 启用的整体风格 ID |
| `generation_ratio` | 是 | 画面比例 |
| `video_resolution` | 是 | 视频分辨率 |
| `mode` | 是 | 固定为 `automatic` |
| `video_model_id` | 是 | 启用的视频模型记录 UUID |

示例：

```bash
curl -X POST "$BASE_URL/api/v1/agent-productions/from-file" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@整部剧本.docx" \
  -F "name=雨夜旧宅" \
  -F "style_id=11111111-1111-1111-1111-111111111111" \
  -F "generation_ratio=9:16" \
  -F "video_resolution=1080p" \
  -F "mode=automatic" \
  -F "video_model_id=77777777-7777-7777-7777-777777777777"
```

上传或粘贴会创建 `project_kind=agent` 的独立内部项目，不使用项目中心的普通项目。

### 3.5 创建响应

```json
{
  "production_id": "22222222-2222-2222-2222-222222222222",
  "name": "雨夜旧宅",
  "status": "draft",
  "current_stage": "source",
  "source": {
    "source_type": "text",
    "file_name": null,
    "content_hash": "sha256",
    "character_count": 50231,
    "content_preview": "剧本开头……",
    "content_preview_truncated": true,
    "warnings": []
  },
  "style_id": "11111111-1111-1111-1111-111111111111",
  "generation_ratio": "9:16",
  "video_resolution": "1080p",
  "mode": "automatic",
  "video_model_id": "77777777-7777-7777-7777-777777777777",
  "configuration_required": false,
  "created_at": "2026-08-04T10:00:00+08:00"
}
```

前端保存 `production_id`。响应不公开内部 Agent 项目的 `project_id`。

### 3.6 查询和修改启动前配置

```http
GET /api/v1/agent-productions/{production_id}/configuration
PUT /api/v1/agent-productions/{production_id}/configuration
```

PUT 请求为全量配置：

```json
{
  "style_id": "11111111-1111-1111-1111-111111111111",
  "generation_ratio": "16:9",
  "video_resolution": "1080p",
  "mode": "automatic",
  "video_model_id": "77777777-7777-7777-7777-777777777777"
}
```

配置只能在 `status=draft`、`current_stage=source` 时修改。启动后返回 `40981`。保存自动模式配置时余额必须至少为 100；切换为审核模式时必须删除 `video_model_id`，后端同时清除自动模式的视频模型快照。

配置响应：

```json
{
  "production_id": "...",
  "style_id": "...",
  "style": {
    "id": "...",
    "name": "写实漫剧",
    "cover": "https://example.com/style.png",
    "version": "v1"
  },
  "generation_ratio": "16:9",
  "video_resolution": "1080p",
  "mode": "automatic",
  "video_model_id": "77777777-7777-7777-7777-777777777777",
  "configured": true,
  "configurable": true
}
```

## 4. 项目控制和状态查询

### 4.1 接口清单

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/agent-productions` | 当前用户 Agent 项目列表 |
| GET | `/agent-productions/{production_id}` | 项目详情 |
| DELETE | `/agent-productions/{production_id}` | 软删除项目 |
| POST | `/agent-productions/{production_id}/start` | 开始剧本分析 |
| POST | `/agent-productions/{production_id}/pause` | 暂停继续调度 |
| POST | `/agent-productions/{production_id}/resume` | 恢复失败或暂停任务 |
| POST | `/agent-productions/{production_id}/cancel` | 取消项目 |
| GET | `/agent-productions/{production_id}/workflow` | 四步及逐集状态 |
| GET | `/agent-productions/{production_id}/workbench` | 工作台、成本、控制器和下一动作 |

### 4.2 启动

```http
POST /api/v1/agent-productions/{production_id}/start
```

请求体为空。启动时后端执行：

1. 校验整体配置已确认，且风格、比例仍然有效。
2. 自动模式余额再次校验，必须至少为 100。
3. 解析并写入固定文本模型 `gpt-5.5`。
4. 解析并写入固定图像模型 `gpt-image-2`。
5. 保留创建时选择的 `video_model_id` 和 `video_resolution`。
6. 创建第一步分析任务并进入 `planning/source_analysis`。

创建和修改配置时已经校验视频模型。启动时不会重新选择视频模型；如果模型之后被停用，实际提交视频任务时返回 `40404`。固定文本或图像模型缺失、停用时返回 `40980`，不会开始剧本分析。

### 4.3 暂停、恢复和取消

- `pause`：阻止继续派发新任务；已经提交给模型供应商的任务可能仍会完成并落库。
- `resume`：从当前持久化阶段恢复，不重复已完成步骤；失败任务是否重新提交取决于对应任务状态。
- `cancel`：停止继续生产，保留已经产生的剧本、资产、分镜、媒体和积分记录。
- `DELETE`：软删除 Agent 项目；未完成生产先取消，列表和详情不再返回该项目。

### 4.4 项目列表轮询

`GET /agent-productions` 的列表项包含：

```json
{
  "id": "...",
  "name": "雨夜旧宅",
  "status": "running",
  "current_stage": "batch_storyboards",
  "mode": "automatic",
  "style_id": "...",
  "generation_ratio": "9:16",
  "video_resolution": "1080p",
  "video_model_id": "...",
  "active_task_count": 3,
  "has_active_tasks": true,
  "should_poll": true,
  "next_poll_seconds": 10
}
```

前端只在 `should_poll=true` 时轮询。自动模式处于 `planning` 或 `running` 时，即使当前查询瞬间没有活动任务，也可能返回 `should_poll=true`。

### 4.5 四步工作流

```http
GET /api/v1/agent-productions/{production_id}/workflow
```

```json
{
  "production_id": "...",
  "mode": "automatic",
  "current_step": 3,
  "steps": [
    {
      "step_number": 3,
      "step_code": "storyboard_generation",
      "title": "分镜生成",
      "status": "processing",
      "completed": false,
      "can_view": true,
      "is_current": true,
      "started_at": "2026-08-04T10:00:00+08:00",
      "completed_at": null,
      "extra": {}
    }
  ],
  "episodes": []
}
```

步骤状态：`not_started`、`processing`、`waiting_review`、`completed`、`failed`、`invalidated`。

页面访问权限只认 `steps[].can_view` 和 `episodes[].steps[].can_view`，不得根据步骤数字自行推导。未开放页面返回 `40990`。

## 5. 第一步：剧本处理

第一步在前端是一项任务，后端内部使用两个模型结果：

1. 分集规划：模型根据剧情结构决定合理集数并完整覆盖原文。
2. 资产分析：按分集提取人物、场景、道具和对应变体。

本步骤不生成参考图，也不使用用户设置的集数、单集时长或分镜时长。

### 5.1 查询结果

```http
GET /api/v1/agent-productions/{production_id}/script-package
```

核心响应：

```json
{
  "production_id": "...",
  "script_version": 1,
  "bible_version": 1,
  "status": "draft",
  "episodes": [],
  "characters": [],
  "character_variants": [],
  "scenes": [],
  "scene_variants": [],
  "props": [],
  "prop_variants": [],
  "warnings": []
}
```

人物、场景、道具必须分组展示。变体通过 `base_candidate_id` 关联主资产，并包含变体类型、触发原因、出现集数和原文证据。

### 5.2 当前确认接口

当前自动模式仍需调用：

```http
POST /api/v1/agent-productions/{production_id}/script-package/confirm
```

```json
{
  "expected_script_version": 1,
  "expected_bible_version": 1,
  "idempotency_key": "auto-script-confirm-v1"
}
```

确认前后端会校验：

- 分集结果不存在未解决的结构警告。
- 变体存在主资产。
- 变体包含类型、触发原因、有效集数和原文证据。
- 版本和幂等键未冲突。

成功后分集与资产落库，第一步完成，项目进入 `core_assets`。

### 5.3 自动模式禁止补充剧本

以下接口只支持审核模式：

```http
POST /api/v1/agent-productions/{production_id}/script-supplements/from-text
POST /api/v1/agent-productions/{production_id}/script-supplements/from-file
```

自动模式调用返回 HTTP `409`、业务码 `40982`。

## 6. 第二步：资产确认

`asset_type` 固定为 `character`、`scene`、`prop`。

### 6.1 资产管理接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET/POST | `/agent-productions/{production_id}/core-assets` | 查询或新增资产 |
| GET/PATCH/DELETE | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}` | 详情、编辑、删除 |
| POST | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants` | 新增变体 |
| DELETE | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}` | 删除变体 |
| PUT | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/reference-image` | 设置或清空主图 |
| POST | `/agent-productions/{production_id}/core-assets/image-generations` | 批量生成主图 |
| PUT | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/reference-image` | 设置或清空变体图 |
| POST | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/image-generation` | 根据主图生成变体图 |
| GET | `/agent-productions/{production_id}/core-assets/readiness` | 查询确认准备度 |
| POST | `/agent-productions/{production_id}/core-assets/confirm` | 选择全部有效资产并确认 |

主资产和变体图都支持手动上传后的 URL。模型生成变体图必须先存在主资产图，缺少时返回 `40984`。

默认资产图构图：

- 人物：16:9 白底图，左侧人物肖像，右侧人物三视图。
- 场景：16:9 完整场景图。
- 道具：16:9 白底图，左侧道具完整图，右侧细节角度。

### 6.2 当前确认接口

```http
POST /api/v1/agent-productions/{production_id}/core-assets/confirm
```

```json
{
  "expected_lock_version": 0,
  "idempotency_key": "auto-core-confirm-v1"
}
```

接口自动选择当前全部有效资产和变体。参考图不是确认的前置条件。

确认成功后：

1. 创建核心资产锁版本。
2. 第二步标记完成。
3. 项目进入 `batch_production`。
4. 后端立即按集并发提交待处理分集的分镜分析。
5. 自动控制器在容量恢复后继续补充派发尚未提交的分镜任务。

## 7. 第三步：分镜和视频生产

### 7.1 分镜分析规则

- 所有待处理集并发开始，不按集串行。
- 每集独立记录 `pending`、`running`、`ready`、`failed` 等状态。
- 分镜组按剧情顺序保存，每组总时长必须在 4～15 秒。
- 超过 15 秒的模型结果无效，必须重新拆分，不强制截断。
- 长对话需要增加人物反应、动作或环境镜头。
- 分镜提示词中的人物、场景、道具必须绑定已确认资产或变体。
- 固定提示词包含统一画风、不得出现字幕或文字叠加、纯画面、不要 BGM、不要配乐。

### 7.2 查询分镜进度

```http
GET /api/v1/agent-productions/{production_id}/storyboards
```

响应包含：

- `episode_count`
- `completed_episode_count`
- `remaining_episode_count`
- `analysis_complete`
- `episode_analyses`
- `failed_episodes`
- `active_task_count`
- `episodes`

前端边分析边展示已完成集。未完成集不能强行查看或修改具体分镜，返回 `40968`。

手动补位接口：

```http
POST /api/v1/agent-productions/{production_id}/storyboards/generations
```

```json
{
  "expected_core_asset_lock_version": 1,
  "idempotency_key": "storyboards-recover-v1"
}
```

### 7.3 分镜组编辑

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards` | 新增分镜组 |
| PATCH | `/agent-productions/{production_id}/storyboards/{storyboard_id}` | 自动保存分镜修改 |
| POST | `/agent-productions/{production_id}/storyboards/{storyboard_id}/copy` | 复制分镜组 |
| DELETE | `/agent-productions/{production_id}/storyboards/{storyboard_id}` | 删除分镜组 |
| PUT | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/order` | 调整本集顺序 |
| GET | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/asset-options` | 查询可绑定资产和变体 |

提示词中的资产引用使用稳定 token：

```text
{{asset:character_1}}
{{asset:scene_1}}
{{asset:prop_1}}
```

前端 `@` 选择资产后必须保存为 token，不能只保存显示名称。引用未绑定或已删除资产时返回 `40962`。

### 7.4 自动模式视频默认配置

项目默认视频参数通过以下接口读取：

```http
GET /api/v1/agent-productions/{production_id}/configuration
```

自动模式前端不再展示二次模型选择弹窗，直接使用响应中的：

```json
{
  "video_model_id": "77777777-7777-7777-7777-777777777777",
  "video_resolution": "1080p"
}
```

当前逐集和逐分镜组生成 Schema 仍要求把这两个字段放入请求体。它们不能省略。

单个分镜组允许保存覆盖配置：

```http
PUT /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-config
```

```json
{
  "expected_core_asset_lock_version": 1,
  "expected_storyboard_revision": 3,
  "expected_config_version": 0,
  "video_model_id": "77777777-7777-7777-7777-777777777777",
  "video_resolution": "1080p",
  "estimated_duration_seconds": 8
}
```

### 7.5 按集生成视频

```http
POST /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/video-generations
```

```json
{
  "expected_core_asset_lock_version": 1,
  "expected_episode_revision": 4,
  "idempotency_key": "automatic-episode-1-video-v1",
  "video_model_id": "77777777-7777-7777-7777-777777777777",
  "video_resolution": "1080p"
}
```

自动编排端应对每个 `ready` 分集分别提交该接口，不能把多集放进一个请求。

### 7.6 生成单个分镜组视频

```http
POST /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-generations
```

```json
{
  "expected_core_asset_lock_version": 1,
  "expected_episode_revision": 4,
  "expected_storyboard_revision": 3,
  "expected_video_config_version": 1,
  "duration_seconds": 8,
  "idempotency_key": "automatic-storyboard-video-v1",
  "video_model_id": "77777777-7777-7777-7777-777777777777",
  "video_resolution": "1080p",
  "prompt": "镜头推进稍慢"
}
```

生成前后端按提示词中资产首次出现顺序加载绑定资产图片，并把 token 编译为 `@图片1`、`@图片2` 等多模态引用。绑定资产缺图或超过模型参考图上限时，任务不会提交。

### 7.7 查询和选择视频版本

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/agent-productions/{production_id}/episodes/{chapter_id}/videos` | 本集视频状态 |
| GET | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-versions` | 分镜组全部视频版本 |
| PUT | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/primary-video` | 选择主要视频 |

一个分镜组存在多个成功版本且 `selection_required=true` 时：

```json
{
  "expected_selection_revision": 1,
  "history_id": "...",
  "result_url": "https://oss.example.com/video.mp4"
}
```

## 8. 第四步：审片和交付

任意一集分镜完成后，该集第四步立即开放；尚未生成视频的分镜组仍按顺序展示。

### 8.1 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/agent-productions/{production_id}/review` | 全剧审片摘要 |
| GET | `/agent-productions/{production_id}/episodes/{chapter_id}/video-timeline` | 本集顺序时间线 |
| POST | `/agent-productions/{production_id}/review-issues` | 记录审片问题 |
| PATCH | `/agent-productions/{production_id}/review-issues/{issue_id}` | 解决或忽略问题 |
| POST | `/agent-productions/{production_id}/episodes/{chapter_id}/approve` | 确认本集 |
| GET | `/agent-productions/{production_id}/delivery-readiness` | 查询交付准备度 |
| POST/GET | `/agent-productions/{production_id}/deliveries` | 创建或查询交付 |
| POST/GET | `/agent-productions/{production_id}/jianying-exports` | 创建或查询剪映导出 |

时间线只读取第三步选择的主要视频，不会暂停或修改第三步生成任务。无视频时返回：

```json
{
  "video_status": "not_started",
  "video_url": null,
  "video_history_id": null
}
```

### 8.2 确认单集

```http
POST /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/approve
```

```json
{
  "expected_lock_version": 0,
  "idempotency_key": "approve-episode-1-v1"
}
```

本集所有分镜必须存在已选择的有效视频，且没有未解决的 `blocking` 问题。

### 8.3 导出剪映草稿

```http
POST /api/v1/agent-productions/{production_id}/jianying-exports
```

```json
{
  "platform": "windows",
  "jianying_version": "10.8",
  "draft_name": "雨夜旧宅",
  "chapter_ids": [],
  "idempotency_key": "jianying-windows-v1"
}
```

`platform` 支持 `windows`、`macos`。`chapter_ids=[]` 表示全部有效集。通过以下接口轮询详情：

```http
GET /api/v1/agent-productions/{production_id}/jianying-exports/{export_id}
```

当 `status=completed` 时使用 `output_url` 下载 ZIP。

## 9. 生产监控和失败处理

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/agent-productions/{production_id}/matrix` | 按集、分镜和媒体查看生产矩阵 |
| GET | `/agent-productions/{production_id}/exceptions` | 查询失败和内容审核异常 |
| GET | `/agent-productions/{production_id}/events` | 查询用户及系统事件 |
| GET | `/agent-productions/{production_id}/costs` | 查询实际积分明细 |
| POST | `/agent-productions/{production_id}/jobs/retry` | 用户明确重试失败任务 |
| POST | `/agent-productions/{production_id}/jobs/skip` | 跳过任务或使用替代媒体 |

自动模式没有用户配置的失败重试次数。业务生成失败后不会因为创建参数自动重新生成；需要用户通过 `/jobs/retry` 或具体生成接口明确发起。

`GET /workbench` 同时返回控制器状态：

```json
{
  "controller": {
    "status": "idle",
    "lease_expires_at": null,
    "attempt_count": 3,
    "last_started_at": "2026-08-04T10:00:00+08:00",
    "last_finished_at": "2026-08-04T10:00:01+08:00",
    "last_error": null,
    "last_result": { "advanced": true }
  },
  "next_action": "monitor_storyboards",
  "available_actions": ["monitor_storyboards", "pause", "cancel"]
}
```

控制器的租约重试用于防止任务丢失和重复派发，不代表重新调用模型。所有模型任务仍必须依赖幂等记录避免重复扣费。

## 10. 关键错误码

| 业务码 | HTTP | 含义 | 建议处理 |
| --- | --- | --- | --- |
| `40003` | 400 | 自动模式余额不足 100，或实际任务积分不足 | 提示充值后由用户重试 |
| `40059` | 400 | multipart 自动模式缺少视频模型，或审核模式错误提交模型 | 检查 `mode` 与 `video_model_id` |
| `40057` | 400 | DOCX 损坏或压缩结构异常 | 重新导出 DOCX |
| `40064` | 400 | PDF/DOCX 解析进程异常 | 重新导出文件后上传 |
| `40801` | 408 | PDF/DOCX 解析超时 | 拆分或重新导出文件 |
| `40404` | 404 | 视频模型不存在、停用或类型错误 | 重新查询视频模型列表 |
| `40430` | 404 | Agent 项目不存在或已删除 | 返回项目列表 |
| `40434` | 404 | 核心资产不存在 | 刷新资产列表 |
| `40932` | 409 | 当前阶段不允许修改或确认资产 | 刷新工作流 |
| `40933` | 409 | 核心资产锁版本冲突 | 重新查询资产 |
| `40941` | 409 | 剧本处理版本冲突 | 重新查询剧本包 |
| `40942` | 409 | 资产分析版本冲突 | 重新查询剧本包 |
| `40943` | 409 | 资产变体结构无效 | 修复返回的变体 |
| `40949` | 409 | 剧本仍有结构警告 | 展示警告并阻止确认 |
| `40962` | 409 | 分镜提示词引用未绑定资产 | 刷新资产选项 |
| `40968` | 409 | 本集分镜尚未分析完成 | 根据任务状态轮询 |
| `40972` | 409 | 本集仍有未就绪视频 | 等待或重新生成 |
| `40973` | 409 | 仍有阻断审片问题 | 先处理问题 |
| `40980` | 409 | 固定文本或图像模型未配置 | 管理端配置模型 |
| `40981` | 409 | 整体配置不完整或启动后修改 | 刷新配置与项目状态 |
| `40982` | 409 | 自动模式不能补充剧本 | 隐藏补充入口 |
| `40984` | 409 | 变体生成缺少主资产图 | 先上传或生成主图 |
| `40990` | 409 | 前置步骤未完成 | 按 `can_view` 控制页面 |

自动模式 JSON 创建或配置请求缺少 `video_model_id` 时，由 Pydantic 条件校验返回 HTTP `422`；multipart 文件创建由业务校验返回 `40059`。

## 11. 当前推荐调用顺序

```text
GET /points/balance
  → 余额必须至少为 100
  → GET /models/options?model_type=video
  → 用户选择一次视频模型
  → POST /agent-productions/from-text 或 /from-file，mode=automatic
  → POST /agent-productions/{id}/start
  → GET /workflow 或 /workbench
  → 等待第一步分析完成
  → GET /script-package
  → POST /script-package/confirm
  → GET /core-assets
  → 可选上传或生成主资产、变体图片
  → POST /core-assets/confirm
  → 后端按集并发生成分镜
  → GET /storyboards，逐集展示 ready 结果
  → 对每个 ready 集 POST /episodes/{chapter_id}/video-generations
     请求直接携带创建时保存的 video_model_id 和 video_resolution
  → GET /episodes/{chapter_id}/videos
  → 必要时选择 primary-video
  → GET /review 和 /video-timeline
  → POST /episodes/{chapter_id}/approve
  → GET /delivery-readiness
  → POST /jianying-exports
```

## 12. 兼容和废弃接口

以下接口不是新版四步自动模式的推荐入口：

- `/projects/{project_id}/agent-productions`：从项目中心普通项目创建的旧入口。
- `/agent-productions/{production_id}/episode-plans...`：旧独立分集规划接口。
- `/agent-productions/{production_id}/pilot...`：旧试播集流程。
- `/agent-productions/{production_id}/batch/video-model`：旧全局批量流程的视频模型选择。
- `/agent-productions/{production_id}/batch/dispatch`：旧全局“分镜 → 图片 → 视频”批量调度。

新版自动模式使用独立创建入口、`script-package`、`core-assets`、`storyboards`、逐集 `video-generations` 和 `review` 接口。
