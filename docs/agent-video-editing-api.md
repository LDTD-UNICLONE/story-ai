# Agent 第四步：视频审片与剪映草稿导出接口文档

| 项目         | 内容                                 |
| ------------ | ------------------------------------ |
| 文档版本     | 1.0                                  |
| 更新日期     | 2026-08-03                           |
| API 前缀     | `/api/v1`                            |
| 适用步骤     | 第四步 `video_editing`               |
| 目标客户端   | Windows 剪映专业版、macOS 剪映专业版 |
| 当前草稿版本 | `10.8`                               |

## 1. 功能边界

第四步只负责：

1. 按集数查看视频审片结果。
2. 按分镜组顺序返回每集的主视频时间线。
3. 记录、解决或驳回审片问题。
4. 确认每一集当前的视频快照。
5. 将已确认的视频按顺序生成 Windows 或 macOS 剪映草稿 ZIP。

分镜组提示词修改、资产变体切换、视频模型和分辨率设置、生成新的视频候选以及
主视频选择属于第三步。第四步直接使用每个分镜组当前已选定的主视频；如果尚未生成或
选定视频，时间线仍返回该分镜组，但 `video_url` 和 `video_history_id` 为 `null`。

## 2. 认证和通用响应

所有接口都需要登录：

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

成功响应统一结构：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "timestamp": "2026-08-03T18:30:00+08:00"
}
```

失败响应统一结构：

```json
{
  "code": 40990,
  "message": "前置步骤尚未完成",
  "data": {
    "requested_step": 4,
    "required_step": 3,
    "current_step": 3,
    "required_status": "completed"
  },
  "timestamp": "2026-08-03T18:30:00+08:00"
}
```

## 3. 第四步门禁和完成条件

第四步所有接口都会执行后端门禁。所有分集的分镜分析完成、第三步状态变为
`completed` 后即可访问，不要求分镜已生成视频。前置步骤未完成时返回 HTTP 409 /
`40990`。

项目第四步标记为 `completed` 必须同时满足：

- 存在一个 `status=completed` 的剪映草稿导出任务。
- 该导出任务覆盖项目当前全部有效集数。

全剧导出任务在创建时已强制校验所有集数已确认、无阻断问题且视频完整。

只导出部分集数不会完成第四步。Windows 或 macOS 任意一种全剧草稿导出成功均可
完成第四步。

## 4. 推荐对接流程

```text
GET /review
  → 按集数展示审片总览
  → GET /episodes/{chapter_id}/video-timeline
  → 按 items[].group_number 显示当前集的主视频
  → 可选：POST /review-issues
  → 可选：PATCH /review-issues/{issue_id}
  → POST /episodes/{chapter_id}/approve
  → 重复处理其他集
  → GET /delivery-readiness
  → ready=true 后让用户选择 Windows 或 macOS
  → POST /jianying-exports
  → GET /jianying-exports/{export_id} 轮询
  → status=completed 后下载 output_url
```

## 5. 接口索引

| 方法  | 路径                                                                      | 用途                      |
| ----- | ------------------------------------------------------------------------- | ------------------------- |
| GET   | `/agent-productions/{production_id}/review`                               | 查询整剧审片总览          |
| GET   | `/agent-productions/{production_id}/episodes/{chapter_id}/video-timeline` | 查询单集视频时间线        |
| POST  | `/agent-productions/{production_id}/review-issues`                        | 新建审片问题              |
| PATCH | `/agent-productions/{production_id}/review-issues/{issue_id}`             | 解决或驳回审片问题        |
| POST  | `/agent-productions/{production_id}/episodes/{chapter_id}/approve`        | 确认单集当前视频快照      |
| GET   | `/agent-productions/{production_id}/delivery-readiness`                   | 查询全剧交付就绪度        |
| POST  | `/agent-productions/{production_id}/jianying-exports`                     | 创建剪映草稿 ZIP 导出任务 |
| GET   | `/agent-productions/{production_id}/jianying-exports`                     | 查询剪映导出历史          |
| GET   | `/agent-productions/{production_id}/jianying-exports/{export_id}`         | 查询单个剪映导出任务      |
| POST  | `/agent-productions/{production_id}/deliveries`                           | 通用交付入口              |
| GET   | `/agent-productions/{production_id}/deliveries`                           | 查询通用交付列表          |
| GET   | `/agent-productions/{production_id}/deliveries/{delivery_id}`             | 查询通用交付详情          |

## 6. 查询整剧审片总览

```http
GET /api/v1/agent-productions/{production_id}/review
```

成功响应：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "production_id": "71b21c5e-55f0-4538-9ae7-d1c7ff8a7c60",
    "status": "running",
    "total_episode_count": 2,
    "approved_episode_count": 1,
    "open_issue_count": 1,
    "blocking_issue_count": 1,
    "delivery_ready": false,
    "episodes": [
      {
        "chapter_id": "11111111-1111-1111-1111-111111111111",
        "episode_number": 1,
        "title": "第一集",
        "review_status": "pending",
        "review_lock_version": 0,
        "approved_at": null,
        "issue_count": 1,
        "blocking_issue_count": 1,
        "storyboards": [
          {
            "storyboard_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "shot_number": 1,
            "title": "雨夜推门",
            "duration_seconds": 7,
            "image_status": "success",
            "image_url": "https://oss.example.com/storyboard-1.png",
            "image_history_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "image_version_count": 1,
            "video_status": "selected",
            "video_url": "https://oss.example.com/storyboard-1.mp4",
            "video_history_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "video_version_count": 2,
            "issue_count": 1,
            "blocking_issue_count": 1
          }
        ]
      }
    ]
  },
  "timestamp": "2026-08-03T18:30:00+08:00"
}
```

顶层字段：

| 字段                     | 类型    | 说明                                              |
| ------------------------ | ------- | ------------------------------------------------- |
| `production_id`          | UUID    | Agent 项目 ID                                     |
| `status`                 | string  | Agent 项目当前状态                                |
| `total_episode_count`    | integer | 有效集数                                          |
| `approved_episode_count` | integer | 当前媒体快照仍然有效的已确认集数                  |
| `open_issue_count`       | integer | 未处理问题数                                      |
| `blocking_issue_count`   | integer | 未处理的阻断问题数                                |
| `delivery_ready`         | boolean | 全部集数已确认、无阻断问题且无缺失视频时为 `true` |
| `episodes`               | array   | 按集数顺序返回审片数据                            |

`review_status` 枚举：

| 值            | 说明                                                   |
| ------------- | ------------------------------------------------------ |
| `pending`     | 尚未确认                                               |
| `approved`    | 已确认，且当前媒体快照与确认时一致                     |
| `invalidated` | 确认后图片、视频、主版本或生成状态发生变化，需重新确认 |

`video_status` 常见值包括 `not_started`、`pending`、`running`、`selection_required`、
`selected`、`success`、`failed`、`invalidated` 和 `skipped`。只有 `success` 或 `selected` 且
`video_url` 非空时，该分镜组视频才算就绪。

## 7. 查询单集视频时间线

```http
GET /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/video-timeline
```

成功响应：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "production_id": "71b21c5e-55f0-4538-9ae7-d1c7ff8a7c60",
    "chapter_id": "11111111-1111-1111-1111-111111111111",
    "episode_number": 1,
    "title": "第一集",
    "review_status": "pending",
    "review_lock_version": 0,
    "ready_to_approve": true,
    "estimated_duration_seconds": 13,
    "items": [
      {
        "storyboard_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "group_number": 1,
        "title": "雨夜推门",
        "estimated_duration_seconds": 7,
        "video_status": "selected",
        "video_url": "https://oss.example.com/E001_G001.mp4",
        "video_history_id": "cccccccc-cccc-cccc-cccc-cccccccccccc"
      },
      {
        "storyboard_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "group_number": 2,
        "title": "屋内环视",
        "estimated_duration_seconds": 6,
        "video_status": "success",
        "video_url": "https://oss.example.com/E001_G002.mp4",
        "video_history_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
      }
    ]
  },
  "timestamp": "2026-08-03T18:30:00+08:00"
}
```

| 字段                         | 类型      | 说明                                             |
| ---------------------------- | --------- | ------------------------------------------------ |
| `review_lock_version`        | integer   | 提交单集确认时作为 `expected_lock_version`       |
| `ready_to_approve`           | boolean   | 本集有分镜组、视频全部就绪且无未处理阻断问题     |
| `estimated_duration_seconds` | integer   | 本集所有分镜组预估时长之和，不是实际视频解析时长 |
| `items`                      | array     | 已按 `group_number` 升序排列的分镜组主视频       |
| `video_history_id`           | UUID/null | 当前选中的视频生成历史 ID                        |

前端不需要再次对 `items` 作业务排序，但建议仍使用 `group_number` 作为展示编号。

## 8. 新建审片问题

```http
POST /api/v1/agent-productions/{production_id}/review-issues
```

请求体：

```json
{
  "chapter_id": "11111111-1111-1111-1111-111111111111",
  "storyboard_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  "media_type": "video",
  "category": "motion",
  "severity": "blocking",
  "description": "人物手部动作异常"
}
```

| 字段            | 类型        | 必填 | 限制                                          |
| --------------- | ----------- | ---- | --------------------------------------------- |
| `chapter_id`    | UUID        | 是   | 必须属于当前 Agent 项目                       |
| `storyboard_id` | UUID/null   | 否   | 传入时必须属于 `chapter_id`；不传表示集级问题 |
| `media_type`    | string/null | 否   | `image`、`video`、`audio` 或 `text`           |
| `category`      | string      | 是   | 见下方类别枚举                                |
| `severity`      | string      | 否   | `info`、`warning`、`blocking`；默认 `warning` |
| `description`   | string      | 是   | 2–2000 个字符                                 |

`category` 枚举：

- `character_consistency`：角色一致性。
- `style_drift`：画风漂移。
- `motion`：动作问题。
- `rhythm`：节奏问题。
- `dialogue`：台词问题。
- `audio`：音频问题。
- `compliance`：合规问题。
- `other`：其他。

成功响应 `message` 为 `审片问题已记录`，`data`：

```json
{
  "id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
  "production_id": "71b21c5e-55f0-4538-9ae7-d1c7ff8a7c60",
  "chapter_id": "11111111-1111-1111-1111-111111111111",
  "storyboard_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  "media_type": "video",
  "category": "motion",
  "severity": "blocking",
  "status": "open",
  "description": "人物手部动作异常",
  "resolution_note": null,
  "resolved_by": null,
  "resolved_at": null,
  "lock_version": 0,
  "created_at": "2026-08-03T18:30:00+08:00",
  "updated_at": "2026-08-03T18:30:00+08:00"
}
```

该接口没有幂等键，重复提交会创建多条问题。前端点击提交后应立即禁用按钮，等待请求结束。

## 9. 处理审片问题

```http
PATCH /api/v1/agent-productions/{production_id}/review-issues/{issue_id}
```

请求体：

```json
{
  "expected_lock_version": 0,
  "status": "resolved",
  "resolution_note": "已重新检查并接受当前动作"
}
```

| 字段                    | 类型    | 必填 | 限制                                     |
| ----------------------- | ------- | ---- | ---------------------------------------- |
| `expected_lock_version` | integer | 是   | 必须与最新 `lock_version` 一致，不小于 0 |
| `status`                | string  | 是   | `resolved` 或 `dismissed`                |
| `resolution_note`       | string  | 是   | 2–2000 个字符                            |

成功后问题 `lock_version` 加 1，并返回完整的问题对象。使用旧版本号返回 HTTP 409 /
`40970`，`data.current_lock_version` 是服务端最新版本。

## 10. 确认单集

```http
POST /api/v1/agent-productions/{production_id}/episodes/{chapter_id}/approve
```

请求体：

```json
{
  "expected_lock_version": 0,
  "idempotency_key": "approve-episode-01-v1"
}
```

| 字段                    | 类型    | 必填 | 限制                                           |
| ----------------------- | ------- | ---- | ---------------------------------------------- |
| `expected_lock_version` | integer | 是   | 使用时间线或审片总览中的 `review_lock_version` |
| `idempotency_key`       | string  | 是   | 8–128 个字符；同一次确认重试时保持不变         |

确认前置条件：

- 本集至少有一个分镜组。
- 每个分镜组 `video_status` 为 `success` 或 `selected`。
- 每个分镜组 `video_url` 非空。
- 本集没有未处理的 `blocking` 问题。

成功响应 `message` 为 `剧集审片已确认`，`data`：

```json
{
  "id": "12345678-1234-1234-1234-123456789012",
  "production_id": "71b21c5e-55f0-4538-9ae7-d1c7ff8a7c60",
  "chapter_id": "11111111-1111-1111-1111-111111111111",
  "status": "approved",
  "media_snapshot_hash": "d610c53c...",
  "approved_by": "22222222-2222-2222-2222-222222222222",
  "approved_at": "2026-08-03T18:35:00+08:00",
  "lock_version": 1,
  "idempotency_key": "approve-episode-01-v1"
}
```

使用同一 `idempotency_key` 且媒体快照未变时，返回原确认结果。如果快照已变化，返回
`40974`。确认后如果第三步重新选择主视频或修改生成结果，`review_status` 会变为
`invalidated`，必须用最新 `review_lock_version` 和新幂等键再次确认。

## 11. 查询全剧交付就绪度

```http
GET /api/v1/agent-productions/{production_id}/delivery-readiness
```

成功响应：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "production_id": "71b21c5e-55f0-4538-9ae7-d1c7ff8a7c60",
    "ready": false,
    "total_episode_count": 2,
    "approved_episode_count": 1,
    "unapproved_chapter_ids": ["22222222-2222-2222-2222-222222222222"],
    "open_blocking_issue_count": 0,
    "missing_video_storyboard_ids": []
  },
  "timestamp": "2026-08-03T18:36:00+08:00"
}
```

`ready=true` 的条件与整剧导出校验一致。如果只需导出部分集数，本接口仍然返回全剧
就绪度；真正创建部分导出时，服务端只校验 `chapter_ids` 中的集数。

## 12. 创建剪映草稿导出

```http
POST /api/v1/agent-productions/{production_id}/jianying-exports
```

Windows 请求示例：

```json
{
  "platform": "windows",
  "jianying_version": "10.8",
  "draft_name": "旧宅谜案-全剧",
  "chapter_ids": [],
  "idempotency_key": "jianying-windows-all-v1"
}
```

macOS 请求示例：

```json
{
  "platform": "macos",
  "jianying_version": "10.8",
  "draft_name": "旧宅谜案-全剧",
  "chapter_ids": [],
  "idempotency_key": "jianying-macos-all-v1"
}
```

请求字段：

| 字段               | 类型        | 必填 | 限制与语义                                         |
| ------------------ | ----------- | ---- | -------------------------------------------------- |
| `platform`         | string      | 是   | `windows` 或 `macos`；服务端不根据 User-Agent 猜测 |
| `jianying_version` | string      | 否   | 默认且当前只允许 `10.8`                            |
| `draft_name`       | string/null | 否   | 1–80 个字符；不传时使用项目名                      |
| `chapter_ids`      | UUID[]      | 否   | 默认 `[]`；最多 200 个，重复 ID 会去重             |
| `idempotency_key`  | string      | 是   | 8–128 个字符，在当前 Agent 项目内唯一              |

`chapter_ids` 规则：

- `[]`：导出当前 Agent 项目全部有效集数，按服务端集数顺序输出。
- 非空：导出指定集数，导出集的先后顺序与 `chapter_ids` 请求顺序一致。
- 所有 ID 都必须属于当前 Agent 项目。
- 指定集数必须已确认、无未处理阻断问题，且每个分镜组都有就绪的主视频。

成功响应 `message` 为 `剪映草稿导出任务已创建`，`data`：

```json
{
  "id": "99999999-9999-9999-9999-999999999999",
  "production_id": "71b21c5e-55f0-4538-9ae7-d1c7ff8a7c60",
  "status": "pending",
  "platform": "windows",
  "jianying_version": "10.8",
  "draft_name": "旧宅谜案-全剧",
  "episode_count": 2,
  "video_count": 18,
  "output_url": null,
  "error_summary": null,
  "compatibility_notes": [
    "草稿格式目标版本为剪映专业版 10.8。",
    "解压后先运行包内路径重定位脚本，再将草稿目录导入剪映。",
    "Windows 包使用 draft_content.json 和 PowerShell 路径重定位脚本。"
  ],
  "created_at": "2026-08-03T18:40:00+08:00",
  "updated_at": "2026-08-03T18:40:00+08:00"
}
```

导出状态：

| `status`    | 说明                             | 前端处理                       |
| ----------- | -------------------------------- | ------------------------------ |
| `pending`   | 已建立任务，等待 Worker          | 开始轮询详情                   |
| `running`   | 正在下载视频、生成草稿或上传 ZIP | 继续轮询                       |
| `completed` | 导出成功                         | 使用 `output_url` 下载 ZIP     |
| `failed`    | 导出失败                         | 停止轮询并显示 `error_summary` |

幂等规则：

- 请求超时后，应带原请求体和原 `idempotency_key` 重试。
- 相同幂等键与相同参数返回原导出任务，不重复打包。
- 相同幂等键修改平台、版本、草稿名或集数后返回 HTTP 409 / `40976`。
- 用户主动重新导出时必须换新幂等键。

## 13. 查询剪映导出列表

```http
GET /api/v1/agent-productions/{production_id}/jianying-exports
```

`data` 直接是数组，按创建时间倒序返回：

```json
{
  "code": 0,
  "message": "success",
  "data": [
    {
      "id": "99999999-9999-9999-9999-999999999999",
      "production_id": "71b21c5e-55f0-4538-9ae7-d1c7ff8a7c60",
      "status": "completed",
      "platform": "windows",
      "jianying_version": "10.8",
      "draft_name": "旧宅谜案-全剧",
      "episode_count": 2,
      "video_count": 18,
      "output_url": "https://oss.example.com/旧宅谜案-windows.zip",
      "error_summary": null,
      "compatibility_notes": ["草稿格式目标版本为剪映专业版 10.8。"],
      "created_at": "2026-08-03T18:40:00+08:00",
      "updated_at": "2026-08-03T18:42:00+08:00"
    }
  ],
  "timestamp": "2026-08-03T18:42:00+08:00"
}
```

## 14. 查询单个剪映导出

```http
GET /api/v1/agent-productions/{production_id}/jianying-exports/{export_id}
```

响应字段与创建导出接口相同。建议只在 `status=pending|running` 时每 3–5 秒轮询一次；
当 `status=completed|failed` 时立即停止轮询。

## 15. ZIP 包内容

导出视频使用 `E{episode_number}_G{group_number}.mp4` 命名，例如：

```text
旧宅谜案-全剧/
├── draft_content.json
├── draft_meta_info.json
├── manifest.json
├── README.txt
├── relocate_windows.ps1
└── materials/
    └── video/
        ├── E001_G001.mp4
        ├── E001_G002.mp4
        └── E002_G001.mp4
```

macOS ZIP 同时包含 `draft_info.json`、`draft_content.json` 和
`relocate_macos.command`。Windows ZIP 的主草稿文件为 `draft_content.json`。

用户操作：

1. 完整解压 ZIP，不要只解压 JSON。
2. Windows 运行 `relocate_windows.ps1`；macOS 运行 `relocate_macos.command`。
3. 将整个草稿目录导入或复制到剪映专业版配置的草稿目录。
4. 不要单独移动 `materials` 目录，否则草稿中的视频会离线。

## 16. 通用交付接口

后端仍保留下列通用交付接口：

```http
POST /api/v1/agent-productions/{production_id}/deliveries
GET  /api/v1/agent-productions/{production_id}/deliveries
GET  /api/v1/agent-productions/{production_id}/deliveries/{delivery_id}
```

`delivery_type` 支持：

| 类型             | 说明                                        |
| ---------------- | ------------------------------------------- |
| `manifest`       | 同步生成交付清单，创建后立即为 `completed`  |
| `merged_video`   | 异步合并视频                                |
| `jianying_draft` | 异步生成剪映草稿 ZIP，必须同时传 `platform` |

前端对接剪映导出时，建议使用 `/jianying-exports` 专用接口。专用响应已隐藏内部租约和
完整交付清单，只返回第四步页面需要的字段。

## 17. 错误码

| HTTP | `code`            | 含义                                   | 处理建议                                  |
| ---- | ----------------- | -------------------------------------- | ----------------------------------------- |
| 401  | `40102` / `40103` | 登录信息无效或用户不存在               | 重新登录                                  |
| 403  | `40302`           | 账号已被禁用                           | 退出并提示用户                            |
| 404  | `40430`           | Agent 项目不存在或不属于当前用户       | 返回 Agent 项目列表                       |
| 404  | `40440`           | 审片剧集不存在，或部分导出包含无效剧集 | 刷新集数列表                              |
| 404  | `40441`           | 审片分镜不存在或不属于指定剧集         | 刷新当前集                                |
| 404  | `40442`           | 审片问题不存在                         | 刷新问题列表                              |
| 404  | `40444`           | 剪映导出或交付任务不存在               | 刷新导出列表                              |
| 409  | `40970`           | 审片问题版本冲突                       | 用 `current_lock_version` 刷新后再提交    |
| 409  | `40972`           | 剧集仍有未就绪视频                     | 根据 `data.storyboard_ids` 返回第三步处理 |
| 409  | `40973`           | 剧集仍有未解决的阻断问题               | 先处理阻断问题                            |
| 409  | `40974`           | 幂等确认对应的媒体快照已变化           | 刷新时间线并使用新键确认                  |
| 409  | `40975`           | 剧集审片版本冲突                       | 使用 `current_lock_version` 刷新后再提交  |
| 409  | `40976`           | 导出幂等键已用于其他参数               | 生成新的幂等键                            |
| 409  | `40979`           | 所选集数尚未满足交付条件               | 处理未确认剧集和缺失视频                  |
| 409  | `40990`           | 前三步尚未完成                         | 按顺序完成前置步骤                        |
| 422  | `42200`           | 请求参数校验失败                       | 根据 `data[].loc` 定位字段                |

下列错误发生在异步导出 Worker 中，接口通过 `status=failed` 和 `error_summary` 返回结果：

| 内部错误 | 含义                         |
| -------- | ---------------------------- |
| `40063`  | 导出平台或剪映草稿版本不支持 |
| `40977`  | 导出清单没有可用视频         |
| `41302`  | 导出视频总大小超过服务端限制 |
| `42216`  | 下载的视频无法解析或读取     |

## 18. 前端对接要点

- 步骤页面先查询 `/workflow`，只有第四步 `can_view=true` 时才进入。
- 使用 `/review` 显示集数列表，使用 `/video-timeline` 显示某一集的剪辑时间线。
- `ready_to_approve=false` 时禁用确认按钮，并根据视频状态或阻断问题给出原因。
- 单集确认前立即重新查询时间线，使用最新 `review_lock_version`。
- 让用户显式选择 Windows 或 macOS，然后将结果原样传入 `platform`。
- 创建导出前先查询 `/delivery-readiness`；全剧导出只在 `ready=true` 时开放。
- 导出任务只在 `pending` 或 `running` 时轮询，建议间隔 3–5 秒。
- 下载完 ZIP 后展示 `compatibility_notes`，提醒用户先运行路径重定位脚本。

## 19. 当前实现限制

- 当前没有审片问题列表或详情查询接口。`GET /review` 只返回问题数量，不返回问题 ID、
  描述和版本。前端只能从创建问题的响应中取得 `issue_id` 后立即处理；页面重新加载后无法
  仅通过现有第四步接口恢复问题明细。
- 剪映导出任务不返回百分比进度，只有 `pending`、`running`、`completed`、`failed`
  四种状态。
- Worker 内部错误明细保存在内部字段和日志中，专用导出接口只返回脱敏后的
  `error_summary`。
- 剪映草稿格式当前固定为 10.8，不接受其他版本。剪映客户端升级后需要先做实机导入验证。
