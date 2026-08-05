# Agent 审核模式接口文档

| 项目     | 内容                                   |
| -------- | -------------------------------------- |
| 文档版本 | 1.3                                    |
| 更新日期 | 2026-08-04                             |
| API 前缀 | `/api/v1`                              |
| 适用模式 | `mode=supervised`                      |
| 鉴权     | `Authorization: Bearer <access_token>` |

本文档是 Agent 审核模式的主接口契约。其他 Agent 文档与本文冲突时，以本文和当前后端代码为准。

## 1. 四步流程和门禁

| 步骤 | `step_code`             | 名称     | 开放条件             | 完成条件                               |
| ---- | ----------------------- | -------- | -------------------- | -------------------------------------- |
| 1    | `script_processing`     | 剧本处理 | 创建后立即开放       | 分集、资产和变体分析已确认落库         |
| 2    | `asset_confirmation`    | 资产确认 | 首次剧本结果可审核   | 当前有效资产已确认；参考图不是前置条件 |
| 3    | `storyboard_generation` | 分镜制作 | 存在有效资产确认版本 | 按集并发分析，每集独立达到`ready`      |
| 4    | `video_editing`         | 视频审核 | 至少一集分镜分析完成 | 目标集审片通过并交付                   |

必须遵守以下规则：

1. `current_step` 只是当前建议处理的位置，不是访问权限。
2. 页面能否进入只认 `steps[].can_view`，不得用 `step_number <= current_step` 推导。
3. 首次确认后补充剧本会重新运行第一步，但只分析追加原文；已开放页面和历史分集状态保持不变。
4. 第二步确认资产结构，不要求主资产或变体已有参考图；生图任务执行中也能确认。
5. 第二步确认后仍能上传或生成参考图。模型生成变体图必须先有主资产图，手动上传不受此限。
6. 第三步按集并发。未分析完成的集不能查看或修改；完成几集就返回几集。
7. 第三、四步按集并行：任一集分镜完成，该集立即进入第四步；无视频的分镜组也按顺序展示。
8. 无视频只阻止单集确认和交付，不阻止查看时间线，也不影响第三步继续生成。
9. 视频模型只在第三步生成视频时选择；文本和图像模型使用服务端默认配置。

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

- HTTP `422`：请求字段校验失败。
- HTTP `409`：步骤门禁、版本冲突、幂等冲突或业务状态冲突。
- `expected_*_version` 必须使用最近查询值；冲突后重新查询，不要复用旧版本盲重试。
- `idempotency_key` 为 8～128 字符；同一动作重试复用相同值，不同内容不得复用。
- 只在响应 `should_poll=true` 时按 `next_poll_seconds` 轮询；不要因存在历史运行项目固定每 5 秒刷新列表。

## 3. 项目入口

### 3.1 接口清单

| 方法    | 路径                                               | 用途                          |
| ------- | -------------------------------------------------- | ----------------------------- |
| POST    | `/agent-productions/from-text`                     | 粘贴剧本并创建独立 Agent 项目 |
| POST    | `/agent-productions/from-file`                     | 上传剧本并创建独立 Agent 项目 |
| GET     | `/agent-productions`                               | 项目列表                      |
| GET     | `/agent-productions/{production_id}`               | 项目详情                      |
| DELETE  | `/agent-productions/{production_id}`               | 软删除项目                    |
| GET/PUT | `/agent-productions/{production_id}/configuration` | 查询/修改启动前整体配置       |
| POST    | `/agent-productions/{production_id}/start`         | 开始剧本分析                  |
| POST    | `/agent-productions/{production_id}/pause`         | 暂停后台任务                  |
| POST    | `/agent-productions/{production_id}/resume`        | 恢复后台任务                  |
| POST    | `/agent-productions/{production_id}/cancel`        | 取消项目                      |
| GET     | `/agent-productions/{production_id}/workbench`     | 工作台汇总和下一动作          |
| GET     | `/agent-productions/{production_id}/workflow`      | 四步及逐集状态                |

### 3.2 粘贴剧本

```http
POST /api/v1/agent-productions/from-text
Content-Type: application/json
```

```json
{
  "name": "雨夜旧宅",
  "content": "完整剧本文本……",
  "style_id": "11111111-1111-1111-1111-111111111111",
  "generation_ratio": "9:16",
  "video_resolution": "1080p",
  "mode": "supervised"
}
```

`name` 可省略。创建请求只控制风格、画面比例、视频分辨率和运行模式，不接受集数、单集时长、分镜时长、模型 ID、重试次数、试播集数或积分预算。

`generation_ratio`：`21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16`。

`video_resolution`：`480p`、`720p`、`1080p`、`4k`。

### 3.3 上传文件

```http
POST /api/v1/agent-productions/from-file
Content-Type: multipart/form-data
```

| 字段               | 必填 | 说明                                                   |
| ------------------ | ---- | ------------------------------------------------------ |
| `file`             | 是   | `.txt`、`.md`、`.docx`、`.pdf`；文件名和扩展名不能为空 |
| `name`             | 否   | Agent 项目名                                           |
| `style_id`         | 是   | 启用的风格 ID                                          |
| `generation_ratio` | 是   | 画面比例                                               |
| `video_resolution` | 是   | 视频分辨率                                             |
| `mode`             | 是   | 固定为`supervised`                                     |

上传或粘贴会自动创建 `project_kind=agent` 的独立项目，不使用项目中心已有普通项目。

### 3.4 工作流查询

```http
GET /api/v1/agent-productions/{production_id}/workflow
```

`data` 返回：

```json
{
  "production_id": "...",
  "mode": "supervised",
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
    },
    {
      "step_number": 4,
      "step_code": "video_editing",
      "title": "视频制作",
      "status": "waiting_review",
      "completed": false,
      "can_view": true,
      "is_current": false,
      "started_at": "2026-08-04T10:02:00+08:00",
      "completed_at": null,
      "extra": {}
    }
  ],
  "episodes": [
    {
      "chapter_id": "...",
      "episode_number": 1,
      "title": "第一集",
      "steps": []
    }
  ]
}
```

实际 `steps` 和 `episodes[].steps` 都固定四项。状态枚举：`not_started`、`processing`、`waiting_review`、`completed`、`failed`、`invalidated`。第四步项目级开放后，仍须用对应集第四项的 `can_view` 判断该集能否审片。

## 4. 第一步：剧本处理

第一步对前端是一项任务，后端内部是两个顺序模型阶段：先由模型按剧情自主确定集数并完整覆盖原文，再按分集分析人物、场景、道具和各类变体。本步骤不生图，也不要求用户设置集数或时长。

### 4.1 接口

| 方法  | 路径                                                              | 用途                 |
| ----- | ----------------------------------------------------------------- | -------------------- |
| GET   | `/agent-productions/{production_id}/script-package`               | 查询分集和资产分析包 |
| PATCH | `/agent-productions/{production_id}/asset-variants/{variant_id}`  | 编辑资产变体         |
| POST  | `/agent-productions/{production_id}/script-package/confirm`       | 一次确认分集和资产   |
| POST  | `/agent-productions/{production_id}/script-supplements/from-text` | 追加文本             |
| POST  | `/agent-productions/{production_id}/script-supplements/from-file` | 追加文件             |

`GET script-package` 核心结构：

```json
{
  "production_id": "...",
  "script_version": 2,
  "bible_version": 2,
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

人物、场景、道具必须分组响应。变体通过 `base_candidate_id` 关联基础资产，并包含变体类型、触发原因、关联集数和原文证据。

### 4.2 确认

```http
POST /api/v1/agent-productions/{production_id}/script-package/confirm
```

```json
{
  "expected_script_version": 2,
  "expected_bible_version": 2,
  "idempotency_key": "script-confirm-v2"
}
```

成功返回 `chapter_ids`、`asset_ids`、新建/复用数量和 `already_confirmed`，并开放第二步。

### 4.3 补充剧本

文本请求：

```http
POST /api/v1/agent-productions/{production_id}/script-supplements/from-text
Content-Type: application/json
```

```json
{ "content": "追加到原文末尾的新剧情……" }
```

文件请求：

```http
POST /api/v1/agent-productions/{production_id}/script-supplements/from-file
Content-Type: multipart/form-data
```

表单字段只包含 `file`，文件类型、大小和字符数限制与首次上传一致。

成功响应的 `data`：

```json
{
  "production_id": "...",
  "source_document_id": "...",
  "source_version": 2,
  "step_id": "...",
  "appended_character_count": 2150,
  "status": "planning",
  "current_stage": "source_analysis"
}
```

- 只允许审核模式；暂停、取消或尚未开始时不可补充。
- 首次确认前补充会废弃当前草稿分析，生成新草稿。
- 首次确认后只分析本次追加原文，不向模型重新提交整部剧本。
- 增量结果会与历史分集合并后展示；未修改的旧分集继续复用原 `plan_id`、章节 ID 和分镜状态。
- 如果用户在确认前编辑、合并或拆分分集，确认时会同步实际章节；受影响章节的旧分镜状态置为 `invalidated`，合并后不再存在的章节会停用。
- 再次确认 `script-package` 时只新建或更新本次受影响章节；确认资产后也只将这些章节提交分镜分析。
- 历史确认结果和已开放页面保留，旧资产仍能操作。
- 同一资产的别名合并到基础资产；不同时期、服装、受伤、天气或损坏等合并为变体；真正的新资产才新建。
- 增量结果完成后仍须重新查询并确认 `script-package`。

### 4.4 补充剧本的查询、确认和提交范围

补充分析完成后，前端仍调用原有接口，不增加新请求字段。“页面展示范围”和
“后端自动提交范围”必须分开理解：

| 操作                           | 结果范围                           | 说明                                                 |
| ------------------------------ | ---------------------------------- | ---------------------------------------------------- |
| `GET /script-package`          | 历史分集 + 本次新增分集            | 前端始终得到可展示的完整分集列表                     |
| `POST /script-package/confirm` | 确认完整分集包，同步新增或修改章节 | 未修改章节按`plan_id` 或原文范围复用；已移除章节停用 |
| `POST /core-assets/confirm`    | 只自动提交本次新增或修改章节       | 范围外旧分集即使也是`not_started` 也不会被带入       |
| `GET /storyboards`             | 按现有逐集状态返回                 | 新增集独立进入`pending/running/success/failed`       |

`POST /script-package/confirm` 的 `chapter_ids` 是当前完整剧本对应的章节 ID 列表，
不等于本次将提交分镜的列表。前端应使用 `created_chapter_count` 判断本次新建数量，
使用 `GET /storyboards` 的逐集状态展示分镜进度，不得根据 `chapter_ids` 自行推导提交范围。

服务端在剧本包确认时累计记录本次新增或修改章节，在下一次资产确认成功后仅用该范围调度分镜。
生产记录上的待提交标记随后清除，但范围会持久化到批量分镜步骤，直到该范围全部成功或失败，
因此控制器多轮派发不会误带入历史 `not_started` 分集。修复前已经确认的增量项目，后端会根据最新增量分析步骤
和章节的 `agent_step_id` 恢复新增范围。用户显式调用 `/storyboards/generations` 则表示重新发起全部待处理分集，
会解除本次增量范围限制。

## 5. 第二步：资产确认

`asset_type` 固定为 `character`、`scene`、`prop`。

### 5.1 接口

| 方法             | 路径                                                                                                            | 用途                   |
| ---------------- | --------------------------------------------------------------------------------------------------------------- | ---------------------- |
| GET/POST         | `/agent-productions/{production_id}/core-assets`                                                                | 查询/新增资产          |
| GET/PATCH/DELETE | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}`                                        | 详情/编辑/删除         |
| POST             | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants`                               | 新增变体               |
| DELETE           | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}`                  | 删除变体               |
| PUT              | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/reference-image`                        | 采用或清空主图         |
| POST             | `/agent-productions/{production_id}/core-assets/image-generations`                                              | 批量生成主图           |
| PUT              | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/reference-image`  | 采用或清空变体图       |
| POST             | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/image-generation` | 参考主图生成变体图     |
| GET              | `/agent-productions/{production_id}/core-assets/readiness`                                                      | 确认准备度             |
| POST             | `/agent-productions/{production_id}/core-assets/confirm`                                                        | 自动选择有效资产并确认 |
| POST             | `/agent-productions/{production_id}/core-assets/impact-preview`                                                 | 指定集合的影响预览     |
| POST             | `/agent-productions/{production_id}/core-assets/lock`                                                           | 更新指定资产确认版本   |

列表查询参数：`asset_type`、`keyword`、`page`、`page_size`。

### 5.2 新建、编辑和变体

```json
{
  "asset_type": "character",
  "canonical_name": "沈砚",
  "aliases": ["阿砚"],
  "content": { "identity": "调查员", "appearance": "黑发灰眼" }
}
```

编辑必须带 `expected_lock_version`。结构增删改只允许在当前资产确认阶段且尚未形成有效锁时操作；确认后仍可修改参考图。

变体请求：

```json
{
  "canonical_name": "沈砚受伤造型",
  "variant_type": "injury",
  "description": "额角伤口与血迹",
  "trigger_reason": "第一集遭到袭击",
  "episode_numbers": [1],
  "source_evidence": [
    {
      "source_start": 120,
      "source_end": 138,
      "source_quote": "沈砚额角渗出鲜血"
    }
  ]
}
```

### 5.3 参考图

采用图片 URL 或清空：

```json
{
  "expected_lock_version": 2,
  "reference_image": "https://oss.example.com/agent/shenyan.png"
}
```

`reference_image=null` 表示清空；文件先通过平台通用上传接口上传，再把 URL 传给本接口。

批量生图：

```json
{
  "items": [
    {
      "asset_type": "character",
      "asset_id": "...",
      "generation_mode": "general",
      "prompt": "保持人物身份特征"
    }
  ]
}
```

新流程使用服务端默认图像模型。默认构图：人物为 16:9 白底、左侧肖像、右侧三视图；场景为 16:9 场景图；道具为 16:9 白底、左侧完整图、右侧细节角度。

### 5.4 确认

```json
{
  "expected_lock_version": 0,
  "idempotency_key": "core-confirm-v1"
}
```

`POST /core-assets/confirm` 自动选择全部有效资产和变体。参考图可为空，生图任务正在执行也不会阻止确认。成功返回 `lock_id`、`version`、`assets` 快照、`impact` 和 `already_locked`。

`/confirm` 是审核页面的一键确认入口：即使已有资产锁且资产图片或集合发生变化，也不要求前端提交
`impact_fingerprint`，服务端会基于当前完整资产快照生成新版本。需要由前端指定资产集合时才使用
`impact-preview` + `/lock`；`/lock` 在已有锁时必须提交最新 `impact_fingerprint`。
补充剧本未产生任何资产快照变化时，`/confirm` 会复用当前锁，但仍会完成第二步并提交本次受影响分集，
不会停留在 `core_assets`。

首次确认资产时，将所有可调度分集并发提交第三步；补充剧本后再次确认资产时，
只提交本次新增或修改分集。该限制由后端执行，前端不需要也不应传入分集 ID。

需要明确指定改变后的资产集合时，先调用 `impact-preview`，再携带 `impact_fingerprint` 调用 `/lock`。

## 6. 第三步：分镜制作和视频生成

### 6.1 分镜规则

- 分集并发分析，不按集串行。
- 每集独立维护 `status`、`task_record_id` 和 `revision`。
- 分镜组按剧情顺序保存，时长 4～15 秒；超过 15 秒时模型必须主动拆组，不强制截断。
- 长对话增加周围人物反应、动作或环境镜头；4 个汉字约 1 秒，4 个英文单词约 1 秒。
- 人物、场景、道具必须绑定到确认资产或变体。
- 固定提示词必须包含画风、不得出现字幕/文字叠加、纯画面、不要 BGM、不要配乐。

### 6.2 接口

| 方法   | 路径                                                                                                     | 用途                   |
| ------ | -------------------------------------------------------------------------------------------------------- | ---------------------- |
| GET    | `/agent-productions/{production_id}/storyboards`                                                         | 逐集进度和已完成结果   |
| POST   | `/agent-productions/{production_id}/storyboards/generations`                                             | 并发提交未完成/失败集  |
| POST   | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards`                                   | 新增分镜组             |
| PATCH  | `/agent-productions/{production_id}/storyboards/{storyboard_id}`                                         | 自动保存分镜修改       |
| POST   | `/agent-productions/{production_id}/storyboards/{storyboard_id}/copy`                                    | 复制分镜组             |
| DELETE | `/agent-productions/{production_id}/storyboards/{storyboard_id}`                                         | 删除分镜组             |
| PUT    | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/order`                             | 调整本集顺序           |
| GET    | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/asset-options`     | 绑定资产和变体选项     |
| PUT    | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-config`      | 视频模型、分辨率和时长 |
| GET    | `/agent-productions/{production_id}/episodes/{chapter_id}/videos`                                        | 本集视频状态           |
| POST   | `/agent-productions/{production_id}/episodes/{chapter_id}/video-generations`                             | 生成本集视频           |
| POST   | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-generations` | 生成单组视频           |
| GET    | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-versions`    | 单组视频版本           |
| PUT    | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/primary-video`     | 选择主要视频           |

`POST /storyboards/generations` 是用户显式重新发起入口，会处理全部待处理分集；补充剧本确认后的自动调用则由后端限制为本次新增或修改分集。

`GET /storyboards` 返回 `episode_count`、`completed_episode_count`、`remaining_episode_count`、`analysis_complete`、`episode_analyses`、`failed_episodes`、`active_task_count` 和 `episodes`。前端边分析边显示已完成集；具体集未完成时返回 `40968`。

### 6.3 修改分镜和绑定

```json
{
  "expected_core_asset_lock_version": 1,
  "expected_revision": 3,
  "storyboard_prompt": "画面风格：写实漫剧风格。视频中不得出现任何字幕、文字叠加、纯画面，不要BGM，不要配乐。镜头1：{{asset:character_1}}进入{{asset:scene_1}}。",
  "estimated_duration_seconds": 8,
  "asset_bindings": [
    {
      "binding_key": "character_1",
      "asset_type": "character",
      "asset_id": "...",
      "variant_id": "..."
    }
  ]
}
```

可修改 `title`、`source_content`、`shots`、`prompt_notes`、`storyboard_prompt`、`estimated_duration_seconds`、`asset_bindings`。`storyboard_prompt` 与 `shots` 不能同时提交。替换资产或变体后，后端联动重建有效提示词；使用响应的新 `revision` 和 `effective_prompt`。

#### 6.3.1 `@` 资产图片引用

分镜项同时返回：

- `storyboard_prompt` / `effective_prompt`：已经渲染资产名称的展示文本；
- `prompt_template`：用于编辑和保存的稳定模板，其中资产引用格式为 `{{asset:binding_key}}`。

前端输入 `@` 时调用本分镜的 `asset-options`，只展示 `bound=true` 的项目：

```json
{
  "asset_type": "character",
  "asset_id": "...",
  "bound": true,
  "binding_key": "character_1",
  "selected_variant_id": "...",
  "mention_text": "@沈砚夜行装",
  "mention_reference_image": "https://oss.example.com/shenyan-night.png",
  "mention_enabled": true
}
```

`mention_text` 用于编辑器显示；选中后必须把不可拆分的 mention 节点序列化为
`{{asset:character_1}}`，再放入现有 `storyboard_prompt` 字段提交。稳定关联只认 token，前端不能把
普通键盘输入的 `@沈砚` 当作关联值。为兼容旧提示词，后端可能把与绑定标签一致的普通资产名称
规范化为 token，但前端不能依赖名称匹配。`mention_enabled=false` 表示当前绑定没有可用参考图，
可以显示但应禁止选择。

提交完整 `storyboard_prompt` 时，后端只接受当前分镜 `asset_bindings` 中存在的 `binding_key`。
提示词引用已删除、未绑定或不属于当前分镜的 key 时返回 HTTP `409` / 业务码 `40962`，并在
`data.invalid_binding_keys` 返回无效 key。切换同一绑定的资产变体时保持 `binding_key` 不变，
原提示词引用会自动使用新变体名称和图片。

### 6.4 视频配置、生成和选片

单组配置：

```json
{
  "expected_core_asset_lock_version": 1,
  "expected_storyboard_revision": 3,
  "expected_config_version": 0,
  "video_model_id": "...",
  "video_resolution": "1080p",
  "estimated_duration_seconds": 8
}
```

整集生成：

```json
{
  "expected_core_asset_lock_version": 1,
  "expected_episode_revision": 4,
  "idempotency_key": "episode-1-video-v1",
  "video_model_id": "...",
  "video_resolution": "1080p"
}
```

单组生成必须同时提交本次实际参数：

```json
{
  "expected_core_asset_lock_version": 1,
  "expected_episode_revision": 4,
  "expected_storyboard_revision": 3,
  "expected_video_config_version": 1,
  "duration_seconds": 8,
  "idempotency_key": "storyboard-video-v1",
  "video_model_id": "...",
  "video_resolution": "1080p",
  "prompt": "镜头推进稍慢"
}
```

`expected_video_config_version` 使用分镜组 `video_config.version`，未保存配置时传 `0`；
`duration_seconds` 范围为 4～15 秒。`prompt` 只是本次补充要求，不覆盖保存的分镜提示词。
视频只能按集或单组生成，不能跨集全局生成。

生成前，后端按照 `prompt_template` 中资产首次出现的顺序加载人物、场景、道具及选中变体的
参考图，生成有序 `reference_manifest`，并把分镜提示词编译为 `@图片1`、`@图片2` 等多模态引用。
提示词引用编号与实际图片附件顺序严格一致；选择变体时只提交变体图。绑定资产或变体缺图、
或参考图数量超过所选模型上限时不提交任务，不会静默丢图。人物/道具白底拼版只用于外观
一致性参考，提示词会禁止模型复刻白底、三视图和细节展示布局。

存在 `{{asset:*}}` 引用时，只提交实际被引用的图片；同一资产重复引用只提交一张图片。
没有任何 token 的历史分镜继续提交全部绑定图片，保证旧数据兼容。

一个分镜组可以有多个视频版本；`selection_required=true` 时调用主视频选择接口：

```json
{
  "expected_selection_revision": 1,
  "history_id": "...",
  "result_url": "https://oss.example.com/video.mp4"
}
```

## 7. 第四步：视频审核和交付

任一集分镜完成后，项目第四步 `can_view=true`。`GET /review` 中：

- `storyboard_analysis_status=ready`、`can_review=true`：允许本集审片；
- 未完成集 `can_review=false`：只显示状态；
- 完成集按 `group_number` 返回所有分镜组；
- 无视频项返回 `video_status=not_started`、`video_url=null`、`video_history_id=null`。

### 7.1 接口

| 方法     | 路径                                                                      | 用途              |
| -------- | ------------------------------------------------------------------------- | ----------------- |
| GET      | `/agent-productions/{production_id}/review`                               | 全剧审片摘要      |
| GET      | `/agent-productions/{production_id}/episodes/{chapter_id}/video-timeline` | 本集顺序时间线    |
| POST     | `/agent-productions/{production_id}/review-issues`                        | 记录问题          |
| PATCH    | `/agent-productions/{production_id}/review-issues/{issue_id}`             | 解决/忽略问题     |
| POST     | `/agent-productions/{production_id}/episodes/{chapter_id}/approve`        | 确认本集          |
| GET      | `/agent-productions/{production_id}/delivery-readiness`                   | 交付准备度        |
| POST/GET | `/agent-productions/{production_id}/deliveries`                           | 创建/查询交付     |
| GET      | `/agent-productions/{production_id}/deliveries/{delivery_id}`             | 交付详情          |
| POST/GET | `/agent-productions/{production_id}/jianying-exports`                     | 创建/查询剪映导出 |
| GET      | `/agent-productions/{production_id}/jianying-exports/{export_id}`         | 剪映导出详情      |

### 7.2 时间线

```json
{
  "production_id": "...",
  "chapter_id": "...",
  "episode_number": 1,
  "title": "第一集",
  "review_status": "pending",
  "review_lock_version": 0,
  "ready_to_approve": false,
  "estimated_duration_seconds": 15,
  "items": [
    {
      "storyboard_id": "...",
      "group_number": 1,
      "title": "雨夜入宅",
      "estimated_duration_seconds": 7,
      "video_status": "not_started",
      "video_url": null,
      "video_history_id": null
    }
  ]
}
```

第四步只读取第三步选定的主要视频，不暂停、取消或改变第三步任务。

### 7.3 问题和单集确认

```json
{
  "chapter_id": "...",
  "storyboard_id": "...",
  "media_type": "video",
  "category": "motion",
  "severity": "blocking",
  "description": "人物手部动作异常"
}
```

`category`：`character_consistency`、`style_drift`、`motion`、`rhythm`、`dialogue`、`audio`、`compliance`、`other`。

解决问题：

```json
{
  "expected_lock_version": 0,
  "status": "resolved",
  "resolution_note": "已重新生成并检查"
}
```

确认本集：

```json
{
  "expected_lock_version": 0,
  "idempotency_key": "approve-episode-1-v1"
}
```

本集所有分镜必须有已选择的有效视频，且没有未解决的 `blocking` 问题。

### 7.4 交付和剪映草稿

```json
{
  "delivery_type": "manifest",
  "chapter_ids": [],
  "idempotency_key": "delivery-manifest-v1"
}
```

`delivery_type`：`manifest`、`merged_video`、`jianying_draft`；`chapter_ids=[]` 表示全部有效集。

剪映专用导出：

```json
{
  "platform": "windows",
  "jianying_version": "10.8",
  "draft_name": "雨夜旧宅",
  "chapter_ids": [],
  "idempotency_key": "jianying-windows-v1"
}
```

`platform` 支持 `windows`、`macos`。服务端按分镜顺序下载主要视频并打包剪映专业版草稿 ZIP。轮询导出详情至 `status=completed`，再使用 `output_url` 下载。

## 8. 运维和异常接口

| 方法 | 路径                                            | 用途                   |
| ---- | ----------------------------------------------- | ---------------------- |
| GET  | `/agent-productions/{production_id}/matrix`     | 生产矩阵               |
| GET  | `/agent-productions/{production_id}/exceptions` | 内容审核、供应商等异常 |
| GET  | `/agent-productions/{production_id}/events`     | Agent 事件             |
| GET  | `/agent-productions/{production_id}/costs`      | 实际任务积分明细       |
| POST | `/agent-productions/{production_id}/jobs/retry` | 用户明确重试失败任务   |
| POST | `/agent-productions/{production_id}/jobs/skip`  | 跳过任务或提供替代结果 |

创建项目不填写失败重试次数；这里是失败后的显式处理接口。

## 9. 关键错误码

| 业务码  | HTTP | 含义                     | 处理                           |
| ------- | ---- | ------------------------ | ------------------------------ |
| `40430` | 404  | 项目不存在或已删除       | 返回列表                       |
| `40434` | 404  | 资产不存在               | 刷新资产                       |
| `40440` | 404  | 审片剧集不存在           | 刷新工作流                     |
| `40441` | 404  | 审片分镜不存在           | 刷新分镜                       |
| `40057` | 400  | DOCX 损坏或压缩结构异常  | 重新导出 DOCX                  |
| `40064` | 400  | PDF/DOCX 解析进程异常    | 重新导出文件后上传             |
| `40801` | 408  | PDF/DOCX 解析超时        | 拆分或重新导出文件             |
| `40932` | 409  | 状态不允许修改资产       | 刷新项目状态                   |
| `40933` | 409  | 资产锁版本冲突           | 重新查询资产                   |
| `40935` | 409  | 影响预览已变化           | 重新预览                       |
| `40941` | 409  | 剧本版本冲突             | 重新查询剧本包                 |
| `40962` | 409  | 分镜提示词引用未绑定资产 | 刷新资产选项并移除无效 mention |
| `40968` | 409  | 本集分镜未分析完成       | 按任务提示轮询                 |
| `40970` | 409  | 审片问题版本冲突         | 重新查询审片                   |
| `40972` | 409  | 仍有未就绪视频           | 回第三步处理                   |
| `40973` | 409  | 仍有阻断问题             | 先解决问题                     |
| `40982` | 409  | 自动模式不能人工补充     | 仅审核模式显示入口             |
| `40983` | 409  | 当前状态不能补充         | 刷新详情                       |
| `40984` | 409  | 变体缺少主资产图         | 先设置主图                     |
| `40990` | 409  | 步骤尚未开放             | 按`can_view` 禁用              |

## 10. 推荐调用顺序

```text
上传或粘贴剧本
  → POST /start
  → GET /workbench 或 /workflow
  → GET /script-package
  → POST /script-package/confirm
  → GET /core-assets
  → 可选上传/生成主资产与变体图片
  → POST /core-assets/confirm
  → GET /storyboards，按集并发展示
  → 对 ready 集编辑分镜、绑定资产、配置并生成视频
  → 任一集 ready 后并行进入 /review 和该集 /video-timeline
  → 逐集选择主视频、处理问题并 approve
  → GET /delivery-readiness
  → POST /jianying-exports
  → GET /jianying-exports/{export_id}
```

补充剧本：

```text
POST /script-supplements/from-text 或 /from-file
  → 第一步重新 processing
  → 已开放步骤继续保持 can_view=true
  → 只对追加原文分集和提取资产
  → 与历史分集合并展示；未修改旧分集保持状态
  → 新分析完成后重新查询并确认 script-package
  → 编辑/合并/拆分涉及的旧分集同步更新并失效旧分镜
  → 确认资产后，仅新增或修改集进入第三步并发分析
```

## 11. 兼容接口

以下是旧版或底层编排接口，不是四步审核页面的推荐入口：

- `/projects/{project_id}/agent-productions`：从普通项目创建旧式任务；
- `/agent-productions/{production_id}/episode-plans...`：旧独立分集规划；
- `/agent-productions/{production_id}/pilot...`：旧试播集流程；
- `/agent-productions/{production_id}/batch...`：旧全剧批量控制。

新审核模式使用独立创建入口、`script-package`、`core-assets`、`storyboards` 和 `review` 四组接口。
