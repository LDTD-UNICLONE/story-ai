# Agent 第三步：分镜处理接口文档

本文档对应 Agent 流程第三步：根据第一步确认的分集剧情和第二步锁定的核心资产，
按剧情顺序生成可直接用于后续视频生成和剪辑的分镜组。

## 1. 阶段边界

第三步包含“分镜脚本”和“视频生成”两个功能模块。工作流以分镜脚本分析完成作为
第三步完成条件，视频生成可以在第四步已开放后继续进行：

- 所有待分析分集独立并发生成分镜组，每集内部仍按剧情顺序排序；
- 一个分镜组内允许有多个连续镜头；
- 使用系统固定的 `gpt-5.5` 文本模型，前端不选模型；
- 由后端计算每个分镜组的初始预估时长，用户可在 4～15 秒范围内调整；
- 绑定分镜画面中实际出现的人物、场景和道具核心资产；
- 生成后续视频任务直接使用的 `storyboard_prompt`；
- 允许用户自动保存镜头、补充提示词、调整资产及资产变体绑定；
- 支持分镜组新增、复制、删除和本集内排序；
- 到视频子阶段时为每个分镜组选择视频模型、分辨率和预估时长，可以整集补位生成，也可以指定单个分镜组调参生成。

分镜分析任务可按集并发提交；视频无论运行模式都必须由用户在目标集内显式提交，不存在分镜图片
生成阶段或全剧视频补位。每个分镜组可以保存独立的视频模型和分辨率，整集提交参数只作为未配置
分镜组的默认值。

第三步的工作流完成条件只是“所有分集的分镜分析已完成并落库”，不包含视频生成。
完成后第四步立即可访问；视频仍可以在第三步按集或按分镜组生成。

分镜分析期间不提前选择视频模型。任意一集分析完成后，即可使用
[分集视频生产接口](./agent-episode-video-api.md) 为该集全部分镜组生成视频。无需等待全剧分镜完成，
也不锁定全剧统一视频模型。
旧 `/batch` 链路仅保留历史兼容。

## 2. 前置条件

- 第一步“剧本处理”已确认，分集和文本资产已落库；
- 第二步核心资产已锁定；
- 项目已保存整体画风、画面比例和使用模式；
- 后台存在且已启用 `model_id=gpt-5.5`、`model_type=text` 的默认文本模型；
- 当前用户有足够积分支付本次实际提交的分集任务。

前端应先请求工作台：

```http
GET /api/v1/agent-productions/{production_id}/workbench
```

第二步确认资产后，后端会自动提交当前可入队分集。当 `next_action=monitor_storyboards` 时展示
任务进度；只有历史异常恢复、并发槽不足或仍存在 `not_started` 分集时，才根据
`can_generate=true` 展示手动补位入口。

## 3. 通用约定

所有接口均需要登录态：

```http
Authorization: Bearer <token>
```

成功响应统一为：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "timestamp": "2026-08-01T16:00:00+08:00"
}
```

参数校验失败返回 HTTP 422、`code=42200`。业务错误需同时判断 HTTP 状态码和
响应中的 `code`。

### 3.1 第三步接口清单

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/agent-productions/{production_id}/storyboards` | 查询全部分集分析状态和已完成分镜组 |
| POST | `/api/v1/agent-productions/{production_id}/storyboards/generations` | 手动补位 `not_started/invalidated` 分集 |
| POST | `/api/v1/agent-productions/{production_id}/jobs/retry` | 精确重试失败分集 |
| POST | `/api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards` | 新增本集草稿分镜组 |
| PUT | `/api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/order` | 调整本集分镜组顺序 |
| PATCH | `/api/v1/agent-productions/{production_id}/storyboards/{storyboard_id}` | 自动保存分镜组、镜头和资产绑定 |
| POST | `/api/v1/agent-productions/{production_id}/storyboards/{storyboard_id}/copy` | 复制分镜组 |
| DELETE | `/api/v1/agent-productions/{production_id}/storyboards/{storyboard_id}` | 软删除分镜组 |
| GET | `/api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/asset-options` | 查询可替换资产和当前集适用变体 |
| PUT | `/api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-config` | 保存模型、分辨率和预估时长配置 |
| GET | `/api/v1/agent-productions/{production_id}/episodes/{chapter_id}/videos` | 查询本集视频状态 |
| POST | `/api/v1/agent-productions/{production_id}/episodes/{chapter_id}/video-generations` | 按单组配置生成本集视频，请求参数作为默认值 |
| POST | `/api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-generations` | 调整参数并生成或重生成单个分镜组视频 |
| GET | `/api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-versions` | 查询视频候选和主视频状态 |
| PUT | `/api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/primary-video` | 确认分镜组主视频 |

除 `jobs/retry` 外，上述接口统一经过第三步访问校验；第二步未完成时返回 HTTP 409、
`code=40990`。失败重试接口自身也会校验核心资产锁和当前生产状态。

## 4. 前端调用顺序

```text
GET workbench
  → 第二步 POST core-assets/confirm 自动提交所有当前可入队分集
  → GET storyboards 取得分集任务和核心资产锁版本
  → 从 episode_analyses[].task_record_id 收集任务 ID
  → POST task-records/batch 批量轮询
  → 任意任务终态后重新 GET storyboards，立即展示该集结果
  → 若受全局并发槽限制仍有 not_started，调用 storyboards/generations 手动补位
  → 若某集 failed，调用 jobs/retry 精确重试该集
  → PATCH storyboards/{storyboard_id} 自动保存镜头、提示词或资产绑定
  → 可选：新增、复制、删除或调整本集分镜组顺序
  → 任意 episode_analyses[].status=ready 后 GET episodes/{chapter_id}/videos
  → 方式一：POST episodes/{chapter_id}/video-generations 补位生成本集尚未完成的视频
  → 方式二：先 PATCH 保存目标分镜提示词和资产绑定，再 POST 该分镜的 video-generations
  → 仅在 should_poll=true 时继续查询本集视频状态
  → 全剧所有分集的分镜分析完成后进入第四步
  → 第四步可先展示分镜顺序；未生成视频的分镜显示空视频状态
```

`POST storyboards/generations` 是未开始分集的并发补位、以及输入失效后的重新提交接口；首次提交由
资产确认接口自动完成，失败分集不使用此接口。
该接口的 HTTP 200 只表示分镜任务已同步提交，不代表模型已经生成完成。各集任务相互独立并由
Celery Worker 并发执行；单集失败不会停止其他集，但所有可运行任务结束后，存在失败集会使
批次进入 `exceptions`。

模型结果成功时，Worker 会在同一数据库事务内完成分镜组落库和资产绑定。GET 查询只读取
已经落库的绑定结果，不会在轮询时临时重写分镜绑定。

## 5. 查询分镜处理结果

```http
GET /api/v1/agent-productions/{production_id}/storyboards
```

请求无 Query 参数。

### 5.1 响应示例

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "production_id": "14660067-122a-4fb0-be0f-a7fd404b7b15",
    "core_asset_lock_version": 2,
    "phase": "storyboards",
    "status": "running",
    "current_stage": "batch_storyboards",
    "episode_count": 2,
    "completed_episode_count": 1,
    "remaining_episode_count": 1,
    "analysis_complete": false,
    "episode_analyses": [
      {
        "chapter_id": "11111111-1111-1111-1111-111111111111",
        "episode_number": 1,
        "title": "雨夜旧宅",
        "status": "ready",
        "task_record_id": "22222222-2222-2222-2222-222222222222",
        "error": null
      },
      {
        "chapter_id": "77777777-7777-7777-7777-777777777777",
        "episode_number": 2,
        "title": "怀表的秘密",
        "status": "running",
        "task_record_id": "88888888-8888-8888-8888-888888888888",
        "error": null
      }
    ],
    "current_analysis": {
      "chapter_id": "77777777-7777-7777-7777-777777777777",
      "episode_number": 2,
      "title": "怀表的秘密",
      "status": "running",
      "task_record_id": "88888888-8888-8888-8888-888888888888",
      "error": null
    },
    "failed_episodes": [],
    "storyboard_count": 3,
    "estimated_duration_seconds": 24,
    "active_task_count": 1,
    "failed_item_count": 0,
    "can_generate": true,
    "episodes": [
      {
        "chapter_id": "11111111-1111-1111-1111-111111111111",
        "episode_number": 1,
        "title": "雨夜旧宅",
        "status": "success",
        "revision": 1,
        "task_record_id": "22222222-2222-2222-2222-222222222222",
        "estimated_duration_seconds": 24,
        "storyboards": [
          {
            "id": "33333333-3333-3333-3333-333333333333",
            "chapter_id": "11111111-1111-1111-1111-111111111111",
            "shot_number": 1,
            "group_number": 1,
            "revision": 3,
            "status": "ready",
            "origin": "model",
            "title": "沈砚进入旧宅",
            "source_content": "沈砚推开旧宅木门，望向桌上的旧怀表。",
            "event_goal": "建立旧宅空间并引出关键道具",
            "scene_name": "旧宅客厅",
            "scene_state": "雨夜，室内昏暗",
            "characters": ["沈砚"],
            "props": ["旧怀表"],
            "shot_size": "全景",
            "camera_angle": "平视",
            "camera_movement": "缓慢推进",
            "screen_execution": "沈砚由门口走向木桌",
            "action": "进入旧宅并发现怀表",
            "character_action": "推门、停步、望向木桌",
            "character_expression": "警觉",
            "dialogue": null,
            "sound_effect": "雨声、木门轴响声",
            "atmosphere": "压抑、神秘",
            "shots": [
              {
                "shot_number": 1,
                "shot_size": "全景",
                "camera_shot": "拍摄旧宅客厅与门口的沈砚",
                "camera_angle": "平视",
                "camera_movement": "缓慢推进",
                "visual_content": "沈砚推开木门，走进昏暗的旧宅客厅",
                "scene_name": "旧宅客厅",
                "characters": ["沈砚"],
                "props": [],
                "speaker": "",
                "dialogue": "",
                "character_binding_keys": ["character_1"],
                "scene_binding_key": "scene_1",
                "prop_binding_keys": []
              },
              {
                "shot_number": 2,
                "shot_size": "特写",
                "camera_shot": "拍摄木桌上的旧怀表",
                "camera_angle": "俯拍",
                "camera_movement": "固定",
                "visual_content": "旧怀表静置在积灰木桌上，表盖反射微光",
                "scene_name": "旧宅客厅",
                "characters": [],
                "props": ["旧怀表"],
                "speaker": "",
                "dialogue": "",
                "character_binding_keys": [],
                "scene_binding_key": "scene_1",
                "prop_binding_keys": ["prop_1"]
              }
            ],
            "storyboard_prompt": "画面风格：写实悬疑漫剧\n视频中不得出现任何字幕、文字叠加，保持纯画面。不要BGM，不要配乐。\n镜头1：景别：全景；拍摄镜头：拍摄旧宅客厅与门口的沈砚；拍摄角度：平视；镜头运镜：缓慢推进；画面内容：沈砚推开木门，走进昏暗的旧宅客厅\n镜头2：景别：特写；拍摄镜头：拍摄木桌上的旧怀表；拍摄角度：俯拍；镜头运镜：固定；画面内容：旧怀表静置在积灰木桌上，表盖反射微光\n分镜组总时长：4秒",
            "prompt_template": "画面风格：写实悬疑漫剧\n视频中不得出现任何字幕、文字叠加，保持纯画面。不要BGM，不要配乐。\n镜头1：{{asset:character_1}}走进{{asset:scene_1}}\n镜头2：{{asset:prop_1}}静置在桌面上\n分镜组总时长：4秒",
            "effective_prompt": "画面风格：写实悬疑漫剧\n视频中不得出现任何字幕、文字叠加，保持纯画面。不要BGM，不要配乐。\n镜头1：……\n分镜组总时长：4秒",
            "prompt_notes": "",
            "estimated_duration_seconds": 4,
            "duration_source": "model",
            "video_config": {},
            "asset_ids": {
              "character": ["44444444-4444-4444-4444-444444444444"],
              "scene": ["55555555-5555-5555-5555-555555555555"],
              "prop": ["66666666-6666-6666-6666-666666666666"]
            },
            "asset_bindings": [
              {
                "binding_key": "character_1",
                "asset_type": "character",
                "asset_id": "44444444-4444-4444-4444-444444444444",
                "variant_id": "77777777-7777-7777-7777-777777777777"
              }
            ],
            "production_focus": "保持沈砚造型和怀表位置连续",
            "ending_frame": "怀表表盖反射微光的特写",
            "validation_errors": [],
            "updated_at": "2026-08-02T16:00:00+08:00"
          }
        ]
      }
    ]
  },
  "timestamp": "2026-08-01T16:00:00+08:00"
}
```

### 5.2 顶层字段

| 字段 | 说明 |
| --- | --- |
| `core_asset_lock_version` | 当前核心资产锁版本；提交生成时必须原样传回 |
| `phase` | 新版流程阶段：`not_started`、`storyboards`、`episode_videos`、`exceptions`；`images/videos/completed` 仅可能出现在历史批量链路 |
| `status` | 整剧状态，如 `running`、`paused`、`partially_failed`、`completed` |
| `current_stage` | 当前可为 `batch_production`、`batch_storyboards`、`episode_videos` 或 `batch_exceptions` |
| `episode_count` | 本步要处理的分集数 |
| `completed_episode_count` | 已独立分析完成且已有有效分镜组的集数 |
| `remaining_episode_count` | 尚未完成的集数，包含运行中、待提交、失败或失效的集 |
| `analysis_complete` | 所有分集是否均已分析完成 |
| `episode_analyses` | 全部分集的分析状态、任务 ID 和失败原因，始终按集号顺序返回 |
| `current_analysis` | 兼容字段，指向按集号排序的第一个未完成集；并发轮询应使用 `episode_analyses` |
| `failed_episodes` | 分镜分析失败的分集摘要；失败不会阻止其他集继续处理 |
| `storyboard_count` | 当前响应中已完成分集的分镜组总数，不是组内镜头数 |
| `estimated_duration_seconds` | 当前响应中已完成分镜组的预估时长总和 |
| `active_task_count` | 当前分镜批次记录中的活跃任务数；逐集视频活动数读取分集视频接口 |
| `failed_item_count` | 当前分镜批次失败的处理项数 |
| `can_generate` | 当前阶段和整剧状态是否允许调用分镜补位接口；不保证本次一定存在可提交分集 |

### 5.3 分集状态

`episodes` 只返回已经完成且存在有效分镜组的集。各集独立完成，因此它可以暂时出现集号不连续，
但数组本身始终按集号排序。全部分集的处理状态读取 `episode_analyses[].status`：

| 状态 | 前端处理 |
| --- | --- |
| `not_started` | 该集尚未提交；有可用并发槽时可再次调用生成接口补位 |
| `pending` | 该集任务已创建，等待 Worker |
| `running` | 该集正在独立分析，不影响其他分集 |
| `ready` | 该集分析完成，可从 `episodes` 进入、查看和编辑 |
| `failed` | 该集失败；从 `error` 或 `failed_episodes` 展示错误并显式重试 |
| `stale` | 剧本或资产输入已变化，旧分镜结果不可见，需要重新分析 |
| `invalid` | 状态显示成功但没有有效分镜组，按异常处理 |

### 5.4 分镜组和组内镜头

- `episodes[].storyboards[]` 的每一项是一个“分镜组”；
- `episodes[].revision` 是本集分镜列表版本，用于新增和排序并发校验；
- 分镜组的 `group_number` 是它在本集中的顺序；`shot_number` 为兼容旧前端保留，值相同；
- `revision` 是单个分镜组的自动保存版本；
- `origin` 为 `model`、`user` 或 `copy`；
- `status` 为 `draft`、`ready` 或 `invalid`，只有 `ready` 可进入媒体生产；
- `storyboards[].shots[]` 是组内连续镜头；
- 组内 `shots[].shot_number` 从 1 开始连续排序；
- `effective_prompt` 是后端根据镜头、资产槽位和补充要求编译的最终提示词；当前与
  `storyboard_prompt` 值相同，前端生成视频时以 `effective_prompt` 为准；
- `prompt_template` 是编辑器保存的稳定模板，资产引用使用 `{{asset:binding_key}}`；
- `validation_errors` 返回草稿字段不完整或时长不合规的原因；
- `duration_source` 为 `model` 或 `user`；`video_config` 返回该分镜已保存的视频生成配置；
- 分镜组和组内镜头都已按剧情发生顺序返回，前端不应重新排序。

## 6. 手动补位分镜生成

```http
POST /api/v1/agent-productions/{production_id}/storyboards/generations
Content-Type: application/json
```

```json
{
  "expected_core_asset_lock_version": 2,
  "idempotency_key": "storyboards-14660067-v2-001"
}
```

### 6.1 请求字段

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `expected_core_asset_lock_version` | integer | 是 | `>= 1` | 必须使用最新 GET 响应中的锁版本 |
| `idempotency_key` | string | 是 | 去除首尾空白后 8～128 字符 | 同一次用户操作重试时复用；新一次补位时换新值 |

成功时返回与 GET 相同的完整结构，但 `message` 为：

```json
{
  "code": 0,
  "message": "分镜脚本任务已提交",
  "data": {
    "phase": "storyboards",
    "current_stage": "batch_storyboards",
    "active_task_count": 2,
    "episodes": []
  },
  "timestamp": "2026-08-01T16:00:00+08:00"
}
```

> 上例的 `data` 仅为字段摘要，真实响应会返回第 5 节的完整结构。

### 6.2 幂等与补位

- 相同 `idempotency_key` 重复提交不会重复创建分镜任务或重复扣费；
- 后端每次最多选取 50 个待处理分集，并同时受用户全局并发任务槽限制；
- 当并发槽不足时，后端可能只提交部分分集，接口仍返回 200；
- 前端需要根据 `episode_analyses[].status` 判断是否仍有 `not_started`；
- 存在 `not_started` 且 `can_generate=true` 时，用新的 `idempotency_key` 再次调用本接口补位；
- 任务失败不会自动重试，用户显式重试需使用批量生产的精确重试接口。

### 6.3 失败分集精确重试

普通 `storyboards/generations` 只处理 `not_started` 或 `invalidated` 等可补位状态，不会重新提交
`failed` 分集。失败分集必须调用：

```http
POST /api/v1/agent-productions/{production_id}/jobs/retry
Content-Type: application/json
```

```json
{
  "expected_core_asset_lock_version": 2,
  "stage": "storyboard",
  "scope_ids": ["失败分集的 chapter_id"],
  "idempotency_key": "retry-storyboard-episode-2-v1"
}
```

`stage=storyboard` 时，`scope_ids` 必须是当前 Agent 项目的章节 ID，一次 1～50 个且会自动去重。
仅 `failed`、`invalidated` 或人工跳过的分集允许重试；`pending/running/success` 均返回 `40961`。
新版流程不要求 `confirm_over_retry_limit`。请求会先校验当前任务槽和积分，队列容量不足返回
`42920`。成功响应包含 `requested_count`、`affected_count`、`task_record_ids`、
`affected_scope_ids`、`points_cost` 和 `idempotent`。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `expected_core_asset_lock_version` | integer | 是 | 当前核心资产锁版本，必须 `>= 1` |
| `stage` | string | 是 | 分镜失败重试固定传 `storyboard` |
| `scope_ids` | UUID[] | 是 | 失败分集的 `chapter_id`，1～50 个，服务端保持顺序去重 |
| `idempotency_key` | string | 是 | 去除首尾空白后 8～128 字符 |
| `confirm_over_retry_limit` | boolean | 否 | 默认 `false`；新版四步流程不需要传 |

成功响应：

```json
{
  "code": 0,
  "message": "指定任务已重新提交",
  "data": {
    "action": "retry",
    "stage": "storyboard",
    "requested_count": 1,
    "affected_count": 1,
    "task_record_ids": ["99999999-9999-9999-9999-999999999999"],
    "affected_scope_ids": ["77777777-7777-7777-7777-777777777777"],
    "points_cost": 10,
    "idempotent": false
  },
  "timestamp": "2026-08-03T12:00:00+08:00"
}
```

## 7. 任务轮询

从 `episode_analyses[].task_record_id` 收集非空任务 ID。`episodes` 只包含已经完成的分集，不能
作为运行中任务 ID 的来源。优先使用批量轮询：

```http
POST /api/v1/task-records/batch
Content-Type: application/json
```

```json
{
  "ids": [
    "22222222-2222-2222-2222-222222222222",
    "88888888-8888-8888-8888-888888888888"
  ]
}
```

`ids` 每次 1～50 个，且只会返回当前用户自己的任务。

关键响应字段：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [
      {
        "id": "88888888-8888-8888-8888-888888888888",
        "status": "running",
        "stop_polling": false,
        "next_poll_seconds": 3
      }
    ],
    "total": 1,
    "stop_polling": false,
    "next_poll_seconds": 3
  },
  "timestamp": "2026-08-01T16:00:00+08:00"
}
```

- 单任务也可使用 `GET /api/v1/task-records/{task_record_id}`；
- 前端优先按响应中的 `next_poll_seconds` 轮询，也可读取
  `X-Next-Poll-Seconds` 响应头；
- `stop_polling=true` 代表本次查询的任务均已进入 `success` 或 `failed`；
- 轮询终止后必须重新 GET `storyboards`，不要直接解析任务记录的 `result`
  作为页面数据。

## 8. 分镜组生成规则

### 8.1 分镜组而不是单镜头

分镜组是后续一次视频生成和用户剪辑的最小单元。每组可以包含多个连续镜头，并应有明确
的进入画面、内部推进和结束画面。以下情况必须创建新分镜组：

- 场景变化、时空跳转或叙事目标改变；
- 一个连续剧情无法在 15 秒内完成；
- 句号、问答结束、动作完成、人物反应或情绪变化形成自然剪辑点。

### 8.2 固定提示词

后端不使用普通项目的分镜提示词。每个分镜组的 `storyboard_prompt` 固定按以下格式组装：

```text
画面风格：{项目整体画风}
视频中不得出现任何字幕、文字叠加，保持纯画面。不要BGM，不要配乐。
镜头1：景别：{shot_size}；拍摄镜头：{camera_shot}；拍摄角度：{camera_angle}；镜头运镜：{camera_movement}；画面内容：{visual_content}；人物说台词：{speaker}：{dialogue}
镜头2：……
分镜组总时长：{estimated_duration_seconds}秒
```

无台词镜头不会输出“人物说台词”部分。必须遵守：

- 画面中不得设计字幕、标题、文字叠加、气泡文字或 UI 文字；
- 不设计 BGM 或配乐；
- 人物原声台词、必要环境声和动作声允许保留。

### 8.3 时长计算和主动拆组

后端使用以下公式确定性计算每个分镜组的时长：

```text
台词时长 = ceil(中文汉字数 / 4 + 英文单词数 / 4)
画面时长 = max(4, 组内镜头数 × 2)
分镜组时长 = max(台词时长, 画面时长)
```

每组最终必须在 4～15 秒内。模型在输出前就需要预估台词和镜头承载时长，并在自然剪辑点
主动拆成多个分镜组。后端不会将超过 15 秒的结果强制截断；如果模型仍返回超限分镜组，
当前分集任务失败并记录 `code=50231`。

后端还会校验 `group_number` 必须从 1 连续递增、每组 `source_content` 必须按顺序来自当前
分集，并且所有组覆盖至少 80% 的分集有效文本。跨集原文、乱序或主要剧情覆盖不足同样按
无效模型结果处理，不会落库为可用分镜。

### 8.4 长对话镜头调度

长对话不能全程停留在单一说话人物正面。在不改变剧情和台词的前提下，模型应按现场条件穿插：

- 说话人物的动作、手势、走位和表情变化；
- 当前剧情中真实存在的听者反应；
- 环境建立镜头、空间关系、关键道具或空间细节。

切换到听者、环境或道具画面时，说话人的台词可以作为画外音继续，但对应镜头的
`speaker` 仍填原说话人，`dialogue` 仍按原文顺序分配。不得新增旁观人物、无关空镜或原文不存在的剧情。

## 9. 资产绑定与分镜组编辑

`asset_ids` 始终包含三个键：

```json
{
  "character": ["人物资产 UUID"],
  "scene": ["场景资产 UUID"],
  "prop": ["道具资产 UUID"]
}
```

绑定规则：

- 根据分镜组汇总的 `characters`、`scene_name`、`props` 绑定已锁定的基础资产；
- 人物支持使用标准名和别名匹配；
- 第二步锁定即代表剧本资产已确定，第三步不再要求人工确认资产；
- 当集可用的人物、场景或道具变体名会自动映射到已锁定的基础资产，`asset_ids` 返回对应基础资产 UUID；
- 发送给模型并允许自动绑定的基础资产按 `episode_numbers` 限定为当前集；未限定集数的资产视为全剧可用；
- 未出现在画面中的资产不应被绑定；
- 无法绑定的人物或场景会成为质量错误，无法绑定的道具会成为质量警告。

`asset_ids` 是生成服务使用的基础资产 UUID；`asset_bindings` 额外返回稳定的
`binding_key` 和用户选择的资产变体 UUID。`binding_key` 不随资产或变体切换而改变，镜头通过
该槽位与资产关联。没有选择变体时对应项不返回 `variant_id`。更换槽位中的资产后，后端会在
同一事务内重新编译 `effective_prompt`，前端不得自行对提示词做名称字符串替换。

输入 `@` 时复用 `asset-options` 接口，并只展示 `bound=true` 的资产。绑定项新增：

```json
{
  "binding_key": "character_1",
  "mention_text": "@沈砚·雨夜装",
  "mention_reference_image": "https://cdn.example.com/shenyan-rain.png",
  "mention_enabled": true
}
```

编辑器显示 `mention_text`，保存时将 mention 节点序列化为 `{{asset:character_1}}`。
`mention_enabled=false` 表示当前主资产或选中变体没有参考图，不能作为图片引用选择。
`{{asset:binding_key}}` 才是稳定关联；普通 `@名称` 不应作为接口关联值提交。为兼容旧提示词，
后端可能把与绑定标签一致的普通资产名称规范化为 token，但前端不能依赖这种名称匹配。

未绑定资产质量问题直接读取各分镜的 `validation_errors`。新版前端不查询 `/batch` 获取媒体阶段。

### 9.1 自动保存分镜组

```http
PATCH /api/v1/agent-productions/{production_id}/storyboards/{storyboard_id}
Content-Type: application/json
```

推荐使用结构化镜头和补充要求自动保存：

```json
{
  "expected_core_asset_lock_version": 2,
  "expected_revision": 3,
  "title": "沈砚进入旧宅",
  "source_content": "沈砚推开旧宅木门。",
  "prompt_notes": "加强人物听到异响后的紧张反应",
  "shots": [
    {
      "shot_number": 1,
      "shot_size": "近景",
      "camera_shot": "拍摄沈砚侧脸",
      "camera_angle": "平视",
      "camera_movement": "缓慢推进",
      "visual_content": "沈砚突然停步并转头看向走廊",
      "scene_name": "旧宅客厅",
      "characters": ["沈砚"],
      "props": [],
      "speaker": "",
      "dialogue": "",
      "character_binding_keys": ["character_1"],
      "scene_binding_key": "scene_1",
      "prop_binding_keys": []
    }
  ],
  "asset_bindings": [
    {
      "binding_key": "character_1",
      "asset_type": "character",
      "asset_id": "44444444-4444-4444-4444-444444444444",
      "variant_id": "77777777-7777-7777-7777-777777777777"
    },
    {
      "binding_key": "scene_1",
      "asset_type": "scene",
      "asset_id": "55555555-5555-5555-5555-555555555555"
    }
  ]
}
```

用户也可以直接修改并持久化整个分镜组提示词：

```json
{
  "expected_core_asset_lock_version": 2,
  "expected_revision": 3,
  "storyboard_prompt": "画面风格：写实悬疑漫剧\n视频中不得出现任何字幕、文字叠加、纯画面，不要BGM，不要配乐。\n镜头1：景别：近景；拍摄镜头：拍摄{{asset:character_1}}侧脸；拍摄角度：平视；镜头运镜：缓慢推进；画面内容：{{asset:character_1}}突然停步并看向{{asset:scene_1}}走廊\n分镜组总时长：8秒"
}
```

该修改会写入 `storyboard_prompt` 和 `effective_prompt`，增加分镜组及分集版本，使已有视频失效，
并要求后续视频生成使用新提示词。画风、禁止字幕、纯画面、不要 BGM 和不要配乐属于系统保护规则，
用户可以修改镜头描述和执行要求，但不能删除这些规则。

- 除版本字段外，`title`、`source_content`、`shots`、`prompt_notes`、`storyboard_prompt`、
  `estimated_duration_seconds`、`asset_bindings` 至少提交一个；
- 每次自动保存必须携带响应中的最新 `expected_revision`，缺少该字段返回 422；
- `shots` 和完整 `storyboard_prompt` 不能在同一次请求中同时提交；
- 推荐编辑 `shots` 和 `prompt_notes`，由后端重新生成固定规则及 `effective_prompt`；
- 允许直接提交完整 `storyboard_prompt`，但必须保留固定画风、禁止字幕、纯画面、不要 BGM 和
  不要配乐规则；
- 提交完整提示词时，token 必须引用当前 `asset_bindings` 中的 `binding_key`；无效引用返回 `40962`；
- 服务端会按数组顺序重新编号 `shots[].shot_number`；
- `asset_bindings` 是完整替换，不是增量追加；传空数组表示该分镜不再使用任何资产；
- 如果镜头仍引用被删除的槽位，分镜组会变为 `invalid`；前端应同时修改对应镜头内容和绑定键；
- 更换现有槽位的资产或变体时必须原样提交该项的 `binding_key`；新增槽位可以不传，服务端自动分配；
- `asset_id` 必须属于当前核心资产锁；`variant_id` 必须属于该基础资产且适用于本集；
- 选中变体后，服务端会更新槽位标签和变体上下文，并立即重新编译提示词；
- 相同核心资产版本下，人工绑定不会被后台自动绑定刷新覆盖；核心资产重新锁定后需要重新选择；
- 有视频任务正在运行时返回 `40956`；历史项目如仍有分镜图片任务，也必须等待该任务结束；
- 修改完整提示词、`prompt_notes`、镜头、原文或资产绑定会使已有视频失效，本集重新变为可生成；
- 保存成功后必须用响应的新 `revision` 覆盖本地版本。

自动保存建议：输入停止 800 毫秒后提交；同一分镜组只保留一个在途请求；失焦或切换分镜组时
立即提交。HTTP 409、`code=40959` 表示版本过期，前端应停止覆盖并重新获取最新分镜包。

### 9.2 新增分镜组

```http
POST /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards
Content-Type: application/json
```

```json
{
  "expected_core_asset_lock_version": 2,
  "expected_episode_revision": 4,
  "insert_after_storyboard_id": "33333333-3333-3333-3333-333333333333",
  "title": "补充反应镜头",
  "source_content": ""
}
```

- `insert_after_storyboard_id` 省略时追加到本集末尾；
- `expected_episode_revision` 新前端必须提交，暂时允许省略仅为兼容；
- 新分镜组包含一个空镜头，`origin=user`、`status=draft`、`revision=1`；
- 草稿允许保存，但会阻止该集进入视频生产；补齐镜头字段且时长为 4～15 秒后自动变为
  `ready`。

### 9.3 复制分镜组

```http
POST /api/v1/agent-productions/{production_id}/storyboards/{storyboard_id}/copy
Content-Type: application/json
```

```json
{
  "expected_core_asset_lock_version": 2,
  "expected_revision": 3
}
```

副本插入原分镜组之后，复制镜头、绑定、提示词和时长，但不复制视频、媒体任务和审核
结果。响应中的副本为 `origin=copy`、`revision=1`。

### 9.4 删除分镜组

```http
DELETE /api/v1/agent-productions/{production_id}/storyboards/{storyboard_id}?expected_core_asset_lock_version=2&expected_revision=3
```

服务端软删除分镜组并重新编号本集剩余分镜。每集必须至少保留一个分镜组；删除最后一组返回
`40969`，前端如需替换应先新增再删除。正在生成视频时返回 `40956`。成功响应：

```json
{
  "code": 0,
  "message": "分镜组已删除",
  "data": {
    "id": "33333333-3333-3333-3333-333333333333",
    "chapter_id": "11111111-1111-1111-1111-111111111111",
    "deleted": true,
    "episode_revision": 6
  }
}
```

### 9.5 调整本集分镜组顺序

```http
PUT /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/order
Content-Type: application/json
```

```json
{
  "expected_episode_revision": 6,
  "storyboard_ids": [
    "99999999-9999-9999-9999-999999999999",
    "33333333-3333-3333-3333-333333333333"
  ]
}
```

`storyboard_ids` 必须无重复并完整包含本集全部有效分镜组，不能只提交发生移动的部分。响应返回
新的 `episode_revision` 和排序后的完整 `storyboards`。

### 9.6 整集视频生成

使用 `/episodes/{chapter_id}/video-generations` 为目标集当前全部分镜组生成视频。请求必须携带
默认 `video_model_id` 和 `video_resolution`，但不提交分镜 ID 列表；已经保存单组配置的分镜优先
使用自己的模型和分辨率。人物、场景、道具及变体参考图来自第二步全局资产。完整字段、幂等、
候选版本、主视频确认及轮询约定见
[Agent 分集视频生产接口](./agent-episode-video-api.md)。

整集接口是补位接口：已有主视频、等待候选确认或正在运行的分镜组不会重复提交，只处理未开始、
失败或已失效项。

### 9.7 单个分镜组视频生成

```http
POST /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-generations
Content-Type: application/json
```

```json
{
  "expected_core_asset_lock_version": 2,
  "expected_episode_revision": 6,
  "expected_storyboard_revision": 3,
  "expected_video_config_version": 1,
  "duration_seconds": 8,
  "idempotency_key": "episode-1-storyboard-3-video-v1",
  "video_model_id": "77777777-7777-7777-7777-777777777777",
  "video_resolution": "1080p",
  "prompt": "加强人物停顿后的眼神变化，镜头推进更缓慢"
}
```

单组接口只校验和提交 URL 中指定的分镜组，并具有以下行为：

- `expected_storyboard_revision` 必填，必须使用目标分镜最新 `revision`；
- `expected_video_config_version` 必填，使用目标分镜最新 `video_config.version`，未保存配置时传 `0`；
- `duration_seconds` 必填，范围为 4～15 秒，并作为本次请求的实际视频时长；
- `prompt` 可选，1～5000 字符，是本次视频的补充要求，不覆盖固定画风、禁字幕和资产一致性规则；
- 即使目标分镜已经存在成功视频，使用新的幂等键仍会创建新的生成任务；
- 目标分镜已有 `pending/running` 任务时不会并行重复创建，而是返回当前活动任务；
- 请求不直接接收临时资产 ID。人物、场景、道具和变体使用目标分镜当前的 `asset_bindings`；
- 后端将当前绑定资产编译为有序 `reference_manifest`，最终提示词中的 `@图片N` 与提交给
  视频模型的第 N 张图片严格对应；选择变体后只提交变体图；
- 绑定资产或选中变体缺图时不提交任务；超过视频模型图片上限时明确报错，不会静默丢弃；
- 如需更换资产或变体，先调用第 9.1 节 PATCH 保存绑定，再重新 GET 分镜包取得新的
  `expected_episode_revision` 和 `expected_storyboard_revision`，最后调用本接口；
- 第一次成功生成的视频自动成为主视频；后续成功结果保留为候选，不直接覆盖当前主视频；
- 有待确认候选时本集状态为 `selection_required`，通过 `video-versions` 查询后使用
  `primary-video` 确认主视频；
- 成功响应复用整集视频生成响应结构，但 `items` 只包含目标分镜组。

## 10. 错误处理

### 10.1 接口同步错误

| HTTP | `code` | 消息 | 前端处理 |
| --- | --- | --- | --- |
| 400 | `40003` | 积分不足，请充值 | 提示充值，不要循环重试 |
| 400 | `40036` | 视频候选地址无效或候选没有可用地址 | 重新读取候选列表后选择其中的结果地址 |
| 400 | `40037` | 视频时长缺失或多模态参考图超过模型上限 | 补齐 4～15 秒时长，或减少绑定资产/拆分分镜组 |
| 400 | `40052` | 整剧任务已达到积分预算上限 | 兼容旧数据的预算限制；新 Agent 项目默认不设预算 |
| 404 | `40430` | 整剧任务不存在 | 检查 `production_id` 和当前登录用户 |
| 404 | `40404` | 文本或视频模型不存在、未启用或类型不匹配 | 检查 `gpt-5.5` 配置或重新选择视频模型 |
| 404 | `40410` | 分镜组或插入位置不存在 | 刷新本集分镜组 |
| 404 | `40411` | Agent 分集不存在 | 检查 `chapter_id` 是否属于当前 Agent 项目 |
| 404 | `40431` | 分集视频接口中的 Agent 分集不存在 | 刷新分集列表并检查 `chapter_id` |
| 404 | `40445` | 视频候选不存在、不属于目标分镜或已不可用 | 重新读取该分镜的视频候选 |
| 409 | `40950` | 核心资产尚未锁定或变更尚未重新锁定 | 返回第二步处理 |
| 409 | `40951` | 当前整剧阶段或状态不允许生产 | 重新查询工作台 |
| 409 | `40952` | 整剧任务已暂停 | 恢复后再提交 |
| 409 | `40955` | 核心资产锁版本冲突 | 重新 GET 分镜包并使用新版本 |
| 409 | `40956` | 当前状态不允许修改，或媒体任务正在生成 | 等待任务完成后重试 |
| 409 | `40957` | 绑定的基础资产不在当前核心资产锁 | 刷新分镜和资产列表 |
| 409 | `40958` | 变体不属于该基础资产或不适用于本集 | 清除变体或选择本集可用变体 |
| 409 | `40959` | 分镜组版本冲突 | 重新 GET 后提示用户合并或重新编辑 |
| 409 | `40960` | 分集分镜版本冲突或排序列表不完整 | 刷新本集后重新排序 |
| 409 | `40961` | 资产绑定槽位无效或重复，或精确重试目标不允许 | 按接口场景刷新绑定，或只重试允许状态的分集 |
| 409 | `40962` | 分镜提示词引用了未绑定资产 | 刷新 `asset-options` 并移除 `data.invalid_binding_keys` 中的 mention |
| 409 | `40968` | 该集分镜尚未分析完成 | 停止查看或编辑该集，按 `episode_analyses` 继续轮询 |
| 409 | `40969` | 每集至少需要保留一个分镜组 | 先新增替代分镜组，再删除原分镜组 |
| 409 | `40971` | 分集视频幂等键已用于不同参数 | 新操作生成新幂等键；网络重试继续复用原键 |
| 409 | `40989` | 本集存在尚未准备完成的分镜组 | 根据 `quality_issues` 修正分镜和资产绑定 |
| 409 | `40990` | 前置步骤尚未完成 | 完成第二步资产确认后再访问第三步 |
| 409 | `40992` | 分镜组视频配置版本冲突 | 刷新分镜包后使用最新 `video_config.version` |
| 409 | `40993` | 主视频选择版本冲突 | 重新读取 `video-versions` 后再次确认 |
| 409 | `40994` | 分镜无资产绑定，或绑定资产/选中变体缺少参考图 | 返回第二步补齐资产或变体图后重新提交 |
| 422 | `42200` | 请求参数校验失败 | 按 `data[]` 修正字段 |
| 429 | `42920` | 当前用户任务队列容量不足 | 等待活动任务结束后再补位或重试 |

锁版本冲突响应会同时返回期望值和最新值：

```json
{
  "code": 40955,
  "message": "核心资产锁版本冲突，当前版本为 3",
  "data": {
    "expected_core_asset_lock_version": 2,
    "current_core_asset_lock_version": 3
  },
  "timestamp": "2026-08-01T16:00:00+08:00"
}
```

### 10.2 异步分集失败

以下问题会发生在 Worker 异步执行阶段，提交接口可能已经返回 200：

- 模型没有返回有效分镜组；
- 分镜组不包含任何镜头；
- 组内镜头缺少景别、拍摄镜头、拍摄角度、镜头运镜或画面内容；
- 后端计算的分镜组时长不在 4～15 秒范围内；
- 分镜组序号不连续、原文跨集或顺序错误、主要剧情覆盖不足 80%。

此类模型结果校验失败使用 `code=50231`，分集的 `status` 最终为 `failed`。系统不会自动重试。

## 11. 前端实现要点

1. 页面以 `production_id` 为唯一 Agent 项目 ID，不传内部 `project_id`。
2. 提交前使用最新 GET 响应中的 `core_asset_lock_version`。
3. 分镜组用外层 `group_number` 排序，组内镜头用内层 `shots[].shot_number` 排序。
4. 页面展示后端返回的 `estimated_duration_seconds`，不在前端重新估算。
5. 自动保存使用单个分镜组的 `revision`；新增和排序使用 `episodes[].revision`。
6. 生成服务使用 `asset_ids.character/scene/prop`；编辑器使用 `asset_bindings.binding_key`
   展示、替换基础资产或选择变体。
7. 前端展示 `effective_prompt`；编辑时读取 `prompt_template`，`@` 选择项序列化为稳定 token。
8. `episodes` 渲染所有已完成集；任务进度读取 `episode_analyses`，不要为未完成集创建空编辑器。
9. 轮询任务终止后重新查询分镜包，以已落库结果为准。
10. `phase=episode_videos` 或已进入后续阶段时，不再重复提交分镜分析；所有分集的分镜分析完成后，
    工作流即把第三步标记为 `completed` 并开放第四步，不等待视频生成。

## 12. 当前能力边界

当前 Agent 专用接口支持资产确认后自动并发生成分镜、未开始分集补位、失败分集精确重试、
结构化镜头自动保存、提示词补充、资产和变体联动、新增、复制、软删除、本集内排序、整集视频
补位及单分镜组调参重生成。当前没有提供 Agent 专用的分镜组合并、拆分或跨集复制接口；前端
不应将普通项目的分镜编辑接口直接接入 Agent 流程。
