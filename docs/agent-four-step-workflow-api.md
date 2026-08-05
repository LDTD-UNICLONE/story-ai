# Agent 四步工作流接口文档

> 本文是早期分步骤说明。当前审核模式的权威契约见
> [Agent 审核模式接口文档](./Agent审核模式接口文档.md)，尤其是补充剧本后的单调开放和第三、四步逐集并行规则。

| 项目 | 内容 |
| --- | --- |
| 文档版本 | 1.0 |
| 更新日期 | 2026-08-01 |
| API 前缀 | `/api/v1` |
| 适用模式 | `supervised`、`automatic` |

## 1. 固定流程

Agent 对前端只暴露以下四步，步骤编号和 `step_code` 是稳定接口契约：

| 步骤 | `step_code` | 名称 | 完成条件 |
| --- | --- | --- | --- |
| 1 | `script_processing` | 剧本处理 | 分集和人物/场景/道具资产分析已确认并落库 |
| 2 | `asset_confirmation` | 资产图像确认 | 管理资产及参考图，当前全部有效资产自动确认 |
| 3 | `storyboard_generation` | 分镜生成 | 全部分集的分镜组已分析生成并落库，不要求已生成视频 |
| 4 | `video_editing` | 视频制作 | 逐集审片完成，全剧 Windows 或 macOS 剪映草稿 ZIP 导出完成 |

项目创建时，服务端会在同一事务中立即创建四条项目级步骤记录。第一步确认并物化分集时，
每一集也会创建四条分集级步骤记录。后续查询会根据真实业务数据校准这些记录，不能由前端直接
修改步骤状态。

第三步完成后立即开放第四步。此时分镜组可以没有视频，第四步仍返回完整的分镜顺序，
对应项的 `video_url` 和 `video_history_id` 为 `null`。视频未就绪只会阻止单集确认和导出，
不会阻止进入第四步。

## 2. 查询工作流

```http
GET /api/v1/agent-productions/{production_id}/workflow
Authorization: Bearer <token>
```

成功响应：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "production_id": "14660067-122a-4fb0-be0f-a7fd404b7b15",
    "mode": "supervised",
    "current_step": 2,
    "steps": [
      {
        "id": "11111111-1111-1111-1111-111111111111",
        "step_number": 1,
        "step_code": "script_processing",
        "title": "剧本处理",
        "status": "completed",
        "completed": true,
        "can_view": true,
        "is_current": false,
        "started_at": "2026-08-01T10:00:00+08:00",
        "completed_at": "2026-08-01T10:05:00+08:00",
        "extra": {"current_stage": "core_assets"}
      },
      {
        "id": "22222222-2222-2222-2222-222222222222",
        "step_number": 2,
        "step_code": "asset_confirmation",
        "title": "资产确认",
        "status": "waiting_review",
        "completed": false,
        "can_view": true,
        "is_current": true,
        "started_at": "2026-08-01T10:05:00+08:00",
        "completed_at": null,
        "extra": {"current_stage": "core_assets"}
      },
      {
        "id": "33333333-3333-3333-3333-333333333333",
        "step_number": 3,
        "step_code": "storyboard_generation",
        "title": "分镜生成",
        "status": "not_started",
        "completed": false,
        "can_view": false,
        "is_current": false,
        "started_at": null,
        "completed_at": null,
        "extra": {"current_stage": "core_assets"}
      },
      {
        "id": "44444444-4444-4444-4444-444444444444",
        "step_number": 4,
        "step_code": "video_editing",
        "title": "视频制作",
        "status": "not_started",
        "completed": false,
        "can_view": false,
        "is_current": false,
        "started_at": null,
        "completed_at": null,
        "extra": {"current_stage": "core_assets"}
      }
    ],
    "episodes": [
      {
        "chapter_id": "55555555-5555-5555-5555-555555555555",
        "episode_number": 1,
        "title": "第一集",
        "steps": []
      }
    ]
  },
  "timestamp": "2026-08-01T10:05:00+08:00"
}
```

实际响应中 `episodes[].steps` 与顶层 `steps` 字段结构相同，并固定返回四项。

### 2.1 状态枚举

| `status` | 含义 |
| --- | --- |
| `not_started` | 尚未进入 |
| `processing` | 后台处理中 |
| `waiting_review` | 等待用户审核或确认 |
| `completed` | 已完成 |
| `failed` | 处理失败，需要用户显式处理 |
| `invalidated` | 上游资产或结果变化，当前结果已失效 |

`current_step` 是项目第一条未完成步骤；四步都完成时返回 `4`。`can_view=true` 表示该步骤是
当前步骤或历史已完成步骤；前端不得为 `can_view=false` 的步骤建立可访问页面入口。

## 3. 后端强制门禁

后端不会只依赖前端隐藏页面。以下接口组会校验固定步骤顺序：

| 请求接口组 | 要求 |
| --- | --- |
| `/core-assets...` | 可以进入第 2 步，即第 1 步已完成 |
| `/storyboards...` | 可以进入第 3 步，即第 1、2 步已完成 |
| `/batch...` | 可以进入第 3 步，用于分镜组的视频任务 |
| `/review...`、`/episodes/.../video-timeline`、`/episodes/.../approve`、`/jianying-exports...` | 可以进入第 4 步，即前 3 步已完成 |

越级请求返回 HTTP 409：

```json
{
  "code": 40990,
  "message": "前置步骤尚未完成",
  "data": {
    "requested_step": 3,
    "required_step": 2,
    "current_step": 2,
    "required_status": "completed"
  }
}
```

历史已完成步骤允许查看；未来步骤不能通过直接输入 URL 或重复调用接口强行进入。
`automatic` 与 `supervised` 使用相同的硬门禁，模式只影响允许自动确认的业务节点，不改变
四步顺序。

## 4. 第二步无参考图确认

角色、场景和道具参考图是可选结果：用户可以使用系统生成图、自己上传图片，也可以暂时不提供
图片。`GET /core-assets/readiness` 中：

- `ready_count` 和 `missing_reference_count` 只表示参考图准备情况；
- `can_lock` 表示是否至少存在一个人物资产和一个场景资产；
- `can_lock=true` 时允许确认资产，即使所选资产的 `reference_image=null`；
- 锁定快照会保留 `reference_image=null`，第三步仍可绑定该资产并生成分镜。

## 5. 前端路由建议

进入 Agent 项目时先请求本接口，再按 `current_step` 打开默认页面。切换标签时只允许
`step_number <= current_step`，每次完成确认、生成或交付操作后重新查询本接口。不要根据
`current_stage` 自行拼接四步状态；`current_stage` 是后端内部细分阶段，可能继续扩展。
