# Agent 分集与单分镜组视频生产接口

本文档定义 Agent 第三步的整集补位与单分镜组视频生产契约。

## 1. 范围约束

- 人物、场景、道具及其变体属于全局资产，在第二步统一生成、上传和管理；
- 视频生产必须位于指定分集内，不提供全剧视频生成入口，也不允许一次请求跨集；
- “生成本集视频”会为本集尚未完成的有效分镜组分别创建视频任务；
- “生成当前分镜组”只处理指定分镜，允许对已有成功视频显式重生成；
- 整集接口会跳过已有主视频、等待候选确认或正在生成的分镜；单组接口只会复用正在生成的任务，
  可继续为已有主视频的分镜追加候选；
- 每个分镜组可独立保存视频模型、分辨率和 4～15 秒预估时长，不写入或锁定全剧配置；
- 整集生成优先使用各分镜组已保存的模型和分辨率，请求中的配置只作为未配置分镜组的默认值；
- 单组生成使用本次请求中的模型和分辨率，便于临时调整并生成一个新候选版本；
- 视频直接使用第二步全局资产参考图。分镜选择了资产变体时，优先使用变体参考图；
- 第一次成功生成的视频自动成为主视频；后续成功生成的视频保留为候选，必须由用户确认主视频；
- 每个分镜组仍独立计费、独立记录任务状态、独立回写结果。

全局资产接口继续使用：

```text
POST /api/v1/agent-productions/{production_id}/core-assets/image-generations
PUT  /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/reference-image
POST /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/image-generation
PUT  /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/reference-image
```

## 2. 前端流程

```text
GET /storyboards
  → 某一集分镜分析 status=ready
  → GET /asset-options
  → 如需更换资产或变体：PATCH /storyboards/{storyboard_id}
  → PUT /video-config 保存该组模型、分辨率和预估时长
  → GET /episodes/{chapter_id}/videos
  → 整集模式：POST /episodes/{chapter_id}/video-generations
  → 单组模式：PATCH 保存提示词/资产绑定并重新 GET 最新版本
             → POST /episodes/{chapter_id}/storyboards/{storyboard_id}/video-generations
  → 仅在 should_poll=true 时继续 GET /episodes/{chapter_id}/videos
  → selection_required=true 时 GET /video-versions
  → PUT /primary-video 确认主视频
```

## 3. 分镜组资产与生成配置

### 3.1 查询可用资产和变体

```http
GET /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/asset-options
Authorization: Bearer <token>
```

响应只包含第二步已确认且当前有效的全局资产；变体只返回已就绪并适用于当前集的记录。
`bound=true` 表示基础资产已绑定到该分镜，`selected_variant_id` 和变体的 `selected=true`
表示当前选中的剧情变体。

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "production_id": "14660067-122a-4fb0-be0f-a7fd404b7b15",
    "chapter_id": "11111111-1111-1111-1111-111111111111",
    "storyboard_id": "22222222-2222-2222-2222-222222222222",
    "episode_number": 1,
    "core_asset_lock_version": 2,
    "storyboard_revision": 2,
    "items": [
      {
        "asset_type": "character",
        "asset_id": "44444444-4444-4444-4444-444444444444",
        "candidate_id": "55555555-5555-5555-5555-555555555555",
        "canonical_name": "沈砚",
        "reference_image": "https://cdn.example.com/shenyan.png",
        "bound": true,
        "binding_key": "character_1",
        "selected_variant_id": "66666666-6666-6666-6666-666666666666",
        "mention_text": "@沈砚·雨夜装",
        "mention_reference_image": "https://cdn.example.com/shenyan-rain.png",
        "mention_enabled": true,
        "variants": [
          {
            "variant_id": "66666666-6666-6666-6666-666666666666",
            "canonical_name": "沈砚·雨夜装",
            "variant_type": "costume",
            "description": "深色雨衣，衣角湿润",
            "trigger_reason": "第一集雨夜进入旧宅",
            "reference_image": "https://cdn.example.com/shenyan-rain.png",
            "selected": true
          }
        ]
      }
    ]
  }
}
```

更换基础资产、选择其他变体或删除绑定仍使用统一自动保存接口：

```http
PATCH /api/v1/agent-productions/{production_id}/storyboards/{storyboard_id}
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "expected_core_asset_lock_version": 2,
  "expected_revision": 2,
  "asset_bindings": [
    {
      "binding_key": "character_1",
      "asset_type": "character",
      "asset_id": "44444444-4444-4444-4444-444444444444",
      "variant_id": "77777777-7777-7777-7777-777777777777"
    }
  ]
}
```

`asset_bindings` 是目标分镜组的完整绑定列表，不是增量列表。删除某项即表示该分镜不再使用该资产。
后端会校验资产/变体归属和当前集适用范围，并按绑定名称连锁重编译分镜提示词；保存成功后使用响应中的
新 `revision`，旧媒体结果会被标记失效。

提示词编辑器输入 `@` 时只展示 `bound=true` 且 `mention_enabled=true` 的项目。界面显示
`mention_text` 和 `mention_reference_image`，保存时把 mention 节点序列化为
`{{asset:binding_key}}` 并通过分镜 PATCH 的 `storyboard_prompt` 提交。

### 3.2 保存单组视频配置

```http
PUT /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-config
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "expected_core_asset_lock_version": 2,
  "expected_storyboard_revision": 2,
  "expected_config_version": 0,
  "video_model_id": "77777777-7777-7777-7777-777777777777",
  "video_resolution": "1080p",
  "estimated_duration_seconds": 8
}
```

```json
{
  "code": 0,
  "message": "分镜组视频配置已更新",
  "data": {
    "storyboard_id": "22222222-2222-2222-2222-222222222222",
    "chapter_id": "11111111-1111-1111-1111-111111111111",
    "storyboard_revision": 3,
    "episode_revision": 4,
    "config_version": 1,
    "video_model_id": "77777777-7777-7777-7777-777777777777",
    "video_resolution": "1080p",
    "estimated_duration_seconds": 8
  }
}
```

`expected_config_version` 首次保存传 `0`，之后取 `storyboards[].video_config.version`。
只修改模型或分辨率不会改变分镜版本；修改预估时长会重编译提示词、增加分镜及分集版本，并使已有媒体结果失效。

## 4. 查询本集视频状态

```http
GET /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/videos
Authorization: Bearer <token>
```

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "production_id": "14660067-122a-4fb0-be0f-a7fd404b7b15",
    "chapter_id": "11111111-1111-1111-1111-111111111111",
    "episode_number": 1,
    "title": "雨夜旧宅",
    "episode_revision": 3,
    "core_asset_lock_version": 2,
    "status": "processing",
    "storyboard_count": 2,
    "video_status_counts": {
      "not_started": 0,
      "pending": 1,
      "running": 0,
      "success": 0,
      "selected": 1,
      "failed": 0,
      "invalidated": 0,
      "selection_required": 0
    },
    "active_task_count": 1,
    "failed_item_count": 0,
    "selection_required_count": 0,
    "can_generate": false,
    "should_poll": true,
    "next_poll_seconds": 10,
    "storyboards": [
      {
        "storyboard_id": "22222222-2222-2222-2222-222222222222",
        "shot_number": 1,
        "title": "沈砚进入旧宅",
        "revision": 2,
        "estimated_duration_seconds": 8,
        "video": {
          "status": "selected",
          "result_url": "https://cdn.example.com/episode-1-shot-1.mp4",
          "task_record_id": "33333333-3333-3333-3333-333333333333",
          "model_id": "44444444-4444-4444-4444-444444444444",
          "resolution": "1080p",
          "next_poll_seconds": null,
          "selected_history_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
          "latest_history_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
          "selection_revision": 1,
          "selection_required": false
        }
      }
    ]
  },
  "timestamp": "2026-08-03T12:00:00+08:00"
}
```

分集状态：

| 状态 | 含义 |
| --- | --- |
| `ready` | 本集存在未开始、失败或已失效的视频，可提交生成 |
| `processing` | 本集存在 `pending` / `running` 视频任务 |
| `selection_required` | 至少一个分镜组生成了新候选，等待用户确认主视频 |
| `failed` | 本集存在失败视频，系统不会自动重试 |
| `completed` | 本集全部有效分镜组都已确认主视频 |

只有 `should_poll=true` 时才继续查询，间隔使用 `next_poll_seconds`。
`can_generate` 表示整集补位接口当前是否还有可提交项，不限制单组重生成；本集 `completed` 时，
目标分镜没有活动任务且版本有效，仍可调用单组接口生成新版本。

## 5. 生成本集视频

```http
POST /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/video-generations
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "expected_core_asset_lock_version": 2,
  "expected_episode_revision": 3,
  "idempotency_key": "episode-1-videos-20260803-001",
  "video_model_id": "77777777-7777-7777-7777-777777777777",
  "video_resolution": "1080p"
}
```

请求不传 `storyboard_ids`。后端以目标集当前完整分镜组列表为准，避免前端漏传或把其他集分镜混入。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `expected_core_asset_lock_version` | 是 | 来自查询响应，防止使用旧资产引用 |
| `expected_episode_revision` | 是 | 来自查询响应，任何分镜新增、编辑、复制、删除、排序后都会变化 |
| `idempotency_key` | 是 | 8～128 字符；网络重试复用原键，新操作使用新键 |
| `video_model_id` | 是 | 未保存单组配置时的默认模型；来自 `/models/options?model_type=video` |
| `video_resolution` | 否 | 未保存单组配置时的默认分辨率；省略为 `720p` |

每个视频任务使用对应分镜组的 `estimated_duration_seconds`，不会使用统一 5 秒。当前固定
`generate_audio=false`，对白、音效和配乐在后续制作环节处理。

### 5.1 生成单个分镜组视频

```http
POST /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-generations
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "expected_core_asset_lock_version": 2,
  "expected_episode_revision": 3,
  "expected_storyboard_revision": 2,
  "expected_video_config_version": 1,
  "duration_seconds": 8,
  "idempotency_key": "episode-1-storyboard-1-video-v2",
  "video_model_id": "77777777-7777-7777-7777-777777777777",
  "video_resolution": "1080p",
  "prompt": "加强人物迟疑时的眼神变化，镜头缓慢推进"
}
```

除整集接口字段外，单组接口增加：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `expected_storyboard_revision` | 是 | 目标分镜组最新 `revision`，必须 `>= 1` |
| `expected_video_config_version` | 是 | 目标分镜组 `video_config.version`；没有保存过配置时传 `0` |
| `duration_seconds` | 是 | 本次实际提交给视频模型的时长，范围 4～15 秒 |
| `prompt` | 否 | 本次生成的补充提示词，去除首尾空白后 1～5000 字符 |

资产不作为临时参数重复传入。单组生成读取目标分镜当前保存的 `asset_bindings`，包括基础资产和
变体选择。需要调整使用资产时，先 PATCH 分镜组的 `asset_bindings`；需要完整修改固定分镜提示词
时，先 PATCH `shots`、`prompt_notes` 或 `storyboard_prompt`。保存后重新 GET 分镜包取得最新的
分集版本和分镜版本，再提交单组视频。

持久化的 `storyboard_prompt` 会用于该分镜组之后的所有视频生成；本接口请求中的 `prompt` 只是
当前一次生成的补充要求，不会覆盖分镜组保存的提示词。

提交前，后端按照持久化提示词中 `{{asset:*}}` 首次出现的顺序解析人物、场景、道具和当前选中变体的
参考图，并生成有序 `reference_manifest`。最终提示词会使用 `@图片1`、`@图片2` 等引用，编号
与实际提交给视频模型的图片顺序严格一致。选中变体时只使用变体图，不重复提交主资产图。

存在 token 时只提交被引用图片，同一资产重复出现只提交一次；没有 token 的历史分镜继续提交
全部绑定图片。提示词引用未绑定 key 时返回 `40962`，不会静默删除引用。

人物和道具资产图可能是白底肖像/三视图或完整图/细节拼版，最终提示词会明确要求模型只参考
资产外观，不得复刻白底、拼版、三视图、说明文字或水印。绑定资产或选中变体缺少参考图时，
本次任务不会提交；参考图超过所选模型上限时也不会静默丢图。

单组接口允许已有成功视频使用新幂等键重新生成；已有 `pending/running` 任务时返回并复用当前
任务，不会并行重复提交。响应结构与第 6 节相同，但 `items` 固定只有目标分镜一项。

## 6. 提交响应

```json
{
  "code": 0,
  "message": "本集视频任务已提交",
  "data": {
    "request_id": "88888888-8888-8888-8888-888888888888",
    "production_id": "14660067-122a-4fb0-be0f-a7fd404b7b15",
    "chapter_id": "11111111-1111-1111-1111-111111111111",
    "idempotency_key": "episode-1-videos-20260803-001",
    "status": "submitted",
    "idempotent_replay": false,
    "submitted_count": 1,
    "reused_count": 1,
    "failed_count": 0,
    "total_points_cost": 20,
    "items": [
      {
        "storyboard_id": "22222222-2222-2222-2222-222222222222",
        "submitted": true,
        "reused_active_task": false,
        "task_record_id": "99999999-9999-9999-9999-999999999999",
        "status": "pending",
        "points_cost": 20,
        "next_poll_seconds": 10,
        "error_code": null,
        "error_message": null
      },
      {
        "storyboard_id": "55555555-5555-5555-5555-555555555555",
        "submitted": false,
        "reused_active_task": false,
        "task_record_id": "66666666-6666-6666-6666-666666666666",
        "status": "success",
        "points_cost": 0,
        "next_poll_seconds": null,
        "error_code": null,
        "error_message": null
      }
    ]
  }
}
```

HTTP 200 表示分集请求已处理，不代表每个分镜组都成功入队。前端必须检查 `failed_count` 和
`items[].error_message`。`reused_count` 表示已有成功结果或活动任务、因此没有重复创建的分镜组数。

## 7. 视频候选与主视频确认

每次成功生成都会新增不可变的视频候选快照，记录当次模型、分辨率、预估时长、提示词、资产绑定、
参考图、分镜版本和资产锁版本。第一次成功结果自动成为主视频；已有主视频后的重生成不会覆盖主视频，
而是将分镜状态置为 `selection_required`。

### 7.1 查询候选版本

```http
GET /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-versions
Authorization: Bearer <token>
```

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "production_id": "14660067-122a-4fb0-be0f-a7fd404b7b15",
    "chapter_id": "11111111-1111-1111-1111-111111111111",
    "storyboard_id": "22222222-2222-2222-2222-222222222222",
    "selection_revision": 1,
    "selection_required": true,
    "selected_history_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "latest_history_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "total": 2,
    "items": [
      {
        "history_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "task_record_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "video_model_id": "77777777-7777-7777-7777-777777777777",
        "result_url": "https://cdn.example.com/candidate-2.mp4",
        "result_urls": ["https://cdn.example.com/candidate-2.mp4"],
        "last_frame_url": "https://cdn.example.com/candidate-2-last.png",
        "status": "success",
        "is_selected": false,
        "validity_status": "current",
        "resolution": "1080p",
        "requested_duration_seconds": 8,
        "prompt": "加强人物迟疑时的眼神变化",
        "asset_bindings": [],
        "reference_images": [],
        "reference_manifest": [
          {
            "index": 1,
            "reference_token": "@图片1",
            "media_type": "image",
            "reference_role": "character_identity",
            "binding_key": "character_1",
            "binding_keys": ["character_1"],
            "asset_type": "character",
            "asset_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "variant_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "label": "沈砚夜行变装",
            "url": "https://cdn.example.com/shenyan-night.png"
          }
        ],
        "provider_parameters": {
          "model_id": "77777777-7777-7777-7777-777777777777",
          "resolution": "1080p",
          "ratio": "16:9",
          "duration_seconds": 8,
          "generate_audio": false,
          "return_last_frame": true
        },
        "storyboard_revision": 3,
        "core_asset_lock_version": 2,
        "created_at": "2026-08-03T16:00:00+08:00"
      }
    ]
  }
}
```

### 7.2 确认主视频

```http
PUT /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/primary-video
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "expected_selection_revision": 1,
  "history_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
  "result_url": "https://cdn.example.com/candidate-2.mp4"
}
```

`result_url` 可省略，此时使用候选的默认地址；如果候选包含多个输出，只能选择其 `result_urls` 中的地址。
确认后该候选成为唯一主视频，`selection_revision` 增加，`selection_required=false`。如果本集所有分镜组
都已有主视频且没有待确认候选，本集状态变为 `completed`。选择新主视频会使已通过的该集审片结论失效。

## 8. 幂等与补位

- 唯一范围为 `production_id + video + idempotency_key`；
- 相同请求重放返回 `idempotent_replay=true`，不重复创建任务或扣积分；
- 整集接口同键改变分集、资产锁版本、分集版本、视频模型或分辨率，返回 `40971`；
- 单组接口同键改变目标分镜、分镜版本、补充提示词、模型或分辨率，同样返回 `40971`；
- 本集部分任务失败后，用户明确点击重新生成，使用最新版本和新幂等键重新提交本集；
- 整集接口跳过已有主视频、等待候选确认和仍在运行的分镜组，只补交失败、未开始或已失效项；
- 单组接口允许重生成成功项，但不会重复提交仍在运行的目标分镜。

## 9. 错误处理

| HTTP | `code` | 含义 |
| --- | --- | --- |
| 400 | `40003` | 用户积分不足 |
| 400 | `40036` | 指定结果地址不属于候选，或候选没有可用视频地址 |
| 400 | `40037` | 视频时长缺失，或多模态参考图超过所选模型上限 |
| 404 | `40404` | 所选视频模型不存在、未启用或类型不匹配 |
| 404 | `40410` | 单组接口指定的分镜组不存在或不属于该集 |
| 404 | `40430` | Agent 项目不存在或不属于当前用户 |
| 404 | `40431` | Agent 分集不存在 |
| 404 | `40445` | 指定视频候选不存在、不属于目标分镜或已不可用 |
| 409 | `40950` | 全局核心资产尚未确认 |
| 409 | `40955` | 核心资产锁版本冲突 |
| 409 | `40959` | 单组接口的分镜组版本冲突 |
| 409 | `40960` | 分集分镜版本冲突 |
| 409 | `40962` | 分镜提示词引用了未绑定的资产槽位 |
| 409 | `40968` | 该集分镜尚未分析完成 |
| 409 | `40971` | 幂等键已用于另一组分集视频参数 |
| 409 | `40989` | 本集存在不完整或未绑定人物、场景的分镜组 |
| 409 | `40990` | 第二步资产确认尚未完成，不能访问第三步接口 |
| 409 | `40992` | 分镜组视频配置版本冲突 |
| 409 | `40993` | 主视频选择版本冲突 |
| 409 | `40994` | 分镜没有资产绑定，或绑定资产/选中变体缺少参考图 |
| 422 | `42200` | 请求字段校验失败 |

模型能力或单项入队失败可能通过 HTTP 200 的 `items[].error_code` 和 `error_message` 返回。
系统不会自动重试。
