# Story AI Backend

AI 短剧项目后端，包含文本/图像/视频对话、项目无限画布与节点生成、Agent 整剧制作、生成历史、作品展示、积分充值和管理端功能。

技术组成：

- FastAPI
- SQLAlchemy Async + PostgreSQL
- Alembic
- Redis
- Celery + Redis
- Aliyun OSS
- 统一 API 响应
- 默认使用北京时间 `Asia/Shanghai`

## 文档入口

- [模块职责与任务事务](docs/后端架构与事务.md)
- [测试与回归](docs/测试与回归.md)
- [接口文档分类目录](docs/README.md)
- 0.x 通用：[0.0 通用接口](docs/0.0通用接口文档.md)、[0.1 全量接口与请求字段索引](docs/0.1全量接口与请求字段索引.md)
- 1.x 对话型：[1.0 对话接口](docs/1.0对话型接口文档.md)
- 2.x 项目型：[2.0 项目接口](docs/2.0项目型接口文档.md)、[2.1 画布接口](docs/2.1项目画布接口.md)、[2.2 前端接入指南](docs/2.2项目画布前端接入指南.md)
- 3.x Agent 型：[3.0 Agent 接口](docs/3.0Agent接口文档.md)
- 4.x 作品展示：[4.0 作品接口](docs/4.0作品展示接口文档.md)
- 5.x 管理端：[5.0 管理端接口](docs/5.0管理端整体接口文档.md)
- [本地运行](run.md)、[优化计划与验收记录](优化计划/计划表.md)

请求声明可在当前应用的 `/docs` 和 `/openapi.json` 查看；条件校验、响应过滤与业务状态以对应接口文档及服务代码为准，部分路由没有声明完整的 OpenAPI 响应模型。以下示例中的模型 ID、UUID、密钥和业务数据均需替换。

## 快速开始

需要 Python 3.12+、`uv`、PostgreSQL 和 Redis；仓库 Compose 使用 PostgreSQL 16、Redis 7。媒体元数据探测调用 `ffprobe`，需在 API/Worker 的执行环境中安装该命令。

```bash
test -f .env || cp .env.example .env
docker compose up -d postgres redis
uv sync --extra dev --frozen
uv run --frozen alembic upgrade head
uv run --frozen uvicorn app.main:app --reload
```

已有本机 PostgreSQL/Redis 时，可使用 [run.md](run.md) 的启动方式。数据库迁移使用 `.env` 中的连接，先确认 `POSTGRES_*` 指向要运行的开发库。

对话、项目制作和 Agent 生成通过 Celery 执行。另开终端启动 Worker 和 Beat：

```bash
source .venv/bin/activate
./scripts/celery_workers.sh
```

`celery_workers.sh`会同时启动 Celery Beat，用于补偿待投递任务、第三方结果回收和 Agent 自动推进。如果只启动单个 Worker，还需要在独立终端启动：

```bash
./scripts/celery_beat.sh
```

也可以按队列分别启动 worker：

```bash
python -m celery -A app.worker.celery_app worker -l info -E --concurrency=2 --queues=celery,story_ai_default
python -m celery -A app.worker.celery_app worker -l info -E --concurrency=16 --queues=story_ai_text --hostname=story-ai-text@%h
python -m celery -A app.worker.celery_app worker -l info -E --concurrency=16 --queues=story_ai_image --hostname=story-ai-image@%h
python -m celery -A app.worker.celery_app worker -l info -E --concurrency=10 --queues=story_ai_video --hostname=story-ai-video@%h
python -m celery -A app.worker.celery_app worker -l info -E --concurrency=1 --queues=story_ai_delivery --hostname=story-ai-delivery@%h
```

监控任务可按下方 [Flower](#flower) 说明启动。`celery_workers.sh` 中的并发和队列参数读取 shell 环境变量；systemd 部署则由服务文件的 `EnvironmentFile` 注入，不能仅依靠 Python 读取 `.env` 来设置这些 shell 参数。

也可以直接使用脚本创建虚拟环境、安装依赖并启动开发服务：

```bash
chmod +x scripts/dev.sh scripts/celery_worker.sh scripts/celery_beat.sh scripts/celery_flower.sh
./scripts/dev.sh
```

访问：

- API: `http://127.0.0.1:8000/api/v1/health`
- Docs: `http://127.0.0.1:8000/docs`

## 依赖管理

- `pyproject.toml` 是直接依赖及可选依赖的唯一契约。
- `uv.lock` 锁定实际安装版本，本地和生产启动统一使用 `uv --frozen`。
- `requirements*.txt` 仅用于兼容仍使用 `pip` 的环境，修改依赖时必须与
  `pyproject.toml` 同步；测试会自动检查两者是否一致。

## 配置与异步运行

应用通过 [Settings](app/core/config.py) 读取 `.env`，环境变量覆盖文件配置。完整示例见 [.env.example](.env.example)。

- 数据库连接由 `POSTGRES_HOST/PORT/USER/PASSWORD/DB` 生成；`DATABASE_URL` 不是应用配置字段。
- Redis 使用 `REDIS_*`；Celery broker/result 分别使用 `CELERY_BROKER_DB` 和 `CELERY_RESULT_DB` 指定的 Redis 库。
- `COMFLY_*`、`APIMART_*`、`VOLCENGINE_ARK_*` 配置对应模型接入；管理员创建模型时使用相应的 `vendor`。
- OSS 使用 `OSS_*`，注册验证码使用 `ALIYUN_SMS_*`，微信 Native 充值使用 `WECHAT_PAY_*`。凭证填写在运行环境，不写入代码或文档。
- 非开发/测试环境检查 JWT、Debug、CORS 和 mock 开关；启用 `ENFORCE_PRODUCTION_CONFIG` 时还检查模型接入、OSS、短信及支付配置，具体以 [startup.py](app/core/startup.py) 为准。

代码缺省值与 `.env.example` 中的示例值有区别：

| 配置 | 代码缺省值 | `.env.example` 示例值 |
| --- | --- | --- |
| `PROVIDER_TASK_POLL_INTERVAL_SECONDS` | 2 | 2 |
| `USER_PENDING_TASK_LIMIT` / `USER_PENDING_MEDIA_TASK_LIMIT` | 20 / 5 | 40 / 12 |
| `GENERATED_MEDIA_TRANSFER_CONCURRENCY` | 3 | 8 |
| `MAX_UPLOAD_SIZE_MB` | 100 | 300 |

普通对话、项目画布及 Agent 的资产图、分镜图和分镜视频，取得厂商任务 ID 后均交给同一个后台回收模块查询、转存结果和结算，生成 Worker 随即释放。厂商状态查询走独立 `story_ai_query` 队列，厂商完成后的媒体转存走独立 `story_ai_media` 队列。结果与转存 outbox 同事务保存，转存失败复用已保存的结果。首次回收立即入队；厂商尚未完成时，按 `PROVIDER_TASK_POLL_INTERVAL_SECONDS`（默认 2 秒，最低 1 秒）继续查询。已到期任务重新投递时立即查询，不重新等待一个完整周期。文本流式输出及直接返回成品的同步模型继续在原 Worker 完成。

查询受 Redis 全局预算约束：同一接入厂商/地址/密钥作用域默认最多 4 个在途请求、每秒 5 次查询；这不是每个 Worker 单独的额度。查询超时独立为 10 秒，故障按指数退避（本地上限 60 秒），厂商 Retry-After 可以要求更长等待。Redis 故障时暂缓查询，不绕过预算。正常 2 秒查询间隔是资源允许时的调度目标，不是高并发下的检测时延保证。

启动脚本已增加独立查询 Worker（默认并发 4）和转存 Worker（默认并发 2）；systemd 模板见 `data/story-ai-celery-query.service`、`data/story-ai-celery-media.service`。转存并发需要按所有实例的进程数与单任务媒体并发合计，不属于全局 Redis 查询额度。同步返回成品的模型仍在原生成 Worker 转存，文本流式路径保留。

平台活动任务的客户端 `next_poll_seconds=1`，终态为 `null`，不采用历史 extra 中较长的提示。对话与多任务 SSE 通过 Redis 变化通知唤醒，默认每 15 秒补读数据库快照；每用户默认最多 3 个 SSE 连接。客户端查平台不会直接请求厂商；前端应合并 batch 请求、使用最新提示，终态立即更新结果并停止轮询。

部署时将已有环境中的 `PROVIDER_TASK_POLL_INTERVAL_SECONDS` 改为 `2`，重启 API、全部 Worker 和 Beat。旧 `PROVIDER_TASK_IMAGE_POLL_INTERVAL_SECONDS`、`PROVIDER_TASK_VIDEO_POLL_INTERVAL_SECONDS`、`PROVIDER_TASK_WORKER_POLL_*`、`PROVIDER_TASK_POLL_MAX_ATTEMPTS` 已删除，不再生效。缩短间隔会增加厂商状态查询次数；不支持或尚未接入完成回调的链路仍存在查询检测延迟，端到端还包含队列、网络和 OSS 转存耗时，不能保证与厂商生成时间完全相同。

前端按任务响应中的 `next_poll_seconds` 查询状态。任务与待投递记录在同一数据库事务提交，队列异常时保留待投递状态；Beat 每 10 秒补偿未投递消息。另定期补偿结果回收、自动 Agent 调度及待结算文本任务；结果回收、待结算补偿扫描间隔为 180 秒（仅用于丢失投递等异常恢复，正常任务由上述即时/2 秒调度推进）。实例、队列和调度配置见 [celery_app.py](app/core/celery_app.py)，任务注册和运行钩子见 [worker.py](app/worker.py)；启动入口仍为 `app.worker.celery_app`。升级到 OPT-12 时先执行 `alembic upgrade head` 创建待投递表，再启动新版 API、Worker 和 Beat。

未取得厂商任务 ID 的 `pending/running` 任务可按 `TASK_STALE_TIMEOUT_MINUTES`（默认 30 分钟）判定超时并退款；已有厂商任务 ID 的任务交给结果回收，避免长视频被通用超时误退。重试规则、回收租约和事务说明见[架构文档](docs/后端架构与事务.md)。

限流按全局、认证、上传、生成、任务轮询分类；轮询 GET 使用独立额度。任务软/硬超时还会结合厂商 HTTP 超时和 `CELERY_TASK_TIMEOUT_GRACE_SECONDS` 计算，配置值不一定等于最终生效值。

生成媒体通过流式下载转存 OSS，单文件上限由 `GENERATED_MEDIA_DOWNLOAD_MAX_SIZE_MB` 控制，默认 500MB。作品容量及未引用暂存文件清理分别使用 `USER_WORK_STORAGE_LIMIT_MB`、`UNUSED_WORK_UPLOAD_TTL_HOURS`。

微信支付平台证书下载入口：

```bash
.venv/bin/python scripts/download_wechatpay_platform_cert.py
```

该脚本读取 `WECHAT_PAY_*` 配置并请求微信支付平台证书；支付配置示例仅在 `.env.example` 中维护。

## 认证接口

注册：

```http
POST /api/v1/auth/register/sms-code
POST /api/v1/auth/register
```

注册必须先发送手机验证码，并在注册请求中提交 `phone` 和 `sms_code`。

```json
{
  "account": "demo",
  "password": "123456",
  "nickname": "Demo",
  "phone": "13800000000",
  "sms_code": "123456",
  "email": "demo@example.com"
}
```

登录支持账号、手机号、邮箱：

```http
POST /api/v1/auth/login
```

```json
{
  "identifier": "demo",
  "password": "123456"
}
```

获取当前用户：

```http
GET /api/v1/auth/me
Authorization: Bearer <access_token>
```

修改当前用户资料：

```http
PATCH /api/v1/users/me/profile
Authorization: Bearer <access_token>
```

```json
{
  "nickname": "新的昵称",
  "avatar": "https://example.com/avatar.png"
}
```

## 管理员用户管理

所有管理员接口都需要：

```http
Authorization: Bearer <admin_access_token>
```

接口：

```http
GET    /api/v1/admin/users?keyword=&is_enabled=&page=1&page_size=20
GET    /api/v1/admin/users/{user_id}
PATCH  /api/v1/admin/users/{user_id}
PATCH  /api/v1/admin/users/{user_id}/password
DELETE /api/v1/admin/users/{user_id}
```

更新用户：

```json
{
  "account": "demo",
  "nickname": "Demo",
  "avatar": "https://example.com/avatar.png",
  "phone": "13800000000",
  "email": "demo@example.com",
  "is_admin": false,
  "is_enabled": true
}
```

重置密码：

```json
{
  "password": "new-password"
}
```

## 用户积分

用户查询自己的积分：

```http
GET /api/v1/points/balance
GET /api/v1/points/transactions?page=1&page_size=20
Authorization: Bearer <access_token>
```

用户创建微信 Native 扫码充值订单，`1 元 = 10 积分`，前端使用返回的 `code_url` 生成二维码：

```http
POST /api/v1/points/recharges
Authorization: Bearer <access_token>
```

```json
{
  "amount_yuan": 10
}
```

用户查看自己的充值记录：

```http
GET /api/v1/points/recharges?page=1&page_size=20
GET /api/v1/points/recharges/{order_id}
Authorization: Bearer <access_token>
```

管理员调整用户积分，`amount` 为正数表示增加，为负数表示扣减：

```http
POST /api/v1/admin/users/{user_id}/points
Authorization: Bearer <admin_access_token>
```

```json
{
  "amount": 100,
  "remark": "后台充值"
}
```

管理员查看指定用户积分流水：

```http
GET /api/v1/admin/users/{user_id}/points/transactions?page=1&page_size=20
Authorization: Bearer <admin_access_token>
```

管理员查看平台充值记录和消耗记录：

```http
GET /api/v1/admin/point-records?page=1&page_size=20
GET /api/v1/admin/point-records/recharges?page=1&page_size=20
GET /api/v1/admin/point-records/consumes?page=1&page_size=20
Authorization: Bearer <admin_access_token>
```

管理员查看所有充值记录和发起退款：

```http
GET /api/v1/admin/recharges?page=1&page_size=20
GET /api/v1/admin/recharges/{order_id}
POST /api/v1/admin/recharges/{order_id}/sync
POST /api/v1/admin/recharges/{order_id}/refund
Authorization: Bearer <admin_access_token>
```

平台模型消耗积分时，可在业务服务中调用 `consume_user_points(db, user_id, amount, remark)`。

## 模型管理

管理员接口需要 `Authorization: Bearer <admin_access_token>`：

```http
GET    /api/v1/admin/models?keyword=&vendor=&model_type=&is_enabled=&page=1&page_size=20
POST   /api/v1/admin/models
GET    /api/v1/admin/models/provider/available?vendor=comfly&model_type=text
POST   /api/v1/admin/models/provider/import
GET    /api/v1/admin/models/{ai_model_id}
GET    /api/v1/admin/models/{ai_model_id}/billing-recommendation
PATCH  /api/v1/admin/models/{ai_model_id}
DELETE /api/v1/admin/models/{ai_model_id}
```

模型类型为 `text`、`image`、`video`；厂商为 `comfly`、`apimart`、`volcengine_ark`。Ark 当前只接入视频；Apimart 图像/视频模型需要匹配仓库支持的型号。实际支持能力由接口返回的 `configuration.request.capabilities` 描述。

创建文本模型示例：

```json
{
  "nickname": "文本模型示例",
  "model_id": "provider-text-model-id",
  "vendor": "comfly",
  "model_type": "text",
  "remark": "替换为已接入厂商的模型 ID",
  "is_enabled": true,
  "is_agent_default": false,
  "configuration": {
    "version": 1,
    "request": {"capabilities": {}},
    "billing": {
      "base_points": 10,
      "multipliers": {"model": "1", "cache": "1", "completion": "1", "platform": "1"},
      "policy": {}
    },
    "operations": {"status": "active", "maintenance_message": null}
  }
}
```

`configuration` 是统一的请求能力、计费和运行状态配置；模型创建/更新不再接受顶层 `points_cost`、`capabilities` 等旧字段。`PATCH` 合并配置分区，`operations.status` 可为 `active` 或 `maintenance`。

计费含义：

- 文本提交预扣为 0，`billing.base_points` 是最低余额门槛；完成后根据厂商成本或 token usage 结算。
- 图像按配置的图片策略或基础积分收费；视频按配置、参考类型、分辨率和时长计算，并在完成后结算。
- Apimart 文本/视频优先使用响应中的厂商成本；图片对用户仍按管理端配置收费。前端显示接口返回的积分，不自行按 `base_points` 复算。

厂商接入配置示例（占位值）：

```env
COMFLY_BASE_URL=https://model-provider.example/v1
COMFLY_API_KEY=<provider-api-key>
```

Apimart 和 Ark 使用 `.env.example` 中各自的配置项。厂商查询接口会调用对应外部服务；先配置凭证，再获取可用模型并导入：

```json
{
  "models": [
    {
      "model_id": "provider-text-model-id",
      "nickname": "文本模型示例",
      "vendor": "comfly",
      "model_type": "text",
      "is_enabled": true,
      "configuration": {"billing": {"base_points": 10}}
    }
  ]
}
```

通用模型接口：

```http
GET /api/v1/models/options?vendor=&model_type=
GET /api/v1/models/{ai_model_id}
```

用户模型列表只返回已启用且不处于维护状态的模型。完整字段、计费策略和默认模型规则见[管理端接口文档](docs/5.0管理端整体接口文档.md)。

## 风格管理

管理员风格管理接口：

```http
GET    /api/v1/admin/styles?keyword=&is_enabled=&page=1&page_size=20
POST   /api/v1/admin/styles
GET    /api/v1/admin/styles/{style_id}
PATCH  /api/v1/admin/styles/{style_id}
DELETE /api/v1/admin/styles/{style_id}
Authorization: Bearer <admin_access_token>
```

创建风格：

```json
{
  "name": "电影感写实",
  "cover": "https://example.com/style-cover.png",
  "prompt": "cinematic, realistic, high detail",
  "version": "v1",
  "is_enabled": true
}
```

通用风格接口只返回已启用风格：

```http
GET /api/v1/styles
GET /api/v1/styles/{style_id}
```

## 素材库管理

素材库用于维护预设图像资源。图片文件上传到 OSS 的 `{OSS_ROOT_DIRECTORY}/material` 目录，接口不会向用户端暴露 OSS 原始地址；用户端拿到的 `image_url` 是需要登录鉴权的后端代理取图地址。

用户端素材接口：

```http
GET /api/v1/materials?keyword=&category=&page=1&page_size=20
GET /api/v1/materials/categories
GET /api/v1/materials/{material_id}
GET /api/v1/materials/{material_id}/image
```

用户端列表和详情只返回已启用素材。`/image` 接口返回图像二进制流，需要携带 Bearer token；前端应使用授权 `fetch` 获取 Blob 后再生成临时预览地址。

用户端素材返回字段：

```json
{
  "id": "素材 UUID",
  "name": "图像名",
  "category": "分类",
  "description": "图像描述",
  "tags": ["标签1", "标签2"],
  "image_url": "/api/v1/materials/{material_id}/image",
  "filename": "example.png",
  "content_type": "image/png",
  "size": 102400,
  "sort_order": 0
}
```

管理端素材接口：

```http
GET    /api/v1/admin/materials?keyword=&category=&is_enabled=&page=1&page_size=20
POST   /api/v1/admin/materials
GET    /api/v1/admin/materials/{material_id}
PATCH  /api/v1/admin/materials/{material_id}
DELETE /api/v1/admin/materials/{material_id}
GET    /api/v1/admin/materials/{material_id}/image
Authorization: Bearer <admin_access_token>
```

创建素材使用 `multipart/form-data`：

```text
file        图像文件，必填，仅支持 image/*
name        图像名，必填，最长 128
category    分类，必填，最长 64
description 图像描述，选填
tags        标签，选填，支持英文逗号分隔或 JSON 数组字符串
sort_order  排序值，选填，默认 0
is_enabled  是否启用，选填，默认 true
```

示例：

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/admin/materials" \
  -H "Authorization: Bearer <admin_access_token>" \
  -F "file=@/path/to/example.png" \
  -F "name=森林背景" \
  -F "category=scene" \
  -F "description=适合作为童话森林场景" \
  -F "tags=森林,背景,自然" \
  -F "sort_order=10" \
  -F "is_enabled=true"
```

更新素材也使用 `multipart/form-data`，所有字段均可选；传 `file` 时会替换素材图像，不传则只更新元数据。删除素材为软删除，会把 `is_enabled` 置为 `false`。

管理端返回比用户端多以下字段：

```json
{
  "image_object_key": "story/material/xxxx.png",
  "is_enabled": true,
  "created_at": "2026-05-29T18:30:00+08:00",
  "updated_at": "2026-05-29T18:30:00+08:00"
}
```

## 对话型业务

对话接口需要登录：

```http
GET  /api/v1/conversations?page=1&page_size=20
POST /api/v1/conversations
GET  /api/v1/conversations/{conversation_id}
GET  /api/v1/conversations/{conversation_id}/messages?page=1&page_size=50
POST /api/v1/conversations/{conversation_id}/messages
Authorization: Bearer <access_token>
```

发送消息：

```json
{
  "content": "帮我写一个短故事",
  "ai_model_id": "00000000-0000-4000-8000-000000000001",
  "client_message_id": "example-text-turn-001",
  "extra": {}
}
```

对话型业务支持三类：

- `text`：文本生成
- `image`：图像生成
- `video`：视频生成

创建会话：

```json
{
  "title": "新的图像生成",
  "ai_model_id": "00000000-0000-4000-8000-000000000002",
  "conversation_type": "image"
}
```

发送消息可传 `ai_model_id` 切换本次模型，类型必须与会话相同；省略时使用会话当前模型。文本请求可用 `client_message_id` 做幂等重试，同一标识不得用于不同内容。文本历史由后端构造，不接受客户端传入 `extra.messages`。

接口创建用户消息、处理中助手消息和任务记录，提交事务后交给 Celery；响应中的 `points_cost` 是提交阶段金额，文本为 0。Worker 调用相应厂商并完成结果更新、媒体转存和积分结算，失败时按任务记录退回实际已扣金额。

查询平台任务：

```http
GET /api/v1/conversations/{conversation_id}/generation-tasks/{task_record_id}
GET /api/v1/conversations/{conversation_id}/generation-tasks/{task_record_id}/stream
```

平台 `task_record_id` 与厂商任务 ID 是不同标识。状态查询、SSE、重试和视频模式的完整约定见[对话接口文档](docs/1.0对话型接口文档.md)。

## 项目与 Agent 制作

普通项目仅提供无限画布、项目媒体库和节点生成；0067 将旧内容转换为画布并移除普通项目章节接口。独立 Agent 项目继续使用内部章节/分镜存储，提供来源剧本分析、资产锁定、分集规划、审核和自动推进。

普通项目的读取和编辑限制 `project_kind == "standard"`；Agent 内部读取使用专门的所属用户及风格校验入口。资产绑定、审核与历史选中逻辑分别保留在对应业务模块。接口入口见[项目文档](docs/2.0项目型接口文档.md)和 [Agent 文档](docs/3.0Agent接口文档.md)。

## Flower

Flower 已拆分为独立监控依赖和启动脚本，用来查看 Celery 队列、任务状态、失败原因和 worker 状态。

Flower 是独立监控服务，不参与主 API 启动校验，也不使用主 `.env` 中的 `FLOWER_*` 配置。需要自定义 Flower 监听地址、端口或账号密码时，使用独立环境变量：

```env
STORY_AI_FLOWER_ADDRESS=127.0.0.1
STORY_AI_FLOWER_PORT=5555
STORY_AI_FLOWER_BASIC_AUTH=<monitor-user>:<monitor-password>
```

启动：

```bash
uv sync --extra monitor --frozen
./scripts/celery_flower.sh
```

服务器部署可使用 `data/story-ai-flower.service`。该服务默认读取可选配置文件 `/etc/story-ai-flower.env`，建议通过 Nginx 做内网反代或只监听 `127.0.0.1`，不要直接把 Flower 暴露到公网。

## 目录

```text
app/
  api/           HTTP 路由、认证依赖和响应组装
  core/          配置（含 Celery 实例/队列/调度）、异常、日志、限流、时间及公共媒体解析
  db/            SQLAlchemy 会话与 Base
  integrations/  模型厂商、Redis、OSS、短信及微信支付接入
  models/        持久化业务模型
  schemas/       请求与响应结构
  services/
    conversation/  对话事务、文本上下文、视频输入准备
    projects/      项目生命周期、画布/媒体/节点生成；Agent 复用的内部制作服务
    agent/         Agent 制作、审核、来源文档及资产锁定
    generation/    模型执行、任务管理、厂商回收及结果落库
    models/        模型目录与请求配置
    billing/       积分账本、充值、计费策略与结算
    ...            认证、上传、素材、作品等独立业务
  tasks/         已发布的 Celery 执行入口及共享重试规则
  prompts/       系统提示词及固定资产提示词
  worker.py      Celery 启动入口、任务注册及运行钩子
alembic/         数据库迁移
scripts/         开发启动、监控和独立数据库测试入口
tests/           单元、接口及 PostgreSQL 集成测试
docs/            接口、架构和测试说明
优化计划/        各项优化与验收记录
```

业务直接导入具体叶子模块。六个业务子包的 `__init__.py` 只说明职责，旧平铺模块路径不再保留。依赖方向、任务锁与提交位置见[架构与事务](docs/后端架构与事务.md)。

分镜制作分为四个模块：`projects/storyboards.py` 负责读取、编辑和排序，`storyboard_submission.py` 负责生成任务提交与输入准备，`storyboard_execution.py` 负责 Worker 执行及结果落库，`storyboard_parsing.py` 负责结果解析和内容规范化。路由、Agent 编排与 Celery 任务直接导入对应模块。

Agent 工作台复用一次监控上下文生成矩阵和费用；独立费用查询只读取任务类型、积分及退款标记。矩阵和异常列表排除大段正文/提示词加载，查询成本及统计兼容性见 [OPT-16 对比记录](优化计划/OPT-16查询成本.md)。

## 测试

```bash
uv sync --extra dev --frozen
.venv/bin/ruff check app tests scripts --no-cache
RUN_DB_INTEGRATION_TESTS=0 .venv/bin/pytest -m 'not integration' -q -p no:cacheprovider
```

数据库测试必须显式指定独立测试库（库名以 `_test` 结尾）：

```bash
export TEST_DATABASE_URL='postgresql+asyncpg://<test-user>:<test-password>@127.0.0.1:<test-port>/story_ai_test'
.venv/bin/python scripts/run_integration_tests.py
```

测试配置独立初始化，不读取开发者 `.env` 或继承业务连接；API/Worker 使用显式测试库。上述命令包含 Alembic 迁移检查，默认使用内存队列。安装 `redis-server` 后，追加 `--with-redis` 可运行真实 Redis、Worker、Beat 恢复测试；外部厂商仍使用受控替身。

[GitHub Actions](.github/workflows/backend-tests.yml) 已配置 Ruff、非数据库及完整数据库/真实队列回归，并保留测试报告和进程日志。环境准备、选择用例及验证范围见[测试与回归](docs/测试与回归.md)。

本轮设计、验收与部署说明见[生成任务响应与负载优化计划](优化计划/生成任务响应与负载优化计划.md)。
