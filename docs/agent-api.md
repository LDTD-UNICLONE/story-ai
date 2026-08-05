# 整剧 Agent 接口文档

> 审核模式统一接口、步骤门禁和逐集并行规则请优先阅读
> [Agent 审核模式接口文档](./Agent审核模式接口文档.md)。若本文与统一文档冲突，以统一文档和当前后端代码为准。

> 第一步“剧本处理”的前端对接请优先阅读
> [Agent 第一步：剧本处理 API](./agent-script-processing-api.md)。
>
> 第二步“核心资产参考图”请阅读
> [Agent 第二步：核心资产参考图 API](./agent-core-assets-api.md)；第三步“分镜处理”请阅读
> [Agent 第三步：分镜处理 API](./agent-storyboards-api.md)。
> [Agent 分集视频生产 API](./agent-episode-video-api.md)。
>
> 四步状态、页面可见性和后端越级门禁请阅读
> [Agent 四步工作流 API](./agent-four-step-workflow-api.md)。
>
> 版本：1.3  
> 范围：Epic A–M  
> 更新日期：2026-08-01  
> 状态：目标公开契约；下游接口已实现，剧本优先入口待按本文档改造

本文档是整剧 Agent 前端联调的主入口。对用户固定展示“剧本处理 → 资产确认 → 分镜生成
→ 视频制作”四步；故事圣经、任务编排、审片和交付是这四步内部的后端子阶段。

各阶段的完整请求示例和业务细节见
[Agent 当前实现与验收说明](./agent-production-api.md)，产品约束见
[Agent PRD](./agent-production-prd-v1.md)。服务启动后还可访问 `/docs` 或 `/openapi.json`
查看当前代码生成的 OpenAPI 定义。

## 1. 通用约定

### 1.1 资源边界

- 用户进入 Agent 板块后，第一步是上传剧本文件或输入剧本文本。
- 用户不选择项目中心已有项目，创建请求中也不传项目中心的 `project_id`。
- 剧本创建成功时，服务端在同一事务中自动创建 Agent 项目、内部承载项目、来源文档和
  `AgentProduction`。
- 对前端而言，`AgentProduction.id` 就是 Agent 项目 ID，后续统一作为
  `{production_id}` 使用。
- 内部承载项目只服务于现有章节、角色、场景、道具和分镜数据关系，不出现在项目中心列表。
- 项目中心已有项目不能直接转换、导入或绑定为 Agent 项目。

### 1.2 基础约定

| 项目 | 约定 |
| --- | --- |
| API 前缀 | `/api/v1` |
| 鉴权 | `Authorization: Bearer <access_token>` |
| JSON 请求 | `Content-Type: application/json` |
| 文件上传 | `multipart/form-data` |
| ID | UUID 字符串 |
| 时间 | ISO 8601，北京时区，如 `2026-07-23T14:30:00+08:00` |

成功响应：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "timestamp": "2026-07-23T14:30:00+08:00"
}
```

失败响应：

```json
{
  "code": 40920,
  "message": "分集规划版本冲突，当前版本为 3",
  "data": {
    "expected_version": 2,
    "current_version": 3
  },
  "timestamp": "2026-07-23T14:30:00+08:00"
}
```

前端应同时判断 HTTP 状态码和业务 `code`。参数校验失败固定返回 HTTP 422、
`code=42200`，字段错误位于 `data[]`。错误响应可能携带 `X-Request-ID` 响应头，报障时
应记录该值。

### 1.3 兼容入口

以下“先创建项目，再上传来源文档和创建生产实例”的接口仅为旧客户端兼容入口：

```http
POST /api/v1/projects/{project_id}/agent-productions/source-preview
POST /api/v1/projects/{project_id}/agent-productions
GET  /api/v1/projects/{project_id}/agent-productions
```

新 Agent 页面必须使用本文档第 4 节的剧本优先入口；其他以
`/agent-productions/{production_id}` 开头的下游接口继续复用。

目标入口的事务规则：

- 文件解析或文本校验失败时，不创建 Agent 项目。
- 创建成功时，Agent 项目、内部承载项目、来源文档和生产实例必须同时成功。
- 任一数据库写入失败时整体回滚，不留下项目中心项目或空 Agent 项目。
- 同一用户短时间重复提交相同剧本时，可按内容哈希返回已有的草稿生产实例。

### 1.4 新建项目策略

- 文本模型固定使用后台启用的 `model_id=gpt-5.5`，图像模型固定使用
  `model_id=gpt-image-2`。自动模式创建时必须选择视频模型，审核模式进入视频生成阶段后选择。
- 自动模式要求账户至少有 100 积分，创建、保存自动模式草稿配置和启动时都会校验；该门槛不预扣积分。
- 不设置整剧积分预算。通过准入后，每个真实任务仍按现有积分规则扣费，余额不足时该任务失败。
- 不配置自动失败重试。单次任务失败后直接进入失败或异常状态，不自动再次扣费提交。
- 用户仍可在异常页显式重新生成、跳过或人工替换；显式操作是新的付费任务。
- 不设置试播集，也没有试播确认关卡。核心资产锁定后直接生产全部剧集。

Worker 崩溃或消息确认失败导致的幂等消息重投属于基础设施恢复，不视为业务重试，也不得重复
创建任务或扣费。

## 2. 主流程和页面路由

```text
上传剧本文件或输入剧本文本
  → 自动创建未配置的 Agent 草稿并返回剧本预览
  → 选择整体风格、画面生成比例和使用模式
  → 保存配置并启动整剧生产
  → 第一步：自动分集并分析、确认人物/场景/道具资产
  → 第二步：管理资产，可选生成或上传参考图，确认资产
  → 第三步：生成分镜组、绑定资产并按需生成视频
  → 第四步：按集数和分镜组顺序审片，导出 Windows 或 macOS 剪映草稿 ZIP
```

工作台是 Agent 页面状态的首选数据源：

```http
GET /api/v1/agent-productions/{production_id}/workbench
```

前端用 `next_action` 决定默认页面，用 `available_actions` 决定按钮，不要只根据
`status` 自行推断。

四步导航和页面访问权限使用：

```http
GET /api/v1/agent-productions/{production_id}/workflow
```

前端必须以 `current_step` 和 `steps[].can_view` 控制步骤页面；后端同时对第二至第四步接口
执行越级门禁。

| `next_action` | 页面或动作 |
| --- | --- |
| `configure` | 选择整体风格、画面比例和使用模式 |
| `start` | 开始分析 |
| `monitor_progress` | 全局进度 |
| `review_script` | 统一审核分集、基础资产与资产变体 |
| `review_episode_plan` | 分集规划 |
| `review_story_bible` | 故事圣经和候选审核 |
| `manage_core_assets` | 核心资产参考图 |
| `review_core_asset_changes` | 核心资产变更影响 |
| `generate_storyboards` | 资产确认自动提交未完成或需要手动补位的分镜组 |
| `monitor_storyboards` | 轮询并查看分镜处理进度 |
| `generate_episode_videos` | 逐集选择视频模型和分辨率并生成该集视频 |
| `monitor_batch` | 全部剧集生产 |
| `review_exceptions` | 异常处理 |
| `review_results` | 逐集审片和剪映草稿导出 |

## 3. 全量接口索引

以下路径均需拼接 `/api/v1`。

### 3.1 创建、状态和分集

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/agent-productions/from-file` | 上传剧本并自动创建 Agent 项目 |
| POST | `/agent-productions/from-text` | 输入剧本并自动创建 Agent 项目 |
| GET | `/agent-productions/{production_id}/configuration` | 查询上传后的整体配置 |
| PUT | `/agent-productions/{production_id}/configuration` | 选定整体风格、画面比例和使用模式 |
| GET | `/agent-productions` | 分页查询当前用户的 Agent 项目 |
| GET | `/agent-productions/{production_id}` | 查询生产详情 |
| GET | `/agent-productions/{production_id}/workbench` | 查询工作台 |
| GET | `/agent-productions/{production_id}/workflow` | 查询固定四步及逐集完成状态 |
| POST | `/agent-productions/{production_id}/start` | 启动 |
| POST | `/agent-productions/{production_id}/pause` | 暂停提交新任务 |
| POST | `/agent-productions/{production_id}/resume` | 恢复 |
| POST | `/agent-productions/{production_id}/cancel` | 取消 |
| GET | `/agent-productions/{production_id}/episode-plans` | 查询分集规划 |
| PATCH | `/agent-productions/{production_id}/episode-plans/{plan_id}` | 编辑一集 |
| POST | `/agent-productions/{production_id}/episode-plans/merge` | 合并相邻两集 |
| POST | `/agent-productions/{production_id}/episode-plans/{plan_id}/split` | 拆分一集 |
| GET | `/agent-productions/{production_id}/episode-plans/impact-preview` | 预览确认影响 |
| POST | `/agent-productions/{production_id}/episode-plans/confirm` | 确认分集 |
| GET | `/agent-productions/{production_id}/script-package` | 查询统一剧本处理结果 |
| POST | `/agent-productions/{production_id}/script-supplements/from-text` | 审核模式粘贴补充剧本并重新分析 |
| POST | `/agent-productions/{production_id}/script-supplements/from-file` | 审核模式上传补充剧本并重新分析 |
| PATCH | `/agent-productions/{production_id}/asset-variants/{variant_id}` | 编辑或审核资产变体 |
| POST | `/agent-productions/{production_id}/script-package/confirm` | 原子确认分集和文本资产 |

### 3.2 故事圣经和核心资产

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/agent-productions/{production_id}/story-bible/initialize` | 初始化故事圣经 |
| GET | `/agent-productions/{production_id}/story-bible` | 查询当前版本 |
| GET | `/agent-productions/{production_id}/story-bible/versions` | 查询历史版本 |
| PATCH | `/agent-productions/{production_id}/story-bible` | 创建修订版本 |
| POST | `/agent-productions/{production_id}/story-bible/confirm` | 确认故事圣经 |
| GET | `/agent-productions/{production_id}/asset-candidates` | 查询资产候选 |
| PATCH | `/agent-productions/{production_id}/asset-candidates/{candidate_id}` | 审核候选 |
| POST | `/agent-productions/{production_id}/asset-candidates/materialize` | 写入项目资产库 |
| GET | `/agent-productions/{production_id}/core-assets` | 查询、筛选和搜索基础资产 |
| POST | `/agent-productions/{production_id}/core-assets` | 新建基础资产 |
| GET | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}` | 查询基础资产详情和变体 |
| PATCH | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}` | 编辑基础资产 |
| DELETE | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}` | 删除基础资产及其变体 |
| POST | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants` | 新增资产变体 |
| DELETE | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}` | 删除资产变体 |
| PUT | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/reference-image` | 绑定变体上传图片并自动采用 |
| POST | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/image-generation` | 使用基础资产主图生成变体参考图 |
| GET | `/agent-productions/{production_id}/core-assets/readiness` | 查询核心资产就绪度 |
| POST | `/agent-productions/{production_id}/core-assets/image-generations` | 批量生成参考图 |
| PUT | `/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/reference-image` | 绑定上传图片并自动采用 |
| POST | `/agent-productions/{production_id}/core-assets/confirm` | 自动确认全部资产并立即提交可入队分集的分镜分析 |
| POST | `/agent-productions/{production_id}/core-assets/impact-preview` | 预览锁定影响 |
| POST | `/agent-productions/{production_id}/core-assets/lock` | 旧版手动选择并锁定资产 |

### 3.3 生产、监控和异常

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/agent-productions/{production_id}/storyboards` | 查询已完成分镜结果及全部分集任务状态 |
| POST | `/agent-productions/{production_id}/storyboards/generations` | 手动补位尚未开始或已失效分集的分镜分析 |
| POST | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards` | 新增草稿分镜组 |
| PUT | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/order` | 调整本集分镜组顺序 |
| PATCH | `/agent-productions/{production_id}/storyboards/{storyboard_id}` | 自动保存镜头、提示词及资产/变体绑定 |
| POST | `/agent-productions/{production_id}/storyboards/{storyboard_id}/copy` | 复制分镜组 |
| DELETE | `/agent-productions/{production_id}/storyboards/{storyboard_id}` | 软删除分镜组；每集至少保留一组 |
| GET | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/asset-options` | 查询目标分镜可用基础资产及当前集适用变体 |
| PUT | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-config` | 保存分镜组视频模型、分辨率和预估时长配置 |
| GET | `/agent-productions/{production_id}/episodes/{chapter_id}/videos` | 查询一集及其全部分镜组的视频状态 |
| POST | `/agent-productions/{production_id}/episodes/{chapter_id}/video-generations` | 按单组配置生成本集视频，请求参数作为未配置分镜的默认值 |
| POST | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-generations` | 调整参数并生成或重生成单个分镜组视频 |
| GET | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/video-versions` | 查询分镜组全部视频候选及选择状态 |
| PUT | `/agent-productions/{production_id}/episodes/{chapter_id}/storyboards/{storyboard_id}/primary-video` | 确认一个候选为分镜组主视频 |
| GET | `/agent-productions/{production_id}/batch` | 查询全部剧集生产状态 |
| POST | `/agent-productions/{production_id}/batch/dispatch` | 批量任务补位 |
| POST | `/agent-productions/{production_id}/batch/video-model` | 旧版全剧批量链路：全局锁定视频模型和分辨率 |
| GET | `/agent-productions/{production_id}/matrix` | 查询逐集逐镜头矩阵 |
| GET | `/agent-productions/{production_id}/exceptions` | 分页查询异常 |
| GET | `/agent-productions/{production_id}/events` | 分页查询事件 |
| GET | `/agent-productions/{production_id}/costs` | 查询积分成本 |
| POST | `/agent-productions/{production_id}/jobs/retry` | 精确重试失败或已失效任务；分镜阶段传章节 ID |
| POST | `/agent-productions/{production_id}/jobs/skip` | 跳过或人工替换 |

### 3.4 审片和交付

第四步的完整请求、响应、状态、幂等和剪映 ZIP 说明见
[`agent-video-editing-api.md`](./agent-video-editing-api.md)。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/agent-productions/{production_id}/review` | 查询审片聚合数据 |
| POST | `/agent-productions/{production_id}/review-issues` | 记录审片问题 |
| PATCH | `/agent-productions/{production_id}/review-issues/{issue_id}` | 处理问题 |
| GET | `/agent-productions/{production_id}/episodes/{chapter_id}/video-timeline` | 按分镜组顺序查询单集视频 |
| POST | `/agent-productions/{production_id}/episodes/{chapter_id}/approve` | 确认单集 |
| POST | `/agent-productions/{production_id}/jianying-exports` | 创建 Windows/macOS 剪映草稿 ZIP |
| GET | `/agent-productions/{production_id}/jianying-exports` | 查询剪映导出列表 |
| GET | `/agent-productions/{production_id}/jianying-exports/{export_id}` | 查询剪映导出状态 |
| GET | `/agent-productions/{production_id}/delivery-readiness` | 检查交付条件 |
| POST | `/agent-productions/{production_id}/deliveries` | 创建交付 |
| GET | `/agent-productions/{production_id}/deliveries` | 查询交付列表 |
| GET | `/agent-productions/{production_id}/deliveries/{delivery_id}` | 查询交付状态 |

## 4. 剧本优先创建 Agent 项目

### 4.1 通过文件创建

```http
POST /api/v1/agent-productions/from-file
Content-Type: multipart/form-data
```

表单字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | File | 是 | TXT、MD、DOCX 或可提取文本的 PDF |
| `name` | string | 否 | Agent 项目名称；为空时从文件名生成 |
| `style_id` | UUID | 是 | `GET /styles` 返回的启用风格 ID |
| `generation_ratio` | string | 是 | 视频画面比例 |
| `video_resolution` | string | 是 | `480p`、`720p`、`1080p` 或 `4k` |
| `mode` | string | 是 | `supervised`（审核模式）或 `automatic`（自动模式） |
| `video_model_id` | UUID | 自动模式必填 | 启用的视频模型记录 ID；审核模式不传 |

集数由 Agent 根据剧情结构自动决定；创建阶段不接受目标集数、单集时长或分镜时长。
创建阶段不接受文本/图像模型、失败重试、试播集数、积分预算或音频开关。自动模式必须提前
选择视频模型，审核模式仍在实际生成视频时选择。
默认文件上限 20 MB、抽取正文上限 10 万字符。V1 不支持扫描 PDF 的 OCR。解析失败时整个
请求失败，不创建 Agent 项目。

### 4.2 通过输入文本创建

```http
POST /api/v1/agent-productions/from-text
Content-Type: application/json
```

```json
{
  "name": "雨夜旧宅",
  "content": "完整剧本文本……",
  "style_id": "style-uuid",
  "generation_ratio": "9:16",
  "video_resolution": "1080p",
  "mode": "automatic",
  "video_model_id": "video-model-record-uuid"
}
```

`content` 去除首尾空白后不能为空。`name` 为空时，服务端从剧本标题或正文首行生成名称。

### 4.3 创建响应

两个创建接口返回相同结构：

```json
{
  "production_id": "production-uuid",
  "name": "雨夜旧宅",
  "status": "draft",
  "current_stage": "source",
  "source": {
    "source_type": "docx",
    "file_name": "雨夜旧宅.docx",
    "content_hash": "sha256",
    "character_count": 45678,
    "content_preview": "正文前 3000 字……",
    "content_preview_truncated": true,
    "warnings": []
  },
  "style_id": "style-uuid",
  "generation_ratio": "9:16",
  "video_resolution": "1080p",
  "mode": "automatic",
  "video_model_id": "video-model-record-uuid",
  "configuration_required": false,
  "created_at": "2026-08-01T14:30:00+08:00"
}
```

前端只保存并使用 `production_id`。响应中不返回供用户选择的项目中心 `project_id`；
即使后续兼容响应中出现内部 `project_id`，前端也应把它视为只读实现细节。

### 4.4 查询或修改整体配置

整体配置已经随上传请求保存，创建响应固定返回 `configuration_required=false`。启动分析前，
前端仍可通过以下接口查询或修改整体配置：

```http
GET /api/v1/styles
GET /api/v1/agent-productions/{production_id}/configuration
```

保存完整配置：

```http
PUT /api/v1/agent-productions/{production_id}/configuration
Content-Type: application/json
```

```json
{
  "style_id": "style-uuid",
  "generation_ratio": "9:16",
  "video_resolution": "1080p",
  "mode": "automatic",
  "video_model_id": "video-model-record-uuid"
}
```

| 字段 | 可选值或约束 | 说明 |
| --- | --- | --- |
| `style_id` | `GET /styles` 返回的启用风格 UUID | 控制整剧人物、场景、分镜画面和视频的统一视觉风格 |
| `generation_ratio` | `21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16` | 控制后续分镜画面和视频画幅；核心资产设定图仍固定 16:9 |
| `video_resolution` | `480p`、`720p`、`1080p`、`4k` | 控制后续视频输出分辨率 |
| `mode` | `supervised`、`automatic` | 前者在关键节点等待确认；后者在强制关卡之外自动推进 |
| `video_model_id` | 启用的视频模型记录 UUID | `automatic` 必填，`supervised` 不传 |

成功响应：

```json
{
  "production_id": "production-uuid",
  "style_id": "style-uuid",
  "style": {
    "id": "style-uuid",
    "name": "写实漫剧",
    "cover": "https://example.com/style.png",
    "version": "v1"
  },
  "generation_ratio": "9:16",
  "video_resolution": "1080p",
  "mode": "automatic",
  "video_model_id": "video-model-record-uuid",
  "configured": true,
  "configurable": true
}
```

配置仅能在 `status=draft`、`current_stage=source` 时修改。启动分析后
`configurable=false`，再次提交返回 `40981`。历史未配置任务调用 `/start` 仍返回 `40981`。

### 4.5 查询 Agent 项目

```http
GET /api/v1/agent-productions?status=draft&page=1&page_size=20
GET /api/v1/agent-productions/{production_id}
```

列表只返回当前用户的 Agent 项目，不接受项目中心 `project_id`。项目中心的
`GET /api/v1/projects` 必须排除内部 Agent 承载项目。

列表响应同时返回服务端计算的轮询指令：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "items": [
      {
        "id": "production-uuid",
        "name": "雨夜旧宅",
        "status": "planning",
        "current_stage": "script_review",
        "mode": "supervised",
        "active_task_count": 0,
        "has_active_tasks": false,
        "should_poll": false,
        "next_poll_seconds": null,
        "created_at": "2026-08-03T11:00:00+08:00",
        "updated_at": "2026-08-03T11:10:00+08:00"
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 20
  },
  "timestamp": "2026-08-03T11:10:00+08:00"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `active_task_count` | integer | 当前项目 `pending/running` 的模型任务和交付任务总数 |
| `has_active_tasks` | boolean | `active_task_count > 0` |
| `should_poll` | boolean | 前端当前是否需要继续请求 Agent 列表 |
| `next_poll_seconds` | integer/null | 建议下次列表轮询间隔；停止轮询时为 `null` |

轮询规则：

- 审核模式仅在 `has_active_tasks=true` 时轮询；停留在剧本审核、资产确认或视频模型选择页面时停止；
- 自动模式处于 `planning/running` 时保持轮询，因为后台控制器可能在任务间隙继续推进；
- 暂停、待审核、异常、完成和取消状态在没有活跃任务时停止轮询；
- 活跃的合并视频交付也计入 `active_task_count`；
- 当前建议间隔为 10 秒。前端应使用所有 `should_poll=true` 项目中最小的
  `next_poll_seconds`，没有此类项目时关闭定时器；
- 创建、删除、启动或手动刷新后仍应立即重新请求一次，不必等待定时器。

删除 Agent 项目：

```http
DELETE /api/v1/agent-productions/{production_id}
```

请求体为空。删除采用软删除：内部 Agent 承载项目设置为不可用，数据库中的生产记录、
剧本、分集和资产历史仍保留。未完成生产会先进入 `cancelled`，已完成生产保留
`completed` 状态。删除成功后，该项目不再出现在 Agent 项目列表中，详情和工作台查询返回
`40430`。

```json
{
  "production_id": "production-uuid",
  "status": "cancelled",
  "deleted": true
}
```

只有项目所有者可以删除。重复删除或删除其他用户的 Agent 项目均按不存在处理。

### 4.6 启动

```http
POST /api/v1/agent-productions/{production_id}/start
```

创建成功后可直接展示正文预览并启动；整体配置已在上传时保存。动作接口无请求体。

规格约束：目标集数 1–200，目标单集时长 15–600 秒，默认镜头时长 4–15 秒。后台没有
启用的 `gpt-5.5` 或 `gpt-image-2` 记录时，启动返回明确的“Agent 固定模型尚未配置”
错误。此时不要求视频模型，视频模型延迟到全剧生产的 `videos` 阶段选择。

## 5. 分集规划

新建 Agent 项目（`workflow_version=2`）仍使用本节的查询、编辑、合并、拆分和影响预览接口，
但不能调用本节的 `/episode-plans/confirm`。第一步必须按
[Agent 第一步：剧本处理 API](./agent-script-processing-api.md) 使用
`/script-package/confirm` 统一确认分集和资产。

查询：

```http
GET /api/v1/agent-productions/{production_id}/episode-plans
```

响应包含 `version`、`status`、`planning_summary` 和 `items[]`。每集包含标题、正文、钩子、
目标、冲突、高潮、预计时长、预计镜头数、原文区间、角色、场景和连续性备注。

编辑：

```http
PATCH /api/v1/agent-productions/{production_id}/episode-plans/{plan_id}
```

```json
{
  "expected_version": 1,
  "title": "第一集：雨夜旧宅",
  "estimated_duration_seconds": 90,
  "position": 1
}
```

合并、拆分和确认：

```http
POST /api/v1/agent-productions/{production_id}/episode-plans/merge
POST /api/v1/agent-productions/{production_id}/episode-plans/{plan_id}/split
GET  /api/v1/agent-productions/{production_id}/episode-plans/impact-preview
POST /api/v1/agent-productions/{production_id}/episode-plans/confirm
```

```json
{
  "expected_version": 4,
  "idempotency_key": "episode-plan-confirm-v4"
}
```

合并请求传两个不同且相邻的 `plan_ids`。拆分请求的 `split_at` 是原文字符位置，必须在该集
`source_start` 与 `source_end` 之间。

## 6. 故事圣经和资产

本节的故事圣经初始化、修改、单独确认和候选单独物化接口属于旧流程。
`workflow_version=2` 只复用基础资产候选 PATCH 接口，并使用 6.1 节的统一剧本处理包查询、
变体 PATCH 和统一确认接口。

```http
POST /api/v1/agent-productions/{production_id}/story-bible/initialize
GET  /api/v1/agent-productions/{production_id}/story-bible
PATCH /api/v1/agent-productions/{production_id}/story-bible
```

```json
{
  "expected_version": 1,
  "content": {
    "story_summary": "全剧梗概",
    "continuity_rules": ["第一至三集保持灰色长风衣"],
    "forbidden_conflicts": ["玉佩不能在第五集之前损坏"]
  }
}
```

修改会创建新版本，不覆盖历史版本。

候选查询支持 `asset_type=character|scene|prop` 和
`review_status=ready|needs_review|rejected|materialized`。

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

低置信度候选必须设置为 `ready` 或 `rejected`。物化请求：

```json
{
  "expected_bible_version": 1,
  "candidate_ids": ["candidate-1-uuid", "candidate-2-uuid"]
}
```

确认故事圣经：

```json
{
  "expected_version": 1,
  "idempotency_key": "story-bible-confirm-v1"
}
```

以上故事圣经接口属于旧版分步流程。新建 Agent 项目使用统一剧本处理包，确认时会自动接受
结构合法的低置信度资产，不要求逐项处理 `needs_review`。

### 6.1 新建 Agent 项目的第一步

`workflow_version=2` 不再要求前端依次确认“分集规划”和“故事圣经”。解析完成后通过
`GET /agent-productions/{production_id}/script-package` 一次读取：

- 分集；
- 人物资产：`characters`、`character_variants`；
- 场景资产：`scenes`、`scene_variants`；
- 道具资产：`props`、`prop_variants`；
- 警告和审核状态。

后端先确定性分块原文（不调用模型），再顺序执行“分集分析”和“资产分析”两个模型任务。
只有分集结果连续且完整覆盖原文，才会创建资产分析任务；两个任务成功后，后端依据分集
原文范围和资产证据关联集数并统一落库。`episodes` 按集数升序返回，每集的
`source_content` 是对应的完整原始剧本；三类基础资产和资产变体均返回 `episode_numbers`。
该拆分不增加前端步骤，工作台仍只展示
`source_analysis → script_review`。

人物、场景、道具的 `asset_type` 分别固定为 `character`、`scene`、`prop`。
变装或状态变化必须通过 `base_candidate_id` 关联同类型基础资产，不能作为新的基础资产。
公开响应以六个顶层分组确定资产类型，不要求前端读取对象中重复的 `asset_type`；候选库外键、
置信度、合并原因、完整原文证据和时间戳不属于第一步主响应。
第一步不生成参考图。审核完成后调用
`POST /agent-productions/{production_id}/script-package/confirm`，直接进入核心资产阶段。

## 7. 核心资产

完整的第二步前端联调契约见
[Agent 第二步：核心资产参考图接口文档](./agent-core-assets-api.md)。

批量生成参考图：

```http
POST /api/v1/agent-productions/{production_id}/core-assets/image-generations
```

```json
{
  "items": [
    {
      "asset_type": "character",
      "asset_id": "character-uuid",
      "prompt": "正面全身，保持人物设定"
    },
    {
      "asset_type": "scene",
      "asset_id": "scene-uuid"
    }
  ]
}
```

一次最多 50 个不同资产，服务端使用后台默认图片模型。生成成功后图片自动成为当前
`reference_image`。手动图片先通过 `/uploads/file` 上传，再调用：

```http
PUT /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/reference-image
```

Agent 核心资产参考图固定使用 `gpt-image-2` 和 `16:9`，不跟随项目画幅：

- 人物：白底横图，左侧人物肖像，右侧同一人物同一服装的正面、侧面、背面三视图；
- 场景：单幅 16:9 场景图，完整呈现空间结构，不生成多格设定卡；
- 道具：白底横图，左侧道具完整图，右侧同一道具的侧面、背面或关键细节角度。

批量接口生成第一步确认后物化的基础人物、场景和道具。资产变体仍通过
`base_candidate_id` 依附基础资产，但拥有独立 `reference_image`，通过单变体接口上传或生成：

```http
PUT  /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/reference-image
POST /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/image-generation
```

手动上传变体图不依赖基础资产主图。模型生成变体图时必须已有主图，后端固定引用该主图，只把
剧情化可见变化应用到变体；两种方式都只更新变体图，不覆盖基础资产主图。

第二步确认完成后，上述基础资产和变体的上传、生成、替换、移除图片接口仍然可用。图片变化会
使第二步暂时变为 `invalidated` 并进入 `core_asset_change_review`；恢复为锁定图片时自动返回原阶段，
采用新图片时需要完成影响确认和新资产锁，依赖旧图的分镜媒体随后失效。确认后不开放资产结构、
名称和剧情描述的直接修改。

完成第二步时调用自动确认接口，前端不传资产列表：

```http
POST /api/v1/agent-productions/{production_id}/core-assets/confirm
```

```json
{
  "expected_lock_version": 0,
  "idempotency_key": "core-assets-confirm-v1"
}
```

后端自动确认当前全部有效资产及其变体，缺少参考图也允许进入第三步；但存在
`pending/running` 的基础资产或变体图像任务时必须等待。旧版 `impact-preview/lock` 接口继续
保留，用于已有锁发生变更后的影响确认。`workflow_version>=2` 时，确认成功会使用稳定幂等键
自动提交分镜分析；`storyboards/generations` 只用于 `not_started/invalidated` 分集的手动补位，
`failed` 分集必须使用 `jobs/retry` 精确重试。

## 8. 逐集媒体生产与旧版批量兼容

核心资产锁定后，各分集的分镜任务独立并发分析，单集失败不阻断其他分集。任意一集完成后，
使用 `/episodes/{chapter_id}/videos` 和 `/video-generations` 按分集生成视频；全局资产参考图
直接作为视频引用。详见 [Agent 分集视频生产接口](./agent-episode-video-api.md)。

以下 `/batch` 接口仅用于 `workflow_version=1` 的旧版自动全剧生产兼容。旧链路仍按
“分镜 → 图片 → 视频”全局推进。`workflow_version>=2` 的四步 Agent 在分镜分析完成后进入
`episode_videos`，不会进入全局图片或视频阶段；调用全局视频模型或全剧媒体调度接口会返回
`40986`。新版前端不得依赖旧批量阶段状态。

所有分集的分镜分析完成后，第三步即为 `completed` 并开放第四步，不等待视频生成。
第四步会返回所有分镜组；尚无视频的分镜组返回 `video_status=not_started`、
`video_url=null`、`video_history_id=null`，仅不允许确认或导出。

当 `GET /batch` 返回 `phase=videos`、`requires_video_model=true` 时，前端才加载
`GET /api/v1/models/options?model_type=video` 并展示视频模型和分辨率选择器。确认请求：

```http
POST /api/v1/agent-productions/{production_id}/batch/video-model
```

```json
{
  "expected_core_asset_lock_version": 1,
  "video_model_id": "video-model-record-uuid",
  "video_resolution": "1080p"
}
```

这里提交模型记录的 `id`，不是供应商 `model_id`。`video_resolution` 必填，支持 `480p`、
`720p`、`1080p`、`4k`。确认前 `can_dispatch=false`；视频任务一旦开始，不允许更换模型或
分辨率。

批量补位：

```http
POST /api/v1/agent-productions/{production_id}/batch/dispatch
```

```json
{
  "expected_core_asset_lock_version": 1,
  "idempotency_key": "batch-dispatch-001",
  "max_tasks": 5
}
```

`max_tasks` 为 1–50。相同幂等键不会重复提交，下一次补位使用新键。自动模式由后台控制器
继续补位；审核模式只执行用户显式提交的请求。页面关闭不会中断已经提交的任务。`GET /batch`
的关键字段为 `paused`、`active_task_count`、
`failed_item_count`、`recommended_batch_size`、`can_dispatch` 和 `is_complete`。

## 9. 监控、失败处理和人工替换

```http
GET /api/v1/agent-productions/{production_id}/matrix
GET /api/v1/agent-productions/{production_id}/exceptions?page=1&page_size=50
GET /api/v1/agent-productions/{production_id}/events?page=1&page_size=50
GET /api/v1/agent-productions/{production_id}/costs
```

系统不自动重试失败任务。用户在异常页显式选择“重新生成”时，使用精确重生成接口：

```json
{
  "expected_core_asset_lock_version": 1,
  "stage": "video",
  "scope_ids": ["storyboard-uuid"],
  "idempotency_key": "manual-regenerate-video-001"
}
```

显式重新生成会按新的任务正常扣费，并保留旧失败记录。

人工跳过或替换：

```json
{
  "expected_core_asset_lock_version": 1,
  "stage": "video",
  "scope_ids": ["storyboard-uuid"],
  "reason": "使用人工审核通过的视频",
  "replacement_url": "https://trusted.example/video.mp4",
  "idempotency_key": "manual-replace-video-001"
}
```

`stage=storyboard` 时 `scope_ids` 为章节 ID；`image|video` 时为分镜 ID。传
`replacement_url` 时一次只能处理一个图片或视频镜头。

## 10. 逐集审片预览

审片聚合：

```http
GET /api/v1/agent-productions/{production_id}/review
```

响应包含总集数、已确认集数、开放问题数、阻断问题数和 `episodes[]`。第四步中每集的
剪辑式预览使用下列接口：

```http
GET /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/video-timeline
```

`items[]` 已按 `group_number` 升序排列，包含分镜组 ID、标题、预估时长、当前主视频
地址和生成历史 ID。`ready_to_approve=true` 表示本集所有分镜组都有主视频且没有开放的
阻断问题。

记录问题：

```json
{
  "chapter_id": "chapter-uuid",
  "storyboard_id": "storyboard-uuid",
  "media_type": "video",
  "category": "character_consistency",
  "severity": "blocking",
  "description": "人物服装与核心资产不一致"
}
```

`category`：`character_consistency`、`style_drift`、`motion`、`rhythm`、`dialogue`、
`audio`、`compliance`、`other`。`severity`：`info`、`warning`、`blocking`。

处理问题：

```json
{
  "expected_lock_version": 0,
  "status": "resolved",
  "resolution_note": "已选择修复后的第三版视频"
}
```

确认单集：

```json
{
  "expected_lock_version": 0,
  "idempotency_key": "approve-episode-01-v1"
}
```

提交前用 `/review` 的最新 `review_lock_version`。只有视频全部就绪且不存在开放的
`blocking` 问题时才能确认。

## 11. 剪映草稿 ZIP 导出

先调用：

```http
GET /api/v1/agent-productions/{production_id}/delivery-readiness
```

确认 `ready=true` 后，根据用户选择传入 `windows` 或 `macos`：

```json
{
  "platform": "windows",
  "jianying_version": "10.8",
  "draft_name": "灵境漫剧-全集",
  "chapter_ids": [],
  "idempotency_key": "jianying-windows-all-v1"
}
```

```http
POST /api/v1/agent-productions/{production_id}/jianying-exports
```

- `platform` 必填，只允许 `windows` 或 `macos`，服务端根据该字段构建对应草稿包。
- `jianying_version` 当前只支持 `10.8`；不传时使用 `10.8`。
- `chapter_ids=[]` 导出全剧；非空时只导出指定集，但不会把项目第四步标记为完成。
- ZIP 内容包含顺序命名的视频、草稿 JSON、导出清单、使用说明和目标平台的路径重定位脚本。
- Windows 包含 `relocate_windows.ps1`，macOS 包含 `relocate_macos.command`；解压后先运行对应脚本，再将草稿目录导入或复制到剪映专业版草稿目录。

查询导出任务：

```http
GET /api/v1/agent-productions/{production_id}/jianying-exports
GET /api/v1/agent-productions/{production_id}/jianying-exports/{export_id}
```

轮询详情中的 `status`；`completed` 时 `output_url` 是 ZIP 下载地址，`failed` 时展示
`error_summary`。全剧导出成功后第四步才完成。草稿是对剪映专业版 10.8 格式的工程化适配，
剪映升级后应先在测试环境验证再放开新版本。

## 12. 并发、幂等和轮询

- `expected_version`、`expected_lock_version`、`expected_core_asset_lock_version`
  必须使用最近一次查询值。
- 分镜组自动保存必须提交响应中的最新 `expected_revision`，缺少版本号不接受写入。
- `idempotency_key` 至少 8 个字符，推荐
  `{production_id}:{action}:{client_request_id}`。
- 同一幂等键只能重放同一请求；参数变化必须换新键。
- HTTP 超时后，带幂等键的写接口应使用原请求重试，再查询状态。
- HTTP 409 后先刷新资源，不要用旧页面数据自动覆盖。

| 页面 | 接口 | 建议间隔 | 停止条件 |
| --- | --- | --- | --- |
| Agent 总览 | `/workbench` | 3 秒 | 等待确认、暂停、异常、完成或取消 |
| 核心资产图片 | `/task-records/batch` | 使用 `next_poll_seconds` | `stop_polling=true` |
| 批量生产 | `/batch` | 3–5 秒 | 完成、暂停或异常 |
| 剪映草稿导出 | `/jianying-exports/{export_id}` | 3–5 秒 | `completed` 或 `failed` |

## 13. 常用错误码

| HTTP | `code` | 含义 | 前端处理 |
| --- | --- | --- | --- |
| 401 | `40102` / `40103` | 登录无效或用户不存在 | 重新登录 |
| 403 | `40302` | 账号被禁用 | 退出并提示 |
| 404 | `40430` | 整剧任务不存在 | 返回项目页 |
| 404 | `40431` | 来源文档不存在 | 重新上传 |
| 404 | `40433` | 资产候选不存在 | 刷新候选 |
| 404 | `40440`–`40444` | 审片或交付资源不存在 | 刷新当前页面 |
| 400 | `40003` | 账户积分不足 | 充值后由用户决定是否重新生成 |
| 400 | `40049` / `40054` | 正文或上传文件为空 | 阻止提交并重新选择 |
| 400 | `40050` | 正文超过上限 | 拆分或精简 |
| 400 | `40053` | 文件类型不支持 | 使用 TXT、MD、DOCX、PDF |
| 400 | `40055`–`40060` | 文件抽取、编码或格式错误 | 展示 `message` |
| 400 | `40063` | 剪映平台或草稿版本不支持 | 改用 Windows/macOS 和 10.8 |
| 400 | `40064` | PDF/DOCX 隔离解析进程异常终止 | 提示重新导出文件后上传 |
| 408 | `40801` | PDF/DOCX 解析超时 | 拆分或重新导出文件后上传 |
| 409 | `40911` | 当前状态不允许该动作 | 刷新工作台 |
| 409 | `40915` / `40918` / `40920` | 分集未生成、不可编辑或版本冲突 | 刷新分集 |
| 409 | `40926` / `40927` | 候选尚未审核 | 返回候选审核 |
| 409 | `40930` / `40931` | 圣经或候选版本冲突 | 刷新对应资源 |
| 409 | `40933` / `40935` | 核心资产版本或指纹过期 | 重新预览 |
| 409 | `40950`–`40955` | 批量生产前置条件不满足 | 刷新批量状态 |
| 409 | `40960`–`40965` | 重试或人工处理不允许 | 刷新生产矩阵 |
| 409 | `40969` | 尝试删除每集最后一个分镜组 | 先新增替代组再删除 |
| 409 | `40970` | 审片问题版本冲突 | 刷新问题 |
| 409 | `40971` / `40976` | 幂等键参数不一致 | 使用新幂等键 |
| 409 | `40972`–`40975` | 单集未就绪或确认快照变化 | 刷新审片 |
| 409 | `40979` | 不满足交付条件 | 查询交付就绪度 |
| 409 | `40980` | `gpt-5.5` 或 `gpt-image-2` 尚未配置 | 阻止启动并提示联系管理员 |
| 409 | `40981` | 整体配置未完成，或分析启动后尝试改配置 | 返回配置页或刷新工作台 |
| 409 | `40986` | 新版 Agent 尝试使用全剧媒体调度 | 改用目标集的 `video-generations` |
| 409 | `40990` | 尝试跳过未完成的前置步骤 | 按工作流顺序完成前置步骤 |
| 413 | `41301` | 剧本文件过大 | 压缩或拆分 |
| 422 | `42200` | 参数校验失败 | 映射 `data[].loc` 到表单 |
| 429 | `42920` | 待处理任务达到上限 | 等待已有任务 |
| 500 | `50000` | 服务端错误 | 记录 `X-Request-ID` |

不要在前端日志中记录访问令牌或完整剧本正文。
