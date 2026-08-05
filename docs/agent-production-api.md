# 整剧 Agent API 实现与验收说明

> `workflow_version >= 2` 的第一步详细契约见
> [Agent 第一步：剧本处理 API](./agent-script-processing-api.md)。
>
> 前端联调请优先使用 [agent-api.md](./agent-api.md)。本文档保留各 Epic 的业务实现细节、
> 后台调度约束和验收命令。
>
> 注意：本文档第 2 节记录的是当前“项目优先”实现。目标产品已经调整为“剧本优先”，
> 用户上传文件或输入文本时自动创建 Agent 项目，不再选择项目中心项目。新公开入口以
> `agent-api.md` 第 4 节为准。
>
> 2026-07-24 产品调整：新建界面不再选择模型、失败重试次数、试播集数或积分预算。运行时
> 使用后台默认模型，按实际任务扣费，失败不自动重试，核心资产锁定后直接生产全部剧集。
> 本文后续的模型 ID、`retry_limit`、`pilot_episode_count`、`max_points` 和试播接口仅描述
> 当前兼容实现，不属于新入口契约。

本文档对应整剧生产 Epic A-M。所有接口前缀均为 `/api/v1`，并要求：

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

成功响应统一为：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "timestamp": "2026-07-22 12:00:00"
}
```

## 1. 前端调用流程

```text
上传剧本并预览
  → 使用来源文档创建整剧任务
  → 启动任务
  → 轮询 Agent 工作台
  → 查询/编辑剧本处理包（分集 + 基础资产 + 资产变体）
  → 一次确认剧本处理包
  → 锁定核心资产
  → 生产并确认试播集
  → 批量生产剩余剧集
```

关键状态：

| `status` | `current_stage` | 前端行为 |
| --- | --- | --- |
| `draft` | `source` | 展示“开始分析” |
| `planning` / `running` | `source_analysis` | 轮询整剧详情 |
| `waiting_approval` | `script_review` | 展示分集、角色/场景/道具及其变体的统一审核页 |
| `waiting_approval` | `episode_plan_review` | 展示分集规划编辑器 |
| `planning` | `story_bible` | 调用故事圣经初始化 |
| `waiting_approval` | `story_bible_review` | 展示故事圣经和候选审核 |
| `planning` | `core_assets` | 进入核心资产参考图阶段 |
| `planning` | `core_asset_change_review` | 已锁定参考图发生变更，必须预览影响并重新锁定 |
| `planning` | `pilot_production` | 核心资产已锁定，进入试播生产 |
| `running` | `pilot_storyboards` | 轮询试播状态，等待分镜草案 |
| `waiting_approval` | `pilot_storyboard_review` | 审核分镜和阻断级质检项 |
| `running` | `pilot_images` | 等待故事板图片任务 |
| `waiting_approval` | `pilot_images_ready` | 图片已就绪，可提交视频 |
| `running` | `pilot_videos` | 等待试播视频任务 |
| `waiting_approval` | `pilot_review` | 审核角色一致性、画风、节奏和成本 |
| `planning` | `batch_production` | 试播已通过，可进入剩余剧集批量生产 |
| `running` | `batch_storyboards` | 轮询批量状态，按队列余量补充分镜任务 |
| `running` | `batch_images` | 分镜已收敛，按队列余量补充故事板图片 |
| `running` | `batch_videos` | 图片已收敛，按队列余量补充视频任务 |
| `partially_failed` | `batch_exceptions` | 展示失败项，修复后执行恢复 |
| `completed` | `completed` | 所有剩余剧集生产完成 |
| `paused` / `partially_failed` | 任意 | 展示错误和恢复按钮 |
| `cancelled` / `completed` | 任意 | 停止轮询 |

## 2. 创建与启动

### 2.1 上传剧本并预览

```http
POST /api/v1/projects/{project_id}/agent-productions/source-preview
Content-Type: multipart/form-data
```

表单字段 `file` 支持 `.txt`、`.md`、`.docx` 和可提取文字的 `.pdf`，单文件默认不超过
20MB，抽取后的正文不超过 10 万字符。接口会保存原文件和标准化正文，并返回
来源文档 `id`、前 3000 字预览、字符数、文件哈希、版本及解析警告；创建任务时将该
`id` 作为 `source_document_id` 提交。

V1 不包含 OCR。扫描 PDF 或无可提取文字的 PDF 返回明确错误，前端应提示用户改传 DOCX、
TXT 或可复制文字的 PDF。

### 2.2 创建整剧任务

```http
POST /api/v1/projects/{project_id}/agent-productions
```

```json
{
  "content": "完整剧本文本",
  "mode": "supervised",
  "production_spec": {
    "text_model_id": "UUID",
    "image_model_id": "UUID",
    "video_model_id": "UUID",
    "target_episode_count": 12,
    "target_episode_duration_seconds": 90,
    "default_shot_duration_seconds": 5
  }
}
```

直接粘贴文本时 `content` 不能为空。模型 ID 必须是不同的有效模型记录。

上传预览后无需再次传输完整正文，改为提交预览响应中的文档 ID：

```json
{
  "source_document_id": "UUID",
  "mode": "supervised",
  "production_spec": {
    "text_model_id": "UUID",
    "image_model_id": "UUID",
    "video_model_id": "UUID"
  }
}
```

`content` 与 `source_document_id` 必须且只能提供一个。来源文档必须属于当前用户和当前项目。

### 2.3 启动、暂停、恢复和取消

```http
POST /api/v1/agent-productions/{production_id}/start
POST /api/v1/agent-productions/{production_id}/pause
POST /api/v1/agent-productions/{production_id}/resume
POST /api/v1/agent-productions/{production_id}/cancel
```

重复 `start` 不会重复创建全剧分析步骤。等待人工确认时不能通过 `resume` 跳过确认点。

### 2.4 查询整剧详情和 Agent 工作台

```http
GET /api/v1/agent-productions/{production_id}
GET /api/v1/agent-productions/{production_id}/workbench
```

详情包含生产状态、积分、步骤和确认点。分析期间建议每 3 秒轮询一次；进入
`waiting_approval`、`paused`、`partially_failed`、`cancelled` 或 `completed` 后停止自动轮询。

Agent 页面优先使用 `workbench`。它一次返回 `production`、逐集逐镜头 `matrix`、积分
`costs`、后台续调度 `controller`、`next_action` 和 `available_actions`。Controller 的内部
租约令牌不会返回；公开错误字段会脱敏。`next_action` 是稳定的界面路由提示，例如
`start`、`review_episode_plan`、`manage_core_assets`、`monitor_pilot`、`monitor_batch`、
`review_exceptions` 或 `review_results`，前端仍应以 `available_actions` 决定可展示操作。

## 3. 分集规划

### 3.1 查询和修改

```http
GET   /api/v1/agent-productions/{production_id}/episode-plans
PATCH /api/v1/agent-productions/{production_id}/episode-plans/{plan_id}
```

修改请求必须携带当前版本：

```json
{
  "expected_version": 1,
  "title": "第一集：雨夜旧宅",
  "estimated_duration_seconds": 90,
  "position": 1
}
```

### 3.2 合并、拆分和影响预览

```http
POST /api/v1/agent-productions/{production_id}/episode-plans/merge
POST /api/v1/agent-productions/{production_id}/episode-plans/{plan_id}/split
GET  /api/v1/agent-productions/{production_id}/episode-plans/impact-preview
```

仅允许合并相邻剧集。拆分位置必须落在该集原文字符范围内。

### 3.3 确认分集规划

```http
POST /api/v1/agent-productions/{production_id}/episode-plans/confirm
```

```json
{
  "expected_version": 3,
  "idempotency_key": "episode-plan-confirm-v3"
}
```

首次确认返回实际创建的章节数量；相同版本重复确认返回
`already_confirmed=true`、`created_count=0`，不会重复创建章节。

### 3.4 新流程：统一剧本处理包

由上传或粘贴剧本创建的 `workflow_version=2` 任务，在解析完成后直接进入
`waiting_approval/script_review`。此阶段只生成文本结构，不生成任何参考图。
后端确定性分块原文后，顺序执行两个模型任务：先自主分集，再根据已划分剧本分析并剧情化
设计资产。分集范围必须连续且完整覆盖原文，否则不会创建资产任务。两个任务成功后才会
关联集数、统一落库并切换到 `script_review`。该内部拆分不会增加前端步骤或改变公开接口。

```http
GET   /api/v1/agent-productions/{production_id}/script-package
PATCH /api/v1/agent-productions/{production_id}/asset-variants/{variant_id}
POST  /api/v1/agent-productions/{production_id}/script-package/confirm
```

查询结果统一包含 `episodes`、`characters`、`character_variants`、`scenes`、
`scene_variants`、`props`、`prop_variants` 和 `warnings`。每个变体通过
`base_candidate_id` 关联基础资产，并保存 `variant_type`、`trigger_reason`、
`episode_numbers`；原文证据保留在后端用于校验，不进入前端主响应。基础资产同样返回
`episode_numbers`；每集通过
`source_content` 返回按原文范围切分的完整剧本，并按 `episode_number` 升序排列。

| 资产分类 | 基础资产字段 | 变体字段 |
| --- | --- | --- |
| 人物资产 | `characters` | `character_variants` |
| 场景资产 | `scenes` | `scene_variants` |
| 道具资产 | `props` | `prop_variants` |

资产类型由顶层分组确定，前端不依赖对象中重复的 `asset_type`。完整公开字段、
编辑接口和前端映射以
[Agent 第一步：剧本处理 API](./agent-script-processing-api.md#6-查询统一剧本处理包) 为准。

```json
{
  "expected_script_version": 2,
  "expected_bible_version": 1,
  "idempotency_key": "script-package-confirm-v2"
}
```

统一确认在一个事务内创建章节、物化已接受的基础资产并确认资产版本，成功后直接进入
`planning/core_assets`。结构合法的 `needs_review` 基础资产或变体会自动转为 `ready`；
`rejected` 项不会物化。资产变体仍为文本数据，参考图由后续核心资产阶段处理。

## 4. 故事圣经

### 4.1 初始化与查询

分集规划确认后调用：

```http
POST /api/v1/agent-productions/{production_id}/story-bible/initialize
GET  /api/v1/agent-productions/{production_id}/story-bible
GET  /api/v1/agent-productions/{production_id}/story-bible/versions
```

初始化复用已经结算的全剧分析结果，不会产生新的模型调用或积分扣费。重复初始化返回当前版本。

### 4.2 修改故事圣经

```http
PATCH /api/v1/agent-productions/{production_id}/story-bible
```

```json
{
  "expected_version": 1,
  "content": {
    "continuity_rules": ["沈砚在第一至三集保持灰色长风衣"],
    "forbidden_conflicts": ["玉佩不能在第五集之前损坏"]
  }
}
```

修改会生成新版本，不覆盖旧版本。当前版本已经物化资产后不能直接创建修订版。

## 5. 资产候选

### 5.1 查询候选

```http
GET /api/v1/agent-productions/{production_id}/asset-candidates
GET /api/v1/agent-productions/{production_id}/asset-candidates?asset_type=character
GET /api/v1/agent-productions/{production_id}/asset-candidates?review_status=needs_review
```

候选包含标准名称、别名、来源章节、置信度、合并原因和审核状态。

### 5.2 审核候选

```http
PATCH /api/v1/agent-productions/{production_id}/asset-candidates/{candidate_id}
```

```json
{
  "expected_lock_version": 0,
  "canonical_name": "沈砚",
  "aliases": ["阿砚"],
  "review_status": "ready"
}
```

`needs_review` 必须人工改为 `ready` 或 `rejected`。旧 `lock_version` 返回 409 和当前版本。

### 5.3 批量物化

```http
POST /api/v1/agent-productions/{production_id}/asset-candidates/materialize
```

```json
{
  "expected_bible_version": 1,
  "candidate_ids": ["UUID", "UUID"]
}
```

仅 `ready` 候选可物化。重复提交不会重复创建资产；标准名一致或标准名命中别名时复用已有资产。
复用资产只补齐空白设定，不覆盖用户已经编辑的内容。

### 5.4 确认故事圣经

```http
POST /api/v1/agent-productions/{production_id}/story-bible/confirm
```

```json
{
  "expected_version": 1,
  "idempotency_key": "story-bible-confirm-v1"
}
```

存在 `needs_review` 候选时返回 409。确认成功后整剧进入 `planning/core_assets`。

## 6. 核心资产参考图与锁定

完整请求、响应和错误处理见
[Agent 第二步：核心资产参考图接口文档](./agent-core-assets-api.md)。

### 6.1 基础资产和变体管理

```http
GET    /api/v1/agent-productions/{production_id}/core-assets
POST   /api/v1/agent-productions/{production_id}/core-assets
GET    /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}
PATCH  /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}
DELETE /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}
POST   /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants
DELETE /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}
PUT    /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/reference-image
POST   /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/image-generation
```

基础资产支持按类型筛选、关键词搜索、新建、编辑和删除；资产变体支持新增和删除。写操作只在
`current_stage=core_assets` 且尚未锁定时开放。删除基础资产会同步删除其全部变体。每个变体拥有
独立参考图和生成状态；手动上传不依赖主图，只有模型生成变体图时必须已有基础资产主图。

### 6.2 查询就绪度

```http
GET /api/v1/agent-productions/{production_id}/core-assets/readiness
```

新流程只读取当前 `lock_version` 和 `lock_status`。资产列表、参考图和生成状态由
`GET /core-assets` 返回，不重复消费 readiness 中的旧候选字段和统计字段。

### 6.3 批量生成参考图

```http
POST /api/v1/agent-productions/{production_id}/core-assets/image-generations
```

```json
{
  "items": [
    {
      "asset_type": "character",
      "asset_id": "UUID",
      "prompt": "保持人物设定中的发型和服装"
    },
    {
      "asset_type": "scene",
      "asset_id": "UUID"
    },
    {
      "asset_type": "prop",
      "asset_id": "UUID"
    }
  ]
}
```

新流程固定使用启用状态的 `gpt-image-2`，前端不传 `ai_model_id`。三类图像都固定为
`16:9`：人物为白底“左侧肖像 + 右侧正/侧/背三视图”；场景为单幅完整场景图；道具为
白底“左侧完整图 + 右侧细节角度”。项目本身的画幅不会覆盖核心资产参考图画幅。

批量入口复用现有单资产生成、任务队列、积分扣费和生成历史。处于 `pending/running` 的资产
不会重复提交；超过整剧 `max_points` 时整批拒绝。结果按资产返回任务 ID、状态和积分。该批量
入口只生成物化后的基础人物、场景、道具。

变体使用单独入口：

```http
POST /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/image-generation
```

后端固定使用基础资产当前主图作为模型参考，只应用变体剧情变化；没有主图返回 `40984`。成功
结果只写入变体 `reference_image`，不覆盖基础资产。变体图片也可通过上传后调用变体
`reference-image` 接口绑定。

新 Agent 入口不向前端暴露内部 `project_id`。任务完成后重新查询 `GET /core-assets` 获取最新参考图；
当前尚未提供 Agent 作用域的历史版本切换接口。

### 6.4 上传图片并自动采用

图片先通过 `POST /api/v1/uploads/file` 上传，再绑定到对应资产：

```http
PUT /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/reference-image
```

模型生成成功和手动上传绑定成功都会自动更新当前 `reference_image`，不需要逐张确认。

### 6.5 自动确认并进入第三步

```http
POST /api/v1/agent-productions/{production_id}/core-assets/confirm
```

```json
{
  "expected_lock_version": 0,
  "idempotency_key": "core-assets-confirm-v1"
}
```

前端不传资产列表。后端自动快照当前全部有效人物、场景、道具及其变体参考图，参考图为空也
允许确认；仍有基础资产或变体图像任务执行时返回 `40985`。

### 6.6 影响预览（已有快照变更时）

```http
POST /api/v1/agent-productions/{production_id}/core-assets/impact-preview
```

```json
{
  "expected_lock_version": 1,
  "assets": [
    {"asset_type": "character", "asset_id": "UUID"},
    {"asset_type": "scene", "asset_id": "UUID"}
  ]
}
```

返回新增、移除、参考图变化，以及受影响的分镜、图片和视频数量。影响关系只使用稳定资产
UUID；缺少稳定 ID 的历史分镜只产生告警，不进行名称模糊匹配。

### 6.7 手动选择并锁定（兼容旧前端）

```http
POST /api/v1/agent-productions/{production_id}/core-assets/lock
```

首次锁定：

```json
{
  "expected_lock_version": 0,
  "assets": [
    {"asset_type": "character", "asset_id": "UUID"},
    {"asset_type": "scene", "asset_id": "UUID"}
  ],
  "idempotency_key": "core-assets-lock-v1"
}
```

修改已锁定资产时，必须先调用影响预览，并原样提交返回的指纹：

```json
{
  "expected_lock_version": 1,
  "assets": [
    {"asset_type": "character", "asset_id": "UUID"},
    {"asset_type": "scene", "asset_id": "UUID"}
  ],
  "idempotency_key": "core-assets-lock-v2",
  "impact_fingerprint": "64位sha256"
}
```

指纹或锁版本过期返回 409。确认修改后只标记受影响的下游结果为 `invalidated`，不删除生成
历史，也不会自动重新扣费。新流程首次锁定成功后进入 `planning/batch_production`。如果参考图在
重新锁定前恢复为锁定版本，系统会撤销对应待复核项，并在全部待复核项清空后自动返回原阶段。

## 7. 试播集生产

试播集数量读取生产规格中的 `pilot_episode_count`，默认选择已确认分集规划的第一集，最多
三集。所有写操作都必须提交当前核心资产锁版本和至少 8 个字符的幂等键：

```json
{
  "expected_core_asset_lock_version": 1,
  "idempotency_key": "pilot-action-v1"
}
```

按以下顺序调用：

```http
GET  /api/v1/agent-productions/{production_id}/pilot
POST /api/v1/agent-productions/{production_id}/pilot/storyboards
POST /api/v1/agent-productions/{production_id}/pilot/images
POST /api/v1/agent-productions/{production_id}/pilot/videos
POST /api/v1/agent-productions/{production_id}/pilot/confirm
```

分镜草案生成后，系统仅按锁定资产的标准名和角色别名进行精确匹配，并把稳定 UUID 写入
`storyboard.extra.agent_asset_ids`。角色或场景无法绑定时产生 `error` 并阻止图片生成；未锁定
道具产生 `warning`。明确传入资产 UUID 后，故事板图片不再额外进行名称匹配。

图片阶段使用生产规格的 `image_model_id`，视频阶段使用 `video_model_id` 和
`video_resolution`。视频以故事板图片及锁定资产参考图执行参考生成。阶段内重复提交会跳过
`pending`、`running`、`success` 和 `selected` 镜头，只补交尚未开始或失败的任务。

最终确认是强制关卡；所有视频成功后才能确认，确认成功进入 `planning/batch_production`。
核心资产处于变更复核时，试播状态轮询不会覆盖 `core_asset_change_review`。
审核期间修改分镜会重新执行稳定资产绑定和质检；修改已有媒体的分镜会保留生成历史，但将
图片和视频标记为 `invalidated`，试播阶段自动退回图片或视频生成步骤。

## 8. 旧版剩余剧集批量生产与暂停恢复

> 本节只适用于 `workflow_version=1` 的历史生产链路。`workflow_version>=2` 的四步 Agent
> 不再批量生成故事板图片或全剧视频：资产图像在第二步按项目全局生成/上传，视频在第三步
> 使用 `/episodes/{chapter_id}/video-generations` 按集生成。新版项目调用全局视频模型或全剧
> 媒体调度会返回 `40986`。

试播确认后，批量范围自动排除生产规格中 `pilot_episode_count` 对应的前几集。编排层继续
调用现有分镜、故事板图片和视频提交服务，不直接调用模型或 Worker，因此沿用原有积分、
任务上限、退款和生成历史逻辑。

```http
GET  /api/v1/agent-productions/{production_id}/batch
POST /api/v1/agent-productions/{production_id}/batch/dispatch
```

首次及后续补位使用相同请求结构：

```json
{
  "expected_core_asset_lock_version": 1,
  "idempotency_key": "batch-dispatch-20260722-01",
  "max_tasks": 5
}
```

`GET /batch` 返回当前阶段、分集进度、图片/视频状态计数、活动任务数、阻断项、已提交积分、
剩余积分预估和 `recommended_batch_size`。前端可在活动任务完成后使用新的幂等键再次调用
`dispatch`；服务只补充当前阶段尚未开始或允许重试的任务，并同时受用户总任务上限和媒体
任务上限约束。相同幂等键重试只返回最新状态，不重复提交。

后台控制器也会每 30 秒执行一次补偿扫描。`supervised` 模式需要用户首次调用 `dispatch`，
之后即使关闭页面也会自动续调度；`automatic` 模式在试播确认后可以由控制器直接发起首次
批量调度。人工确认关卡不会因为自动模式被跳过。前端仍可调用 `GET /batch` 展示实时状态，
但不再需要依赖页面轮询维持生产流程。

Agent 创建的叶子任务会在 `UserTaskRecord.extra` 中写入
`agent_production_id`、`agent_step_id`、`agent_stage`、`agent_scope_type`、
`agent_scope_id` 和 `agent_attempt_number`，用于成本、异常和审计精确归属。

后台控制器使用 `agent_controller_states` 保存排队/运行租约、尝试次数、最后结果和失败原因。
同一生产实例在租约有效期内只允许一个 Controller 执行；消息丢失或 Worker 崩溃后，Beat 会在
租约到期后重新领取。执行失败会进入短暂退避，并写入 `controller.failed` Agent 事件。

阶段固定按 `storyboards → images → videos` 推进。某集或某镜头失败不会阻止其他可生产项；
达到生产规格 `retry_limit + 1` 次尝试后，该项进入异常汇总，最终状态为
`partially_failed/batch_exceptions`。角色或场景不能绑定当前核心资产锁时，该分镜被阻断且
不会进入图片阶段。

暂停和恢复继续使用整剧通用接口：

```http
POST /api/v1/agent-productions/{production_id}/pause
POST /api/v1/agent-productions/{production_id}/resume
```

暂停只关闭“创建新任务”的闸门，不取消已经提交的叶子任务，也不退回其积分。在途任务可以
正常完成，`GET /batch` 仍会收敛其结果，但不会提交下一阶段任务。恢复后系统根据数据库中的
批量阶段和现有任务状态自动补位，不会重新提交已处于 `pending`、`running`、`success` 或
`selected` 的任务。取消整剧才会终止仍为 `pending` 的批量叶子任务并按原规则退款。

## 9. 生产矩阵与异常处理

生产控制台使用以下只读接口：

```http
GET /api/v1/agent-productions/{production_id}/matrix
GET /api/v1/agent-productions/{production_id}/exceptions?page=1&page_size=50
GET /api/v1/agent-productions/{production_id}/events?page=1&page_size=50
GET /api/v1/agent-productions/{production_id}/costs
```

`matrix` 按剧集和镜头返回分镜、图片、视频状态，并包含当前任务、尝试次数、当前任务积分、
公开失败原因、是否可重试/跳过，以及是否需要显式确认突破自动重试上限。`exceptions` 将失败
归类为内容审核、模型失败、积分不足、队列限制、资产绑定、图片缺失或上游变更。

精确重试只支持试播范围之外的批量生产项：

```http
POST /api/v1/agent-productions/{production_id}/jobs/retry
```

```json
{
  "expected_core_asset_lock_version": 1,
  "stage": "video",
  "scope_ids": ["storyboard UUID"],
  "idempotency_key": "manual-retry-video-v1",
  "confirm_over_retry_limit": true
}
```

`stage=storyboard` 时 `scope_ids` 为章节 UUID；`image`、`video` 时为分镜 UUID。只能重试
`failed`、`invalidated` 或先前人工跳过的项目。超过生产规格的自动重试次数后，必须显式提交
`confirm_over_retry_limit=true`。请求提交前会重新检查队列容量、核心资产锁和积分预算。

人工跳过或替换使用：

```http
POST /api/v1/agent-productions/{production_id}/jobs/skip
```

```json
{
  "expected_core_asset_lock_version": 1,
  "stage": "video",
  "scope_ids": ["storyboard UUID"],
  "reason": "使用人工审核通过的视频替换",
  "replacement_url": "https://example.com/manual-video.mp4",
  "idempotency_key": "manual-replace-video-v1"
}
```

`replacement_url` 为空表示跳过；有值时一次只能处理一个图片或视频镜头。外部文件应先通过
现有上传接口进入平台存储，再把返回地址提交给本接口。人工替换会新增一条选中的生成历史，
不会删除原结果。跳过图片且不提供替换时，整个镜头的视频阶段也会标记为跳过。所有重试、
跳过和替换都会写入 `AgentEvent`，相同幂等键不会重复提交或扣费。

## 10. 逐集审片与剪映草稿交付

视频生成完成后，工作台的 `next_action` 为 `review_results`。聚合审片接口返回剧集、
分镜组主视频、人工问题和集级确认状态：

```http
GET /api/v1/agent-productions/{production_id}/review
```

记录和处理人工质量问题：

```http
POST  /api/v1/agent-productions/{production_id}/review-issues
PATCH /api/v1/agent-productions/{production_id}/review-issues/{issue_id}
```

问题类别包括角色一致性、画风漂移、动作、节奏、台词、音频、合规和其他。严重级别为
`info`、`warning`、`blocking`；存在未解决 `blocking` 问题时不能确认该集。

单集剪辑式预览接口会按 `group_number` 返回当前主视频：

```http
GET /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/video-timeline
```

```http
POST /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/approve
GET  /api/v1/agent-productions/{production_id}/delivery-readiness
```

确认请求需要 `expected_lock_version` 和至少 8 个字符的 `idempotency_key`。只有镜头视频
全部就绪且没有阻断问题的剧集才能确认。

剪映草稿导出接口：

```http
POST /api/v1/agent-productions/{production_id}/jianying-exports
GET  /api/v1/agent-productions/{production_id}/jianying-exports
GET  /api/v1/agent-productions/{production_id}/jianying-exports/{export_id}
```

```json
{
  "platform": "macos",
  "jianying_version": "10.8",
  "draft_name": "灵境漫剧-全剧",
  "chapter_ids": [],
  "idempotency_key": "jianying-macos-all-v1"
}
```

`platform` 由用户请求决定，只允许 `windows` 或 `macos`。`chapter_ids=[]` 表示全剧；指定集数可以
生成部分草稿，但只有全剧草稿完成时才完成第四步。`story_ai_delivery` Worker 按集数、
分镜组顺序下载当前主视频，生成剪映专业版 10.8 草稿、平台路径重定位脚本和使用说明，
打包为 ZIP 后上传 OSS。

## 11. 并发和幂等规则

- 分集规划写操作使用 `expected_version`。
- 故事圣经写操作使用 `expected_version`。
- 单个候选修改使用 `expected_lock_version`。
- 核心资产选择使用独立的 `expected_lock_version` 和影响指纹。
- 试播写操作使用 `expected_core_asset_lock_version`。
- 批量补位使用 `expected_core_asset_lock_version`、`max_tasks` 和独立幂等键。
- 精确重试和人工处理使用当前核心资产锁版本及独立幂等键。
- 审片问题使用 `expected_lock_version`，集级确认保存媒体快照哈希。
- 每个剪映导出请求使用生产实例范围内的独立幂等键。
- 确认操作必须提供至少 8 个字符的 `idempotency_key`。
- 前端收到 409 后必须重新查询最新数据，不得用旧版本自动覆盖。
- 网络超时后可以使用原请求体重试确认或物化请求。

## 12. 常用错误码

| HTTP | `code` | 含义 | 前端处理 |
| --- | --- | --- | --- |
| 404 | `40430` | 整剧任务不存在或不属于当前用户 | 返回项目页 |
| 404 | `40431` | 预览剧本文档不存在或不属于当前项目 | 重新上传并预览 |
| 404 | `40440` / `40441` | 审片剧集或分镜不存在 | 刷新审片页 |
| 404 | `40442` / `40443` | 审片问题或媒体版本不存在 | 刷新问题和版本列表 |
| 404 | `40444` | 交付任务不存在 | 刷新交付列表 |
| 400 | `40053` | 剧本文件类型不支持 | 改传 TXT、MD、DOCX 或 PDF |
| 400 | `40054` | 上传文件为空 | 重新选择文件 |
| 400 | `40055` | 文件未抽取到正文 | 检查内容；扫描 PDF 改传文本版 |
| 400 | `40056` | 文本编码无法识别 | 将文件转为 UTF-8 |
| 400 | `40057` | DOCX 损坏、过大、条目过多或压缩比例异常 | 重新导出 DOCX |
| 400 | `40058` | PDF 损坏或加密 | 解除加密或重新导出 |
| 400 | `40059` | PDF 超过 500 页 | 拆分或精简剧本 |
| 400 | `40060` | 剧本文件名超过 255 个字符 | 缩短文件名后重试 |
| 400 | `40063` | 剪映平台或草稿版本不支持 | 改用 Windows/macOS 和 10.8 |
| 400 | `40064` | PDF/DOCX 隔离解析进程异常终止 | 重新导出文件后上传 |
| 408 | `40801` | PDF/DOCX 解析超时 | 拆分或重新导出文件后上传 |
| 413 | `41301` | 剧本文件超过上传上限 | 压缩或拆分文件 |
| 404 | `40433` | 资产候选不存在 | 刷新候选列表 |
| 409 | `40915` | 分集规划尚未生成 | 继续轮询分析状态 |
| 409 | `40918` | 当前状态不能修改分集规划 | 刷新整剧详情 |
| 409 | `40920` | 分集规划版本冲突 | 重新查询分集规划 |
| 409 | `40923` | 当前故事圣经已有物化资产 | 保留当前版本或进入影响评估 |
| 409 | `40926` | 候选未审核或已拒绝 | 完成人工审核 |
| 409 | `40927` | 仍有低置信度候选 | 筛选 `needs_review` |
| 409 | `40930` | 故事圣经版本冲突 | 重新查询故事圣经 |
| 409 | `40931` | 候选锁版本冲突 | 重新查询候选 |
| 409 | `40932` | 当前状态或阶段不能操作核心资产 | 刷新整剧详情 |
| 409 | `40933` | 核心资产锁版本冲突 | 重新查询就绪度 |
| 409 | `40934` | 核心资产尚未选择参考图 | 生成、上传或选择参考图 |
| 409 | `40935` | 影响指纹已经变化 | 重新执行影响预览 |
| 409 | `40940` | 核心资产未锁定或正在变更复核 | 返回核心资产阶段 |
| 409 | `40941` | 当前试播阶段不允许操作 | 刷新试播状态 |
| 409 | `40942` | 分镜存在阻断级质检错误 | 修改分镜或锁定缺失资产 |
| 409 | `40943` | 故事板图片未全部成功 | 等待或重试图片任务 |
| 409 | `40944` | 试播视频未全部成功 | 等待或重试视频任务 |
| 409 | `40945` | 核心资产锁版本冲突 | 刷新试播状态和核心资产锁 |
| 409 | `40950` | 核心资产未锁定、试播未确认或锁变更待复核 | 返回对应上游阶段 |
| 409 | `40951` | 当前整剧状态或阶段不能批量生产 | 刷新整剧和批量状态 |
| 409 | `40952` | 整剧已暂停 | 不再调用补位，展示恢复按钮 |
| 409 | `40953` | 故事板图片结果缺失 | 重试图片或检查生成历史 |
| 409 | `40955` | 批量生产使用的核心资产锁版本冲突 | 刷新批量状态后重试 |
| 409 | `40960` | 批量生产未开始或当前状态不能人工处理 | 刷新整剧状态 |
| 409 | `40961` | 目标不在批量范围或当前状态不允许操作 | 刷新生产矩阵 |
| 409 | `40962` | 分镜仍有资产绑定错误 | 修复资产引用或人工跳过 |
| 409 | `40963` | 视频依赖的故事板图片未就绪 | 先处理图片异常 |
| 409 | `40964` | 已达到自动重试上限 | 人工确认成本后显式重试 |
| 409 | `40965` | 已有分镜的剧集不能整集跳过分镜分析 | 改为镜头级处理 |
| 409 | `40970` | 审片问题版本冲突 | 重新查询问题 |
| 409 | `40971` | 重生成幂等键参数不一致 | 使用新幂等键 |
| 409 | `40972` / `40973` | 视频未就绪或仍有阻断问题 | 完成生成或解决问题 |
| 409 | `40974` / `40975` | 确认快照变化或审片版本冲突 | 刷新剧集后重新确认 |
| 409 | `40976` | 交付幂等键参数不一致 | 使用新幂等键 |
| 409 | `40979` | 尚未满足交付条件 | 查看 delivery-readiness |
| 409 | `40984` | 生成资产变体图时基础资产没有主图 | 先上传或生成基础资产主图；也可直接手动上传变体图 |
| 409 | `40985` | 确认时仍有基础资产或变体图像任务执行中 | 等待任务结束后重试 |
| 409 | `40986` | 新版 Agent 尝试使用全剧媒体调度 | 改用目标集的 `video-generations` |
| 422 | `42200` | 请求参数校验失败 | 展示 `data` 中字段错误 |
| 429 | `42920` | 用户待处理任务达到上限 | 等待已有任务完成 |

权限查询统一加入当前用户条件，因此跨用户访问整剧、故事圣经或候选时返回 404，避免泄露资源存在性。

## 13. Epic F-M 验收命令

普通单元测试：

```bash
.venv/bin/python -m pytest -q
```

真实 PostgreSQL 集成测试使用独立临时 schema，执行完成后自动删除：

```bash
RUN_DB_INTEGRATION_TESTS=1 .venv/bin/python -m pytest -q \
  tests/test_agent_production_integration.py
```
