# Agent 第一步：剧本处理 API

| 项目     | 内容                               |
| -------- | ---------------------------------- |
| 文档版本 | 1.4                                |
| 更新日期 | 2026-08-02                         |
| API 前缀 | `/api/v1`                          |
| 适用流程 | `workflow_version >= 2`            |
| 完成状态 | `waiting_approval / script_review` |
| 下一阶段 | `planning / core_assets`           |

## 1. 功能范围

第一步接收用户上传或粘贴的完整剧本，异步完成：

1. 后端确定性分块原文，仅用于传输、定位和完整性校验，不调用模型；
2. 第一个模型任务分析剧情结构，自主确定合理集数并生成可审阅的剧集划分；
3. 分集结果校验通过后，第二个模型任务按已划分剧本提取人物、场景、道具及各自变体；
4. 资产分析同时生成忠于原文、可直接用于后续参考图生成的剧情化视觉设计；
5. 后端依据原文证据将资产和变体关联到具体集数；
6. 将分集和资产分析结果统一落库；
7. 等待用户统一审核；
8. 一次确认后创建正式剧集和基础资产，进入核心资产阶段。

本阶段只处理文本和结构化数据，不生成角色、场景、道具参考图，也不选择视频模型。
后端固定为两个顺序执行的模型任务，前端仍只展示一个“剧本处理”步骤，公开状态和接口不变。

## 2. 认证与通用响应

所有接口都需要当前登录用户身份：

```http
Authorization: Bearer <access_token>
```

成功响应：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "timestamp": "2026-07-29 10:00:00"
}
```

失败响应：

```json
{
  "code": 40941,
  "message": "剧本处理版本冲突，当前版本为 3",
  "data": {
    "current_script_version": 3
  },
  "timestamp": "2026-07-29 10:00:00"
}
```

HTTP `422` 的业务码固定为 `42200`，`data` 返回字段校验错误列表。

### 2.1 第一步公开响应原则

第一步响应只公开前端完成“查看分集、审核资产、编辑、确认”所需的数据。服务端为兼容旧调用方
可能暂时返回额外字段，但新前端不得依赖以下内部字段：

- `project_id`、`production_id`（路径中已有 `production_id` 时）、`bible_version_id`；
- `source_document_id`、`source_chapter_ids`、`step_id`、`checkpoint_id`；
- `source_block_ids` 等内部原文分块字段；剧集 `source_start/source_end` 因编辑和拆分仍保留；
- 模型 `confidence`、`merge_reason` 和内部 `materialized_asset_id`；
- `created_at`、`updated_at`；
- 完整模型 Prompt、模型 ID、供应商任务字段和任务 `extra`。

第一步仍必须保留以下并发和关联字段：

- `plan_id`：编辑、合并或拆分指定剧集；
- 基础资产 `id`：编辑基础资产，并供变体通过 `base_candidate_id` 关联；
- 变体 `id`：编辑变体；
- `script_version`、`bible_version`、各对象 `lock_version`：防止覆盖并发修改；
- `task_record_id`：仅在需要轮询异步任务时返回。

## 3. 前端推荐调用顺序

```text
创建 Agent 项目
  → 启动任务
  → 后端确定性分块（不调用模型）
  → 模型任务 1：自主分集
  → 后端校验分集范围连续且完整覆盖原文
  → 模型任务 2：按分集分析并剧情化设计资产与变体
  → 后端按分集原文范围关联基础资产和资产变体的出现集数
  → 后端统一校验并落库
  → 轮询工作台
  → next_action = review_script
  → 查询 script-package
  → 审核模式可补充文本或文件；补充后重新轮询分析
  → 编辑分集、基础资产和资产变体
  → 每次写操作后重新查询 script-package
  → 查询分集影响预览并处理警告
  → 确认 script-package
  → 进入 core_assets
```

关键状态：

| `status`               | `current_stage`   | `next_action`        | 前端行为                               |
| ---------------------- | ----------------- | -------------------- | -------------------------------------- |
| `draft`                | `source`          | `start`              | 显示开始分析                           |
| `planning` / `running` | `source_analysis` | `monitor_progress`   | 轮询工作台                             |
| `partially_failed`     | `source_analysis` | `review_exceptions`  | 展示失败原因；用户可显式恢复或取消     |
| `paused`               | `source_analysis` | `resume`             | 编排或调度重投耗尽，等待用户恢复或取消 |
| `waiting_approval`     | `script_review`   | `review_script`      | 展示剧本处理审核页                     |
| `planning`             | `core_assets`     | `manage_core_assets` | 进入参考图和核心资产锁定               |

第一步不配置用户可选的业务失败重试次数。内容契约错误等不可重试失败会直接进入
`partially_failed`；超时、限流或临时服务错误可能按 Celery 全局配置进行基础设施重投，
重投使用同一任务记录和幂等键。基础设施重投耗尽后不会继续自动提交，只有用户显式调用恢复
接口，后端才会创建新的文本任务并重新扣费。

## 4. 创建 Agent 项目

### 4.1 粘贴剧本

```http
POST /api/v1/agent-productions/from-text
Content-Type: application/json
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

字段：

| 字段               | 必填 | 说明                                               |
| ------------------ | ---- | -------------------------------------------------- |
| `name`             | 否   | 1～128 字；不传时根据内容生成名称                  |
| `content`          | 是   | 非空完整剧本                                       |
| `style_id`         | 是   | 启用的项目风格 ID                                  |
| `generation_ratio` | 是   | 视频画面比例                                       |
| `video_resolution` | 是   | `480p`、`720p`、`1080p` 或 `4k`                    |
| `mode`             | 是   | `supervised`（审核模式）或 `automatic`（自动模式） |
| `video_model_id`   | 条件 | 自动模式必填；审核模式不传                         |

集数由 Agent 根据剧情结构自动决定，第一步不接受目标集数、单集时长或分镜时长。创建请求
不接受文本/图像模型 ID、失败重试次数、试播集数、积分预算或音频开关；JSON 中传入未定义
字段会返回参数校验错误。自动模式提前选择视频模型，审核模式在第三步真正进入视频生成阶段
时选择。

自动模式还要求账户余额至少为 100 积分。后端在创建、保存自动模式草稿配置和正式启动时
校验；100 积分只作为准入门槛，不会预扣，后续模型任务仍按实际使用扣费。余额不足返回
HTTP `400`、业务码 `40003`。

响应核心字段：

```json
{
  "production_id": "22222222-2222-2222-2222-222222222222",
  "name": "雨夜旧宅",
  "status": "draft",
  "current_stage": "source",
  "source": {
    "source_type": "text",
    "file_name": null,
    "character_count": 50231,
    "content_preview": "剧本开头……",
    "content_preview_truncated": true,
    "warnings": []
  },
  "style_id": "11111111-1111-1111-1111-111111111111",
  "generation_ratio": "9:16",
  "video_resolution": "1080p",
  "mode": "automatic",
  "video_model_id": "77777777-7777-7777-7777-777777777777"
}
```

创建响应不公开原文哈希、内部源文档 ID、文本/图像模型配置或时间戳。创建接口已经要求完整配置，
`configuration_required` 对新流程恒为 `false`，前端无需依赖。

### 4.2 上传剧本文件

```http
POST /api/v1/agent-productions/from-file
Content-Type: multipart/form-data
```

表单字段：

| 字段               | 类型   | 必填 | 说明                                                   |
| ------------------ | ------ | ---- | ------------------------------------------------------ |
| `file`             | File   | 是   | 文件名必须带扩展名；支持`.txt`、`.md`、`.docx`、`.pdf` |
| `name`             | String | 否   | Agent 项目名称                                         |
| `style_id`         | UUID   | 是   | 启用的项目风格 ID                                      |
| `generation_ratio` | String | 是   | 视频画面比例                                           |
| `video_resolution` | String | 是   | `480p`、`720p`、`1080p` 或 `4k`                        |
| `mode`             | String | 是   | `supervised` 或 `automatic`                            |
| `video_model_id`   | UUID   | 条件 | 自动模式必填；审核模式不传                             |

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

响应结构与粘贴剧本一致。

文件处理约束：

- 默认文件上限为 20 MB，以服务端 `agent_source_max_file_size_mb` 配置为准；
- 抽取后的正文默认不超过 100,000 字符，以 `agent_source_max_characters` 配置为准；
- TXT/Markdown 支持 UTF-8（含 BOM）和 GB18030；
- PDF 最多 500 页，不支持加密 PDF，也不提供扫描 PDF OCR；
- 文件为空、扩展名不支持、文件损坏或未抽取到正文时，整个创建请求失败，不创建 Agent 项目。

### 4.3 查询或修改剧本整体配置

上传或粘贴时已经保存整体风格、画面比例、分辨率和运行模式，因此创建后可以直接启动分析。
启动前仍可通过以下接口查询或修改：

```http
GET /api/v1/styles
GET /api/v1/agent-productions/{production_id}/configuration
PUT /api/v1/agent-productions/{production_id}/configuration
```

PUT 请求：

```json
{
  "style_id": "11111111-1111-1111-1111-111111111111",
  "generation_ratio": "9:16",
  "video_resolution": "1080p",
  "mode": "automatic",
  "video_model_id": "77777777-7777-7777-7777-777777777777"
}
```

- `style_id` 必须来自启用风格列表；
- `generation_ratio` 支持 `21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16`；
- `video_resolution` 支持 `480p`、`720p`、`1080p`、`4k`；
- `mode=supervised` 表示关键节点人工确认；`automatic` 表示强制关卡外自动推进；
- `video_model_id` 在自动模式必填并随项目配置保存，审核模式不传；
- 配置只允许在 `draft/source` 阶段保存，启动后不可修改；
- 新建任务工作台的 `next_action` 直接为 `start`；历史未配置任务调用 `/start` 返回 `40981`。

### 4.4 删除 Agent 项目

```http
DELETE /api/v1/agent-productions/{production_id}
```

请求体为空。删除采用软删除：

- 只能删除当前用户自己的 Agent 项目；
- 未完成生产先切换为 `cancelled`，未开始或排队中的步骤停止继续执行；
- 已完成生产保留 `completed` 状态；
- 内部 Agent 承载项目设置为不可用，不物理删除剧本、分集、资产和审计记录；
- 删除后列表不再返回该项目，详情、工作台和剧本处理包查询返回 `40430`；
- 重复删除返回 `40430`。

成功响应的 `data`：

```json
{
  "production_id": "22222222-2222-2222-2222-222222222222",
  "status": "cancelled",
  "deleted": true
}
```

## 5. 启动与轮询

### 5.1 启动

```http
POST /api/v1/agent-productions/{production_id}/start
```

请求体为空。启动时后端固定解析：

- 文本模型：`model_id=gpt-5.5`；
- 图像模型：`model_id=gpt-image-2`。

固定模型未启用时返回 `40980`。视频模型在后续视频阶段选择，本阶段不传。
整体配置未完成时先返回 `40981`，不会创建分析任务或解析固定模型。

成功响应只需要读取现有完整生产详情中的以下公开字段：

```json
{
  "id": "22222222-2222-2222-2222-222222222222",
  "status": "planning",
  "current_stage": "source_analysis"
}
```

`project_id`、`source_document_id`、`production_spec`、积分预算、内部锁版本、时间戳以及步骤内部
ID都不属于第一步前端契约。启动成功后直接轮询工作台。

### 5.2 查询工作台

```http
GET /api/v1/agent-productions/{production_id}/workbench
```

前端以以下字段决定页面：

```json
{
  "next_action": "review_script",
  "available_actions": ["review_script", "cancel"],
  "production": {
    "id": "22222222-2222-2222-2222-222222222222",
    "status": "waiting_approval",
    "current_stage": "script_review",
    "error_summary": null,
    "steps": [
      {
        "stage": "source_analysis",
        "status": "completed",
        "progress_current": 2,
        "progress_total": 2,
        "error_summary": null
      }
    ]
  }
}
```

第一步轮询不读取工作台中的 `matrix`、`costs`、`controller.last_result`，也不读取生产详情中的
`project_id`、模型配置、积分或步骤 `extra`。这些是后续生产监控或内部调度数据。

解析期间建议轮询；当 `next_action` 变为 `review_script` 后停止高频轮询并查询剧本处理包。
`production.steps[]` 中 `stage=source_analysis` 的 `progress_total` 固定为 `2`：第一项是分集
规划，第二项是资产分析与剧情化设计。前端仍将其作为一个剧本处理步骤展示。

## 6. 查询统一剧本处理包

```http
GET /api/v1/agent-productions/{production_id}/script-package
```

### 6.1 三类资产分类契约

接口不会返回一个需要前端自行判断类型的通用 `assets` 数组，而是将分析结果固定拆成三类资产、
六个顶层字段：

| 资产分类 | 基础资产字段 | 变体字段             | 变体关联                                   |
| -------- | ------------ | -------------------- | ------------------------------------------ |
| 人物资产 | `characters` | `character_variants` | `base_candidate_id` 指向 `characters[].id` |
| 场景资产 | `scenes`     | `scene_variants`     | `base_candidate_id` 指向 `scenes[].id`     |
| 道具资产 | `props`      | `prop_variants`      | `base_candidate_id` 指向 `props[].id`      |

对接时必须遵守：

1. 三类基础资产分别展示和编辑，不要合并成无法区分类型的列表；
2. 顶层字段就是公开分类依据，前端不依赖对象内重复的 `asset_type`；
3. 变体不是独立基础资产，必须通过 `base_candidate_id` 挂在同类型基础资产下；
4. `episodes[].characters/scenes/props` 等字段只是该集引用的资产名称，不是完整资产对象；
5. `episodes` 已按 `episode_number` 升序返回，`source_content` 是本集完整剧本；保留
   `source_start/source_end` 供编辑和拆分，模型整理用的 `content` 和分块 ID不属于主响应；
6. 基础资产和资产变体都通过顶层 `episode_numbers` 标明出现集数；
7. 第一步只返回文本和结构化资产数据，不返回或生成参考图。

公开响应示例。新前端只依赖以下字段；服务端暂时返回的兼容字段应忽略：

```json
{
  "script_version": 3,
  "bible_version": 1,
  "status": "waiting_review",
  "episodes": [
    {
      "plan_id": "33333333-3333-3333-3333-333333333333",
      "episode_number": 1,
      "title": "雨夜入宅",
      "source_content": "本集对应的完整原始剧本文本……",
      "source_start": 0,
      "source_end": 3200,
      "characters": ["沈砚"],
      "character_variants": ["沈砚受伤造型"],
      "scenes": ["旧宅"],
      "scene_variants": ["雨夜旧宅"],
      "props": ["玉佩"],
      "prop_variants": ["碎裂玉佩"]
    }
  ],
  "characters": [
    {
      "id": "44444444-4444-4444-4444-444444444444",
      "canonical_name": "沈砚",
      "aliases": ["阿砚"],
      "episode_numbers": [1],
      "review_status": "ready",
      "content": {
        "source_facts": {
          "story_role": "调查玉佩失踪事件的主角",
          "gender": "男"
        },
        "design_spec": {
          "apparent_age": "二十七八岁",
          "body_type": "高挑偏瘦",
          "default_costume": "黑色长风衣",
          "identity_anchors": ["利落黑色短发", "窄长脸"]
        }
      },
      "lock_version": 0
    }
  ],
  "character_variants": [
    {
      "id": "77777777-7777-7777-7777-777777777777",
      "base_candidate_id": "44444444-4444-4444-4444-444444444444",
      "canonical_name": "沈砚受伤造型",
      "variant_type": "injury",
      "description": "额角流血，衣领沾血",
      "trigger_reason": "遭到黑衣人袭击",
      "episode_numbers": [1],
      "content": {
        "visual_delta": {
          "add": ["额角伤口和血迹"],
          "replace": [],
          "remove": []
        },
        "preserve_anchors": ["利落黑色短发", "窄长脸"],
        "state_scope": "from_episode_until_changed"
      },
      "review_status": "ready",
      "lock_version": 0
    }
  ],
  "scenes": [],
  "scene_variants": [],
  "props": [],
  "prop_variants": [],
  "warnings": []
}
```

`scenes`、`props` 与 `characters` 使用同一基础资产字段；三类 `*_variants` 使用同一变体字段。
基础资产的 `content` 只约定 `source_facts` 和 `design_spec`；变体的 `content` 只约定
`visual_delta`、`preserve_anchors` 和 `state_scope`。服务端暂时保留的其他原始模型字段不属于
公开契约。

<!-- 以下为后端旧版完整响应，仅保留迁移核对，不属于公开契约。

```json
{
  "production_id": "22222222-2222-2222-2222-222222222222",
  "script_version": 3,
  "bible_version": 1,
  "status": "waiting_review",
  "episodes": [
    {
      "plan_id": "33333333-3333-3333-3333-333333333333",
      "episode_number": 1,
      "title": "雨夜入宅",
      "content": "本集漫剧化剧情内容",
      "source_content": "本集对应的完整原始剧本文本……",
      "logline": "沈砚进入旧宅寻找玉佩",
      "opening_hook": "旧宅门自行打开",
      "goal": "找到失踪的玉佩",
      "conflict": "黑衣人阻拦",
      "climax": "双方在祠堂对峙",
      "ending_hook": "玉佩突然碎裂",
      "source_start": 0,
      "source_end": 3200,
      "source_block_ids": [],
      "characters": ["沈砚"],
      "character_variants": ["沈砚受伤造型"],
      "scenes": ["旧宅"],
      "scene_variants": ["雨夜旧宅"],
      "props": ["玉佩"],
      "prop_variants": ["碎裂玉佩"],
      "continuity_notes": []
    }
  ],
  "characters": [
    {
      "id": "44444444-4444-4444-4444-444444444444",
      "project_id": "55555555-5555-5555-5555-555555555555",
      "production_id": "22222222-2222-2222-2222-222222222222",
      "bible_version_id": "66666666-6666-6666-6666-666666666666",
      "asset_type": "character",
      "canonical_name": "沈砚",
      "aliases": ["阿砚"],
      "episode_numbers": [1],
      "source_chapter_ids": [],
      "confidence": 0.98,
      "merge_reason": "标准名称一致",
      "review_status": "ready",
      "content": {
        "source_facts": {
          "story_role": "调查玉佩失踪事件的主角",
          "gender": "男"
        },
        "design_spec": {
          "apparent_age": "二十七八岁",
          "body_type": "高挑偏瘦",
          "default_costume": "黑色长风衣",
          "identity_anchors": ["利落黑色短发", "窄长脸"],
          "design_rationale": "冷色利落造型服务悬疑调查剧情"
        },
        "appearance": "高挑偏瘦，窄长脸，利落黑色短发",
        "costume": "黑色长风衣",
        "prompt": "剧情定位：调查者，视觉设计：冷色利落造型，身份固定特征：利落黑色短发、窄长脸",
        "personality": "沉稳",
        "source_evidence": [
          {
            "source_start": 120,
            "source_end": 168,
            "source_quote": "沈砚推开旧宅木门"
          }
        ]
      },
      "materialized_asset_id": null,
      "lock_version": 0,
      "created_at": "2026-07-29T10:00:00+08:00",
      "updated_at": "2026-07-29T10:00:00+08:00"
    }
  ],
  "character_variants": [
    {
      "id": "77777777-7777-7777-7777-777777777777",
      "project_id": "55555555-5555-5555-5555-555555555555",
      "production_id": "22222222-2222-2222-2222-222222222222",
      "bible_version_id": "66666666-6666-6666-6666-666666666666",
      "base_candidate_id": "44444444-4444-4444-4444-444444444444",
      "asset_type": "character",
      "canonical_name": "沈砚受伤造型",
      "variant_type": "injury",
      "description": "额角流血，衣领沾血",
      "trigger_reason": "遭到黑衣人袭击",
      "episode_numbers": [1],
      "source_evidence": [
        {
          "source_start": 850,
          "source_end": 930,
          "source_quote": null
        }
      ],
      "confidence": 0.95,
      "review_status": "ready",
      "content": {
        "visual_delta": {
          "add": ["额角伤口和血迹"],
          "replace": [],
          "remove": []
        },
        "preserve_anchors": ["利落黑色短发", "窄长脸"],
        "state_scope": "from_episode_until_changed"
      },
      "lock_version": 0,
      "created_at": "2026-07-29T10:00:00+08:00",
      "updated_at": "2026-07-29T10:00:00+08:00"
    }
  ],
  "scenes": [
    {
      "id": "88888888-8888-8888-8888-888888888888",
      "project_id": "55555555-5555-5555-5555-555555555555",
      "production_id": "22222222-2222-2222-2222-222222222222",
      "bible_version_id": "66666666-6666-6666-6666-666666666666",
      "asset_type": "scene",
      "canonical_name": "旧宅",
      "aliases": ["沈家旧宅"],
      "episode_numbers": [1],
      "source_chapter_ids": [],
      "confidence": 0.96,
      "merge_reason": "指向同一地点",
      "review_status": "ready",
      "content": {
        "location": "城郊",
        "visual_features": "荒废院落和木质祠堂",
        "source_evidence": [
          {
            "source_start": 40,
            "source_end": 180
          }
        ]
      },
      "materialized_asset_id": null,
      "lock_version": 0,
      "created_at": "2026-07-29T10:00:00+08:00",
      "updated_at": "2026-07-29T10:00:00+08:00"
    }
  ],
  "scene_variants": [
    {
      "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      "project_id": "55555555-5555-5555-5555-555555555555",
      "production_id": "22222222-2222-2222-2222-222222222222",
      "bible_version_id": "66666666-6666-6666-6666-666666666666",
      "base_candidate_id": "88888888-8888-8888-8888-888888888888",
      "asset_type": "scene",
      "canonical_name": "雨夜旧宅",
      "variant_type": "weather",
      "description": "暴雨笼罩，地面积水反光",
      "trigger_reason": "第一集发生在暴雨夜",
      "episode_numbers": [1],
      "source_evidence": [
        {
          "source_start": 120,
          "source_end": 180,
          "source_quote": null
        }
      ],
      "confidence": 0.94,
      "review_status": "ready",
      "content": {},
      "lock_version": 0,
      "created_at": "2026-07-29T10:00:00+08:00",
      "updated_at": "2026-07-29T10:00:00+08:00"
    }
  ],
  "props": [
    {
      "id": "99999999-9999-9999-9999-999999999999",
      "project_id": "55555555-5555-5555-5555-555555555555",
      "production_id": "22222222-2222-2222-2222-222222222222",
      "bible_version_id": "66666666-6666-6666-6666-666666666666",
      "asset_type": "prop",
      "canonical_name": "玉佩",
      "aliases": ["沈家玉佩"],
      "episode_numbers": [1],
      "source_chapter_ids": [],
      "confidence": 0.97,
      "merge_reason": "同一关键道具",
      "review_status": "ready",
      "content": {
        "appearance": "青白色双鱼纹玉佩",
        "importance": "推动主线的关键道具",
        "source_evidence": [
          {
            "source_start": 420,
            "source_end": 486
          }
        ]
      },
      "materialized_asset_id": null,
      "lock_version": 0,
      "created_at": "2026-07-29T10:00:00+08:00",
      "updated_at": "2026-07-29T10:00:00+08:00"
    }
  ],
  "prop_variants": [
    {
      "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
      "project_id": "55555555-5555-5555-5555-555555555555",
      "production_id": "22222222-2222-2222-2222-222222222222",
      "bible_version_id": "66666666-6666-6666-6666-666666666666",
      "base_candidate_id": "99999999-9999-9999-9999-999999999999",
      "asset_type": "prop",
      "canonical_name": "碎裂玉佩",
      "variant_type": "damage",
      "description": "玉佩从中间裂成两半",
      "trigger_reason": "祠堂对峙时被击碎",
      "episode_numbers": [1],
      "source_evidence": [
        {
          "source_start": 3000,
          "source_end": 3060,
          "source_quote": null
        }
      ],
      "confidence": 0.96,
      "review_status": "ready",
      "content": {},
      "lock_version": 0,
      "created_at": "2026-07-29T10:00:00+08:00",
      "updated_at": "2026-07-29T10:00:00+08:00"
    }
  ],
  "warnings": []
}
```

-->

`characters`、`scenes`、`props` 使用相同的基础资产对象结构，但属于三个独立集合；
三类 `*_variants` 使用相同的变体对象结构，但只能关联本分类的基础资产。

后端仍将剧情事实、视觉设计、图像提示词辅助字段和原文证据保存在完整 `content` 中，但公开
响应只保证基础资产的 `content.source_facts`、`content.design_spec`，以及资产变体的
`content.visual_delta`、`content.preserve_anchors`、`content.state_scope`。用于校验的原文字符
偏移、完整证据对象和后端补齐的 Prompt 无需返回前端。

基础资产示例：

```json
{
  "source_facts": {
    "story_role": "调查玉佩失踪事件的主角"
  },
  "design_spec": {
    "identity_anchors": ["利落黑色短发", "窄长脸"],
    "design_rationale": "冷色利落造型服务悬疑调查剧情"
  }
}
```

基础资产和变体均返回顶层 `episode_numbers`。后端仍根据内部原文证据计算集数，并将标准资产
名称写入对应的 `episodes[].characters/scenes/props` 及变体列表，前端无需重复处理字符范围。

### 6.2 审核状态

| 状态           | 说明                                                              |
| -------------- | ----------------------------------------------------------------- |
| `ready`        | 可确认                                                            |
| `needs_review` | 低置信度提示；新流程统一确认时自动转为`ready`，也可提前编辑或拒绝 |
| `rejected`     | 用户明确拒绝，确认时不物化                                        |
| `materialized` | 基础资产已经写入内部项目资产库                                    |

若变体引用了一个未出现在基础资产列表中的名称，后端会自动补建低置信度基础候选，并将其
标记为 `needs_review`，不会静默丢弃变体。

### 6.3 变体类型

| 资产 | `variant_type`                                                              |
| ---- | --------------------------------------------------------------------------- |
| 角色 | `costume`、`makeup`、`age`、`injury`、`disguise`                            |
| 场景 | `time`、`weather`、`season`、`festival`、`damage`、`layout`、`state`        |
| 道具 | `form`、`damage`、`open_state`、`bloodied`、`upgrade`、`ownership`、`state` |

变体必须具备：

- 有效的 `base_candidate_id`；
- 非空描述和触发原因；
- 至少一个有效关联集数；
- 至少一段位于原剧本范围内的原文证据。

### 6.4 审核模式补充剧本

只有 `mode=supervised` 的审核模式可以补充剧本。补充内容固定追加到当前完整原文
末尾，不能插入或覆盖已有原文。第一次确认前补充会废弃当前草稿并对完整原文重新
分析；已经确认过第一步后补充，则进入增量分析。

粘贴补充内容：

```http
POST /api/v1/agent-productions/{production_id}/script-supplements/from-text
Content-Type: application/json
```

```json
{
  "content": "十年后，沈砚重新回到旧宅……"
}
```

上传补充文件：

```http
POST /api/v1/agent-productions/{production_id}/script-supplements/from-file
Content-Type: multipart/form-data
```

表单只包含必填字段 `file`，文件类型和大小限制与首次上传剧本一致。

成功响应中的 `data`：

```json
{
  "source_version": 2,
  "appended_character_count": 2150,
  "status": "planning",
  "current_stage": "source_analysis"
}
```

路径中已有 `production_id`；内部源文档 ID和步骤 ID不进入公开响应。

服务端会创建“原文 + 补充内容”的新版本。已确认项目的模型输入只包含本次
追加范围，不会重新提交整部原剧本。增量分集结果会追加到旧分集规划后，旧分集的
`plan_id`、章节 ID 和分镜状态保持不变。因此 `GET /script-package` 仍返回全部分集供
页面展示，但再次确认时只会物化新增分集，不会重建或重新提交旧分集。

新结果以已有标准名、别名和变体为合并依据：同一实体补充别名，同一资产的新时期或
状态写入变体，无法匹配时才新建基础资产候选。分析完成后生成新的故事圣经版本并
重新进入 `script_review`。用户确认新版资产后，只有新增且分镜状态为 `not_started`
的分集会自动入队。

自动模式返回 `40982`；当前分析状态不允许再次补充时返回 `40983`。补充后的完整剧本
超过服务端字符上限返回 `40050`。

## 7. 编辑分集

分集接口沿用现有路径，写操作成功后会递增 `script_version`。

### 7.1 编辑单集

```http
PATCH /api/v1/agent-productions/{production_id}/episode-plans/{plan_id}
```

```json
{
  "expected_version": 3,
  "title": "第一集：雨夜入宅",
  "content": "修订后的本集内容",
  "source_start": 0,
  "source_end": 3200,
  "characters": ["沈砚"],
  "character_variants": ["沈砚受伤造型"],
  "scenes": ["旧宅"],
  "scene_variants": ["雨夜旧宅"],
  "props": ["玉佩"],
  "prop_variants": ["碎裂玉佩"],
  "continuity_notes": ["受伤状态延续到第二集"],
  "position": 1
}
```

除 `expected_version` 外，只需提交要修改的字段。支持的修改字段：

| 字段                                                        | 约束                                                                                     |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `title`                                                     | 1～128 字                                                                                |
| `content`                                                   | 非空                                                                                     |
| `logline`                                                   | 字符串                                                                                   |
| `opening_hook`、`goal`、`conflict`、`climax`、`ending_hook` | 非空                                                                                     |
| `source_start`、`source_end`                                | 必须同时提交，且`0 <= source_start < source_end`                                         |
| `position`                                                  | 1～200，且不能超过当前剧集数量                                                           |
| 三类资产与变体列表                                          | `characters`、`character_variants`、`scenes`、`scene_variants`、`props`、`prop_variants` |
| `continuity_notes`                                          | 字符串数组                                                                               |

成功响应的公开字段是新 `version`、`status` 和精简后的 `items[]`。服务端可能暂时返回的
`production_id`、`step_id`、`checkpoint_id`、`planning_summary` 不属于前端契约。

### 7.2 合并相邻剧集

```http
POST /api/v1/agent-productions/{production_id}/episode-plans/merge
```

```json
{
  "expected_version": 3,
  "plan_ids": [
    "33333333-3333-3333-3333-333333333333",
    "88888888-8888-8888-8888-888888888888"
  ],
  "title": "雨夜旧宅",
  "content": "合并后的剧情内容"
}
```

只能合并两个相邻剧集。`plan_ids` 必须恰好包含两个不同 UUID。成功响应返回新版本的完整
分集规划文档。

### 7.3 拆分单集

```http
POST /api/v1/agent-productions/{production_id}/episode-plans/{plan_id}/split
```

```json
{
  "expected_version": 3,
  "split_at": 1680,
  "first_title": "雨夜入宅（上）",
  "second_title": "雨夜入宅（下）",
  "first_content": "拆分后的上集内容",
  "second_content": "拆分后的下集内容"
}
```

`split_at` 是整部原文的字符偏移，必须位于该集 `source_start` 和 `source_end` 之间。
`first_title`、`second_title`、`first_content`、`second_content` 均可省略；省略时后端根据
原分集标题、正文和拆分比例生成。
合并或拆分后，后端根据原文证据重新计算变体的 `episode_numbers` 和每集的变体列表。
成功响应返回新版本、状态和精简后的 `items[]`，不返回步骤或确认点内部 ID。

### 7.4 影响预览

```http
GET /api/v1/agent-productions/{production_id}/episode-plans/impact-preview
```

返回覆盖字符数、将创建章节数以及遗漏或重叠警告。

`data` 结构：

```json
{
  "version": 3,
  "chapter_count": 12,
  "source_covered_characters": 50231,
  "source_character_count": 50231,
  "warnings": []
}
```

`script-package.warnings` 是会阻断确认的资产结构警告，不能替代本接口的分集影响提示。
影响预览的 `warnings` 还可能包含“已有章节，将追加”等信息提示，目前没有独立
`severity` 字段；前端应展示给用户，但不能仅凭 `warnings.length > 0` 判断确认按钮是否
必须禁用。最终可确认性以后端确认接口校验为准。

## 8. 审核基础资产

```http
PATCH /api/v1/agent-productions/{production_id}/asset-candidates/{candidate_id}
```

请求：

```json
{
  "expected_lock_version": 0,
  "canonical_name": "沈砚",
  "aliases": ["阿砚"],
  "content": {
    "appearance": "黑发，灰色长风衣",
    "personality": "沉稳"
  },
  "review_status": "ready"
}
```

规则：

- `expected_lock_version` 必填；
- `review_status` 只能改为 `ready` 或 `rejected`；
- 已物化候选不能再次修改；
- `content` 按键浅合并，不会整体替换原对象；
- 修改标准名称后，后端重新计算各集的基础资产名称引用；
- 成功后候选 `lock_version` 和统一 `script_version` 都会递增；
- 成功响应只需要返回更新后对象的 `id`、名称、别名、`episode_numbers`、`review_status`、
  `content.source_facts`、`content.design_spec` 和 `lock_version`，不返回候选库外键、置信度、
  合并原因或时间戳；
- 响应不直接返回新的 `script_version`；
- 前端必须重新查询 `script-package`。

## 9. 审核资产变体

```http
PATCH /api/v1/agent-productions/{production_id}/asset-variants/{variant_id}
```

请求：

```json
{
  "expected_lock_version": 0,
  "canonical_name": "沈砚受伤造型",
  "variant_type": "injury",
  "description": "额角与衣领均有血迹",
  "trigger_reason": "遭到黑衣人袭击",
  "episode_numbers": [1, 2],
  "source_evidence": [
    {
      "source_start": 850,
      "source_end": 930,
      "source_quote": "黑衣人的刀锋擦过沈砚额角……"
    }
  ],
  "content": {
    "costume_continuity": "灰色长风衣保持不变"
  },
  "review_status": "ready"
}
```

只需提交要修改的字段。

规则：

- `expected_lock_version` 必填；
- 类型必须属于对应资产的允许列表；
- `episode_numbers` 去重并升序保存；
- 原文范围必须满足 `0 <= source_start < source_end <= 剧本字符数`；
- 仅更新 `source_evidence` 时，后端按证据重新计算 `episode_numbers`；同时提交
  `episode_numbers` 时，以显式集数为准；
- `content` 按键浅合并，不会整体替换原对象；
- `review_status` 只能改为 `ready` 或 `rejected`；
- 成功后变体 `lock_version` 和统一 `script_version` 都会递增；
- 成功响应只需要返回更新后变体的公开字段，不返回 `project_id`、`production_id`、
  `bible_version_id`、完整原文证据、置信度或时间戳；前端必须重新查询 `script-package`。

## 10. 统一确认

```http
POST /api/v1/agent-productions/{production_id}/script-package/confirm
```

请求：

```json
{
  "expected_script_version": 5,
  "expected_bible_version": 1,
  "idempotency_key": "script-confirm-22222222-v5"
}
```

确认前置条件：

1. 当前状态为 `waiting_approval / script_review`；
2. `expected_script_version` 和最新查询结果一致；
3. `expected_bible_version` 和最新查询结果一致；
4. 不存在未解决的结构警告；
5. 所有未拒绝变体都有合法类型、描述、触发原因、关联集数和原文证据；
6. 未拒绝变体所关联的基础资产不能是 `rejected`；
7. 分集内容和原文范围通过校验。

`needs_review` 仅作为低置信度提示，不阻止新流程确认；后端会在确认事务中自动转为 `ready`
并物化。用户明确标记为 `rejected` 的资产不会进入第二步。

成功响应：

```json
{
  "script_version": 5,
  "bible_version": 1,
  "already_confirmed": false
}
```

正式章节 ID、物化资产 ID及创建/复用计数只用于服务端审计。前端确认成功后直接进入第二步，
通过 `GET /core-assets` 获取正式资产 ID，不依赖第一步确认响应中的内部物化结果。

确认在一个数据库事务内完成：

- 创建或复用正式剧集；
- 创建或复用未拒绝的基础资产；
- 写入来源章节关联；
- 确认资产版本；
- 完成 `source_analysis` 步骤；
- 状态跳转至 `planning / core_assets`。

本接口不会生成参考图。
资产变体在第一步仍是结构化文本，不会作为独立项目资产物化。

确认成功进入 `core_assets` 后，第二步调用：

```http
POST /api/v1/agent-productions/{production_id}/core-assets/image-generations
```

请求中的 `items[]` 使用第二步 `GET /core-assets` 返回的稳定资产 ID，并明确 `asset_type`。后端固定
使用 `gpt-image-2` 和 `16:9`：人物为白底左侧肖像、右侧正/侧/背三视图；场景为单幅场景图；
道具为白底左侧完整图、右侧细节角度。资产变体继续依附基础资产，但在第二步拥有独立
`reference_image`。变体可以直接手动上传图片；只有调用模型生成接口时才必须先完成基础资产
主图。模型生成会强制引用基础资产主图，生成结果不会覆盖主图。

### 10.1 幂等规则

- 同一确认版本使用相同 `idempotency_key` 重试，返回 `already_confirmed=true`；
- 已确认后使用不同 `idempotency_key`，返回 `40948`；
- 并发确认由生产实例行锁串行化，不会重复创建章节或资产。

### 10.2 旧确认接口

`workflow_version >= 2` 禁止使用以下旧接口完成第一步：

```http
POST /api/v1/agent-productions/{production_id}/episode-plans/confirm
POST /api/v1/agent-productions/{production_id}/story-bible/confirm
```

调用时返回 `40947`。前端必须使用 `/script-package/confirm`。

## 11. 业务错误码

| HTTP | 业务码            | 场景                                                   |
| ---- | ----------------- | ------------------------------------------------------ |
| 400  | `40003`           | 当前积分不足，文本任务未提交                           |
| 400  | `40007` / `40008` | 上传层判定文件为空或无法确定文件类型                   |
| 400  | `40049`           | 粘贴正文或待分析原文为空                               |
| 400  | `40050`           | 抽取后的正文超过字符上限                               |
| 400  | `40053`           | 文件扩展名不是 txt、md、docx、pdf                      |
| 400  | `40054`           | 上传文件为空；或分集`position` 超出数量                |
| 400  | `40055`           | 文件未抽取到正文；或尝试合并非相邻分集                 |
| 400  | `40056`           | 文本编码无法识别；或`split_at` 不在分集原文范围内      |
| 400  | `40057`           | DOCX 损坏、过大、条目过多或压缩比例异常                 |
| 400  | `40058`           | PDF 加密/损坏；或分集原文范围越界                      |
| 400  | `40059`           | PDF 超过 500 页；或分集内容为空                        |
| 400  | `40060`           | 文件名过长；或分集/变体关联范围不合法                  |
| 400  | `40061`           | 资产变体类型无效                                       |
| 400  | `40062`           | 变体原文证据超出剧本范围                               |
| 400  | `40064`           | PDF/DOCX 隔离解析进程异常终止                          |
| 408  | `40801`           | PDF/DOCX 解析超过服务端时间限制                        |
| 413  | `41300` / `41301` | 上传层或剧本解析层判定文件超过大小上限                 |
| 404  | `40403`           | 创建时选择的风格不存在或未启用                         |
| 404  | `40430`           | Agent 项目不存在或不属于当前用户                       |
| 404  | `40431`           | Agent 项目关联的剧本原文不存在                         |
| 404  | `40432`           | 分集规划不存在                                         |
| 404  | `40433`           | 基础资产候选不存在                                     |
| 404  | `40440`           | 资产变体不存在                                         |
| 409  | `40911`           | 当前状态不允许执行启动、暂停、恢复或取消操作           |
| 409  | `40915`～`40917`  | 分集规划、确认点尚未生成或规划内容为空                 |
| 409  | `40918` / `40919` | 当前状态不允许编辑分集或分集已经确认                   |
| 409  | `40920`           | 分集/剧本版本冲突                                      |
| 409  | `40925`           | 基础资产已经物化，不能修改                             |
| 409  | `40931`           | 基础资产`lock_version` 冲突                            |
| 409  | `40940`           | 资产变体`lock_version` 冲突                            |
| 409  | `40941`           | `script_version` 冲突                                  |
| 409  | `40942`           | `bible_version` 冲突                                   |
| 409  | `40943`           | 存在结构不完整、孤立或原文证据无效的资产变体           |
| 409  | `40944`           | 剧本资产尚未生成                                       |
| 409  | `40945`           | 当前任务不是统一剧本处理流程                           |
| 409  | `40946`           | 当前状态不允许修改或确认                               |
| 409  | `40947`           | 新流程错误调用旧确认接口                               |
| 409  | `40948`           | 已确认版本使用了不同幂等键                             |
| 409  | `40949`           | 存在未解决的结构警告                                   |
| 409  | `40982`           | 自动模式不支持人工补充剧本                             |
| 409  | `40983`           | 当前不在第一步待审核阶段，或第一步已经确认             |
| 409  | `40980`           | 固定文本或图像模型缺失                                 |
| 422  | `42200`           | 请求字段校验失败                                       |
| 502  | `50241`           | 模型未返回合法 JSON 对象                               |
| 502  | `50242`           | 分块分析结果不符合契约                                 |
| 502  | `50244`           | 分集分析结果缺字段或原文范围不连续                     |
| 502  | `50245`           | 资产分析缺少六个分类数组、资产名称、原文证据或有效变体 |
| 500  | `50000`           | 未处理的服务器错误                                     |

版本冲突处理：

1. 不要自动覆盖；
2. 重新查询 `script-package`；
3. 用最新数据刷新编辑页；
4. 用户确认后重新提交。

## 12. 前端页面数据映射

| 页面区域              | 数据字段                          | 写接口                                    |
| --------------------- | --------------------------------- | ----------------------------------------- |
| 分集列表              | `episodes`                        | episode plan 编辑/合并/拆分               |
| 人物资产（基础）      | `characters`                      | asset candidate PATCH                     |
| 人物资产（变装/状态） | `character_variants`              | asset variant PATCH                       |
| 场景资产（基础）      | `scenes`                          | asset candidate PATCH                     |
| 场景资产（变体）      | `scene_variants`                  | asset variant PATCH                       |
| 道具资产（基础）      | `props`                           | asset candidate PATCH                     |
| 道具资产（变体）      | `prop_variants`                   | asset variant PATCH                       |
| 阻断提示              | `warnings` 或结构无效的资产变体   | 修正后重新查询；`needs_review` 本身不阻断 |
| 底部确认栏            | `script_version`、`bible_version` | script package confirm                    |

前端可以直接建立三个页签，不需要根据名称或 `content` 猜测资产类型：

```ts
const assetGroups = [
  {
    key: "character",
    label: "人物资产",
    assets: data.characters,
    variants: data.character_variants,
  },
  {
    key: "scene",
    label: "场景资产",
    assets: data.scenes,
    variants: data.scene_variants,
  },
  {
    key: "prop",
    label: "道具资产",
    assets: data.props,
    variants: data.prop_variants,
  },
] as const;
```

每个页签先按 `asset.id` 建立基础资产索引，再用
`variant.base_candidate_id` 将变体挂载到对应基础资产下。资产分类以所在顶层分组为准，前端
不依赖对象内重复的 `asset_type`。

确认按钮建议仅在以下条件全部满足时启用：

```text
status == "waiting_review"
warnings.length == 0
所有未拒绝变体的 base_candidate_id 指向未拒绝的基础资产
```

后端仍会执行最终校验，前端判断不能替代服务端校验。
