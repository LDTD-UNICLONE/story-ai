# Agent 第二步：资产图像确认 API

| 项目 | 内容 |
| --- | --- |
| 文档版本 | 2.0 |
| 更新日期 | 2026-08-02 |
| API 前缀 | `/api/v1` |
| 当前阶段 | `planning / core_assets` |
| 下一阶段 | 第三步分镜生成 |

## 1. 功能范围

第二步管理第一步确认后生成的人物、场景、道具基础资产及其变体：

- 查询、新增、编辑和删除基础资产；
- 查询、新增和删除资产变体；
- 为基础资产上传或生成主图；
- 为资产变体手动上传独立图片，或在基础资产已有主图后使用模型生成变体图片；
- 自动确认当前全部有效资产和变体，进入第三步。

图片不是进入第三步的必填条件，用户可以不生成图片直接确认。但是已经提交的图片任务必须先
结束，不能在 `pending/running` 状态下确认。

### 1.1 固定图片规格

第二步固定使用 `gpt-image-2` 和 `16:9`，前端不传模型 ID或画幅：

- 人物：白底横图，左侧人物肖像，右侧同一人物同一服装的正面、侧面、背面三视图；
- 场景：单幅完整场景图，展示空间结构、固定陈设、材质和光线；
- 道具：白底横图，左侧完整图，右侧侧面、背面或关键细节角度；
- 资产变体：继承所属基础资产版式和主图，只应用剧情要求的可见变化。

## 2. 第二步公开响应原则

所有接口需要 `Authorization: Bearer <access_token>`。本文示例只展示统一响应包中的 `data`；
实际响应仍为 `{code, message, data, timestamp}`，HTTP 422 的业务码为 `42200`。

第二步已经使用正式 `asset_id`。新前端不得依赖以下兼容或内部字段：

- `candidate_id`、`base_candidate_id`、`bible_version`；
- 候选审核状态、模型置信度、合并原因、原文证据和原文字符偏移；
- `project_id`、`production_id`（路径中已有时）、内部锁记录 ID；
- `created_at`、`updated_at`；
- 完整任务 Prompt、模型 ID、积分事务 ID、任务 `extra` 和供应商任务信息；
- 确认接口中的完整资产快照和完整影响快照。

必须保留：

- 基础资产 `asset_type + asset_id`；
- 变体 `id`；
- 名称、视觉设计、出现集数和图片状态；
- 基础资产及变体自己的 `lock_version`；
- 图片任务的 `task_record_id`、状态、积分和轮询间隔；
- 自动确认使用的全局 `lock_version`。

服务端为兼容旧调用方可能暂时返回更多字段，但这些字段不属于公开契约，前端应忽略。

## 3. 前端调用顺序

```text
GET core-assets
  → 按 character / scene / prop 展示基础资产，变体嵌套在所属资产下
  → 可选：新增、编辑或删除资产和变体
  → 可选：为基础资产上传或生成主图
  → 可选：为变体手动上传图片
  → 可选：基础资产已有主图后，为变体生成图片
  → POST task-records/batch 轮询已提交任务
  → GET readiness 取得最新全局 lock_version
  → POST confirm
  → 进入第三步
```

所有写操作只允许在 `current_stage=core_assets` 且尚未确认时执行。

## 4. 查询和管理资产

### 4.1 查询资产列表

```http
GET /api/v1/agent-productions/{production_id}/core-assets?asset_type=character&keyword=沈砚&page=1&page_size=20
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `asset_type` | 否 | `character`、`scene`、`prop` |
| `keyword` | 否 | 匹配名称、别名和视觉设计，1～128 字符 |
| `page` | 否 | 默认 `1` |
| `page_size` | 否 | 默认 `20`，最大 `100` |

公开响应示例：

```json
{
  "items": [
    {
      "asset_type": "character",
      "asset_id": "11111111-1111-1111-1111-111111111111",
      "canonical_name": "沈砚",
      "aliases": ["阿砚"],
      "content": {
        "source_facts": {
          "story_role": "调查玉佩失踪事件的主角"
        },
        "design_spec": {
          "apparent_age": "二十七八岁",
          "default_costume": "黑色长风衣",
          "identity_anchors": ["利落黑色短发", "窄长脸"]
        }
      },
      "reference_image": "https://example.com/shenyan.png",
      "image_generation_status": "selected",
      "lock_version": 2,
      "variants": [
        {
          "id": "77777777-7777-7777-7777-777777777777",
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
          "reference_image": "https://example.com/shenyan-injury.png",
          "image_generation_status": "success",
          "lock_version": 1
        }
      ]
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

基础资产的 `content` 只保证 `source_facts`、`design_spec`；变体 `content` 只保证
`visual_delta`、`preserve_anchors`、`state_scope`。服务端返回的其他模型原始字段不要用于页面
逻辑。

查询单个资产：

```http
GET /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}
```

公开响应与 `items[]` 单项相同。

### 4.2 新增基础资产

```http
POST /api/v1/agent-productions/{production_id}/core-assets
Content-Type: application/json
```

```json
{
  "asset_type": "character",
  "canonical_name": "周凛",
  "aliases": ["周队"],
  "content": {
    "source_facts": {"story_role": "刑警队长"},
    "design_spec": {
      "apparent_age": "三十五岁左右",
      "identity_anchors": ["寸头", "左眉浅疤"]
    }
  }
}
```

成功响应使用精简资产对象，`reference_image=null`、`image_generation_status=null`、
`lock_version=0`、`variants=[]`。

### 4.3 编辑基础资产

```http
PATCH /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}
Content-Type: application/json
```

```json
{
  "expected_lock_version": 2,
  "canonical_name": "沈砚",
  "aliases": ["阿砚"],
  "content": {
    "design_spec": {
      "default_costume": "深灰色长风衣",
      "identity_anchors": ["利落黑色短发", "窄长脸"]
    }
  }
}
```

除 `expected_lock_version` 外至少提交一个修改字段。`content` 按键浅合并。成功响应只需要读取
精简资产对象；前端不要读取候选 ID或时间戳。

### 4.4 删除基础资产

```http
DELETE /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}?expected_lock_version=2
```

删除会同步删除其全部变体。基础资产或任一变体存在 `pending/running` 图片任务时返回
`40939`。HTTP 200 即表示成功，前端无需依赖 `deleted=true` 或删除计数。

### 4.5 新增资产变体

```http
POST /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants
Content-Type: application/json
```

```json
{
  "canonical_name": "沈砚夜行装",
  "variant_type": "costume",
  "description": "黑色夜行服，隐藏身份",
  "trigger_reason": "潜入旧宅",
  "episode_numbers": [1],
  "content": {
    "visual_delta": {
      "add": ["黑色夜行服"],
      "replace": ["替换默认长风衣"],
      "remove": []
    },
    "preserve_anchors": ["利落黑色短发", "窄长脸"],
    "state_scope": "episode_only"
  }
}
```

`canonical_name`、`variant_type`、`description`、`trigger_reason` 必填。公开成功响应使用精简变体
对象，不返回 `base_candidate_id`、重复 `asset_type`、原文证据或时间戳。

变体类型：

| 资产 | `variant_type` |
| --- | --- |
| 人物 | `costume`、`makeup`、`age`、`injury`、`disguise` |
| 场景 | `time`、`weather`、`season`、`festival`、`damage`、`layout`、`state` |
| 道具 | `form`、`damage`、`open_state`、`bloodied`、`upgrade`、`ownership`、`state` |

### 4.6 删除资产变体

```http
DELETE /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}?expected_lock_version=1
```

这里的版本是变体自己的 `lock_version`。存在 `pending/running` 图片任务时返回 `40939`。
HTTP 200 即表示删除成功，前端无需依赖响应中的 `variant_id` 或 `deleted=true`。

## 5. 上传和生成图片

第二步确认完成后，下列图片接口仍然可用：

- 基础资产主图批量生成；
- 基础资产主图上传、替换或移除；
- 资产变体图片生成；
- 资产变体图片上传、替换或移除。

确认只表示当前资产版本允许进入第三步，不会永久冻结图片。确认后仍冻结资产名称、剧情描述以及
资产/变体的结构性新增和删除；本节只放开图片操作。整剧已经 `completed` 或 `cancelled` 时不允许
继续修改。

### 5.1 为基础资产批量生成主图

```http
POST /api/v1/agent-productions/{production_id}/core-assets/image-generations
Content-Type: application/json
```

```json
{
  "items": [
    {
      "asset_type": "character",
      "asset_id": "11111111-1111-1111-1111-111111111111"
    },
    {
      "asset_type": "scene",
      "asset_id": "22222222-2222-2222-2222-222222222222",
      "prompt": "突出暴雨和地面积水反光"
    }
  ]
}
```

一次最多 50 项，不能重复。前端不传 `ai_model_id`、`generation_mode` 或画幅。`prompt` 只是
用户补充要求，不替代固定版式。

公开响应：

```json
{
  "items": [
    {
      "asset_type": "character",
      "asset_id": "11111111-1111-1111-1111-111111111111",
      "submitted": true,
      "task_record_id": "44444444-4444-4444-4444-444444444444",
      "status": "pending",
      "points_cost": 1,
      "next_poll_seconds": 10
    }
  ],
  "submitted_count": 1,
  "failed_count": 0,
  "total_points_cost": 1
}
```

批量接口可能 HTTP 200 但单项失败；此时该项返回 `submitted=false`、`error_code`、
`error_message`。

### 5.2 为资产变体生成图片

```http
POST /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/image-generation
Content-Type: application/json
```

```json
{
  "prompt": "伤口清晰可见，但保持人物身份和原有服装",
  "extra": {}
}
```

`prompt`、`extra` 都可以省略。基础资产没有主图时返回 `40984`，不会创建任务或扣积分。后端
强制把基础资产主图作为模型参考，只应用变体可见变化。成功结果只更新变体图，不覆盖主图。

单变体生成的公开响应只需要：

```json
{
  "submitted": true,
  "task_record_id": "44444444-4444-4444-4444-444444444444",
  "status": "pending",
  "points_cost": 1,
  "next_poll_seconds": 10
}
```

路径中已有资产和变体 ID，响应不需要重复返回。

### 5.3 手动上传并采用基础资产主图

先上传文件：

```http
POST /api/v1/uploads/file
Content-Type: multipart/form-data
```

表单字段为 `file=<图片>`、`category=agent-core-asset`。取得 `data.url` 后绑定：

```http
PUT /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/reference-image
Content-Type: application/json
```

```json
{
  "expected_lock_version": 2,
  "reference_image": "https://example.com/uploaded-character.png"
}
```

绑定成功后图片立即成为主图，状态为 `selected`。传 `reference_image=null` 可以移除主图。

### 5.4 手动上传并采用变体图片

```http
PUT /api/v1/agent-productions/{production_id}/core-assets/{asset_type}/{asset_id}/variants/{variant_id}/reference-image
Content-Type: application/json
```

```json
{
  "expected_lock_version": 1,
  "reference_image": "https://example.com/uploaded-variant.png"
}
```

这里使用变体自己的 `lock_version`。手动上传不依赖基础资产主图；成功响应只需要读取更新后的
变体公开字段。

### 5.5 确认后的图片变更

已确认资产的基础图或变体图发生变化后：

1. 新图片立即保存到对应资产，生成历史不会覆盖或删除；
2. 第二步状态变为 `invalidated`，项目进入 `core_asset_change_review`；
3. 第三步和第四步暂时不可继续提交新任务；
4. 如果把图片恢复成当前锁定版本，系统自动清除变更并回到原生产阶段；
5. 如果采用新图片，先执行影响预览并确认新的资产锁版本；只有实际绑定该资产的分镜媒体会失效。

图片任务仍是异步的：提交生成时不会立即改变资产锁；只有任务成功并写入新的参考图后才进入影响
复核。基础资产与变体都遵循同一规则。

## 6. 轮询图片任务

```http
POST /api/v1/task-records/batch
Content-Type: application/json
```

```json
{
  "ids": ["44444444-4444-4444-4444-444444444444"]
}
```

第二步只依赖以下任务字段：

```json
{
  "items": [
    {
      "id": "44444444-4444-4444-4444-444444444444",
      "status": "success",
      "result": "https://example.com/generated.png",
      "points_cost": 1,
      "stop_polling": true,
      "next_poll_seconds": null
    }
  ],
  "stop_polling": true,
  "next_poll_seconds": null
}
```

前端不得读取通用任务响应中的 `user_id`、`ai_model_id`、`points_transaction_id`、业务外键、完整
Prompt、`extra` 或供应商任务字段。任务全部结束后重新查询 `GET /core-assets` 获取最终图片。

## 7. 查询确认版本

```http
GET /api/v1/agent-productions/{production_id}/core-assets/readiness
```

新流程只依赖：

```json
{
  "lock_version": 0,
  "lock_status": null
}
```

资产列表、图片和生成状态已经由 `GET /core-assets` 返回，不在 readiness 中重复消费。
`bible_version`、`candidate_id`、`review_status`、重复资产列表、`ready_count`、
`missing_reference_count` 和旧版 `can_lock` 不属于新前端契约。

## 8. 自动确认并进入第三步

```http
POST /api/v1/agent-productions/{production_id}/core-assets/confirm
Content-Type: application/json
```

```json
{
  "expected_lock_version": 0,
  "idempotency_key": "core-assets-confirm-v1"
}
```

后端自动确认当前全部有效基础资产和变体，前端不提交资产列表。图片可以为空；存在基础资产或
变体图片任务 `pending/running` 时返回 `40985`。

公开成功响应：

```json
{
  "version": 1,
  "already_locked": false
}
```

前端不读取 `lock_id`、固定 `status`、完整 `assets` 快照或首次确认的完整 `impact`。确认成功后，
后端使用资产锁版本和稳定幂等键立即并发提交当前可入队分集的分镜分析任务，然后进入第三步。
前端只需通过工作台和分镜查询接口读取最新状态，不需要紧接着再次调用
`POST /storyboards/generations`。

相同 `idempotency_key` 重试返回 `already_locked=true`；派生出的分镜提交幂等键保持不变，不会
重复创建任务或重复扣费。后台 Controller 也会识别停在 `batch_production/batch_storyboards`
的新版审核模式项目，用于恢复资产已经确认但直接提交过程被中断的项目。该兜底只生成分镜，
不会自动生成视频。

## 9. 旧版手动锁定兼容接口

新前端不调用以下接口完成首次确认：

```http
POST /api/v1/agent-productions/{production_id}/core-assets/impact-preview
POST /api/v1/agent-productions/{production_id}/core-assets/lock
```

它们用于已确认图片发生变更后的下游影响预览，以及旧前端兼容。影响预览返回的
`impact_fingerprint` 必须原样提交给 `/lock`；前端不得自己计算。基础资产主图或任一变体图
变化都属于 `reference_changed`。

## 10. 主要错误码

| HTTP | `code` | 场景 | 前端处理 |
| --- | --- | --- | --- |
| 400 | `40003` | 积分不足 | 提示充值 |
| 400 | `40052` | 达到旧整剧积分预算 | 停止提交 |
| 400 | `40058` | 旧手动锁定缺少人物或场景 | 补充选择 |
| 404 | `40430` | Agent 项目不存在或无权限 | 返回项目列表 |
| 404 | `40434` | 资产不存在、不属于当前剧本或已删除 | 刷新资产列表 |
| 404 | `40440` | 资产变体不存在 | 刷新资产详情 |
| 409 | `40932` | 当前阶段不允许操作 | 刷新工作台 |
| 409 | `40933` | 基础资产或确认版本冲突 | 刷新资产或 readiness |
| 409 | `40935` | 旧版影响指纹变化 | 重新影响预览 |
| 409 | `40938` | 同名同类型变体已存在 | 不重复创建 |
| 409 | `40939` | 基础资产或变体正在生成图片 | 等待任务结束 |
| 409 | `40940` | 变体版本冲突 | 刷新资产详情 |
| 409 | `40980` | 固定图片模型未配置 | 联系管理员 |
| 409 | `40984` | 生成变体图时基础资产没有主图 | 先设置基础资产主图；手动上传不受此限制 |
| 409 | `40985` | 确认时仍有图片任务执行中 | 等待任务结束 |
| 422 | `42200` | 请求字段校验失败 | 标记错误字段 |
| 503 | `50301` | 图片任务入队失败 | 展示错误并允许重试 |

## 11. 前端页面字段映射

| 页面区域 | 公开字段 |
| --- | --- |
| 三类资产页签 | `asset_type`、`asset_id`、`canonical_name`、`aliases` |
| 视觉设计 | 基础资产 `content.source_facts/design_spec` |
| 变体列表 | `variants[].id/canonical_name/variant_type/description/trigger_reason/episode_numbers/content` |
| 图片卡片 | `reference_image`、`image_generation_status` |
| 并发编辑 | 基础资产和变体各自的 `lock_version` |
| 图片轮询 | `task_record_id/status/result/stop_polling/next_poll_seconds` |
| 最终确认 | readiness 的 `lock_version` |

变体必须作为基础资产子项展示。变体“上传图片”始终可用；只有基础资产已有
`reference_image` 时才启用“模型生成”。图片状态为 `pending/running` 时禁止重复提交、删除和
最终确认。
