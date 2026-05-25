# Story AI Backend

基础后端框架：

- FastAPI
- SQLAlchemy Async + PostgreSQL
- Alembic
- Redis
- Celery + Redis
- Aliyun OSS
- 统一 API 响应
- 默认使用北京时间 `Asia/Shanghai`

## 快速开始

```bash
cp .env.example .env
docker compose up -d postgres redis
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
alembic upgrade head
python -m uvicorn app.main:app --reload
```

对话生成已经走 Celery 异步任务，另开一个终端启动 worker：

```bash
source .venv/bin/activate
python -m celery -A app.worker.celery_app worker -l info -E --concurrency=2
```

查看 Celery 队列可以启动 Flower：

```bash
source .venv/bin/activate
python -m celery -A app.worker.celery_app flower --address=127.0.0.1 --port=5555 --basic_auth=admin:change-me
```

也可以使用项目脚本启动：

```bash
chmod +x scripts/celery_flower.sh
./scripts/celery_flower.sh
```

然后访问 `http://127.0.0.1:5555`。

也可以直接使用脚本创建虚拟环境、安装依赖并启动开发服务：

```bash
chmod +x scripts/dev.sh scripts/celery_worker.sh scripts/celery_flower.sh
./scripts/dev.sh
```

访问：

- API: `http://127.0.0.1:8000/api/v1/health`
- Docs: `http://127.0.0.1:8000/docs`

## 稳定性配置

项目已内置基础生产保护：

- Redis 固定窗口限流：全局、登录注册、上传、生成、任务轮询接口分别限流。任务轮询类 GET 不占用普通全局额度，避免正常查询任务结果时触发 429。
- Celery 任务超时：支持软超时和硬超时。
- Celery 任务重试：模型生成任务失败会有限重试，最终失败自动退回积分。
- 生产配置校验：非 local 环境会检查 JWT、Debug、CORS、OSS、模型服务和阿里云短信配置。

常用环境变量：

```env
RATE_LIMIT_ENABLED=true
RATE_LIMIT_GLOBAL_REQUESTS=300
RATE_LIMIT_AUTH_REQUESTS=30
RATE_LIMIT_UPLOAD_REQUESTS=30
RATE_LIMIT_GENERATION_REQUESTS=60
RATE_LIMIT_POLLING_REQUESTS=60
CELERY_TASK_TIME_LIMIT_SECONDS=300
CELERY_TASK_SOFT_TIME_LIMIT_SECONDS=240
CELERY_TASK_MAX_RETRIES=5
CELERY_TASK_RETRY_COUNTDOWN_SECONDS=30
CELERY_TASK_RETRY_BACKOFF_MAX_SECONDS=300
CELERY_RESULT_EXPIRES_SECONDS=86400
CELERY_WORKER_MAX_TASKS_PER_CHILD=50
PROVIDER_TASK_POLL_INTERVAL_SECONDS=10
PROVIDER_TASK_POLL_MAX_ATTEMPTS=60
PROVIDER_TASK_WORKER_POLL_INTERVAL_SECONDS=5
PROVIDER_TASK_WORKER_POLL_MAX_ATTEMPTS=3
USER_PENDING_TASK_LIMIT=20
USER_PENDING_MEDIA_TASK_LIMIT=5
USER_PENDING_TASK_WINDOW_HOURS=24
TASK_STALE_TIMEOUT_MINUTES=30
GENERATED_MEDIA_DOWNLOAD_MAX_SIZE_MB=500
GENERATED_MEDIA_TRANSFER_CONCURRENCY=3
GENERATED_MEDIA_CONNECT_TIMEOUT_SECONDS=30
GENERATED_MEDIA_READ_TIMEOUT_SECONDS=300
ALIYUN_SMS_ACCESS_KEY_ID=
ALIYUN_SMS_ACCESS_KEY_SECRET=
ALIYUN_SMS_REGION_ID=cn-hangzhou
ALIYUN_SMS_SIGN_NAME=
ALIYUN_SMS_REGISTER_TEMPLATE_CODE=
ALIYUN_SMS_REGISTER_TEMPLATE_PARAM_KEY=code
ALIYUN_SMS_CODE_TTL_SECONDS=300
ALIYUN_SMS_SEND_INTERVAL_SECONDS=60
ALIYUN_SMS_MOCK_ENABLED=false
# 微信支付 Native 扫码支付配置
WECHAT_PAY_APPID=已绑定商户号的小程序/公众号/AppID
WECHAT_PAY_MCHID=微信支付商户号
WECHAT_PAY_API_V3_KEY=微信支付 APIv3 密钥
WECHAT_PAY_MERCHANT_SERIAL_NO=商户 API 证书序列号
WECHAT_PAY_PRIVATE_KEY_PATH=key/apiclient_key.pem
WECHAT_PAY_PLATFORM_CERT_PATH=key/wechatpay_platform.pem
WECHAT_PAY_NOTIFY_URL=https://你的后端域名/api/v1/points/wechat/notify
WECHAT_PAY_TIMEOUT_SECONDS=30
WECHAT_PAY_NATIVE_EXPIRE_MINUTES=120
WECHAT_PAY_MOCK_ENABLED=false
```

下载微信支付平台证书：

```bash
python scripts/download_wechatpay_platform_cert.py
```

脚本会读取 `.env` 中的 `WECHAT_PAY_MCHID`、`WECHAT_PAY_MERCHANT_SERIAL_NO`、`WECHAT_PAY_PRIVATE_KEY_PATH`、`WECHAT_PAY_API_V3_KEY`，调用微信支付 `/v3/certificates`，解密后保存到 `WECHAT_PAY_PLATFORM_CERT_PATH`。

图像/视频生成属于长耗时任务。Worker 默认只短轮询厂商任务 `3 * 5` 秒，如果厂商还未完成，会把任务保持为 `running` 并释放 Celery 进程；前端继续通过任务记录接口轮询，后端会按 `PROVIDER_TASK_POLL_INTERVAL_SECONDS` 节流查询厂商结果，避免长视频持续占用 Worker。

任务从创建时间开始超过 `TASK_STALE_TIMEOUT_MINUTES` 分钟仍处于 `pending/running` 时，会在任务列表、任务详情或业务详情查询时自动判定为失败，并同步更新对话消息、项目章节、资产或分镜状态，已扣积分会自动退回。

为避免单个用户连续提交大量长耗时任务，后端会限制同一用户待处理任务数量：全部待处理任务默认最多 20 个，媒体类任务（图像、视频、资产图、分镜视频）默认最多 5 个。

生成结果转存 OSS 时使用流式下载，默认单个生成媒体最大 500MB、最多并发转存 3 个文件，避免大视频一次性读入内存。

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

管理员模型管理接口：

```http
GET    /api/v1/admin/models?keyword=&vendor=&model_type=&is_enabled=&page=1&page_size=20
POST   /api/v1/admin/models
GET    /api/v1/admin/models/provider/available
POST   /api/v1/admin/models/provider/import
GET    /api/v1/admin/models/{ai_model_id}
PATCH  /api/v1/admin/models/{ai_model_id}
DELETE /api/v1/admin/models/{ai_model_id}
Authorization: Bearer <admin_access_token>
```

创建模型：

```json
{
  "nickname": "GPT-4.1",
  "model_id": "gpt-4.1",
  "vendor": "openai",
  "model_type": "chat",
  "remark": "通用对话模型",
  "points_cost": 10,
  "is_enabled": true
}
```

获取厂商可用模型前，需要在 `.env` 中配置：

```env
GPT_BEST_BASE_URL=https://your-provider-base-url
GPT_BEST_API_KEY=your-api-key
```

导入厂商模型：

```json
{
  "models": [
    {
      "model_id": "gpt-4.1",
      "nickname": "GPT-4.1",
      "vendor": "gpt-best",
      "model_type": "text",
      "remark": "厂商模型导入",
      "points_cost": 10,
      "is_enabled": true
    }
  ]
}
```

通用模型接口：

```http
GET /api/v1/models/options?vendor=&model_type=
GET /api/v1/models/{ai_model_id}
```

通用模型接口只返回已启用模型。

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
  "ai_model_id": "本次使用的模型 UUID",
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
  "ai_model_id": "模型 UUID",
  "conversation_type": "image"
}
```

发送消息可传 `ai_model_id` 切换本次使用的模型；不传则沿用会话当前模型。接口会校验模型是否启用、模型类型是否和会话类型一致，并按模型 `points_cost` 扣减用户积分。接口会立即创建用户消息、处理中助手消息和任务记录，然后把真实 Comfly 调用交给 Celery worker 执行。执行成功后 worker 会更新助手消息和任务记录，执行失败会记录失败原因并自动退回本次积分。

## Celery

```bash
python -m celery -A app.worker.celery_app worker -l info -E --concurrency=2
```

或使用脚本：

```bash
./scripts/celery_worker.sh
```

## Flower

Flower 已拆分为独立监控依赖和启动脚本，用来查看 Celery 队列、任务状态、失败原因和 worker 状态。

Flower 是独立监控服务，不参与主 API 启动校验，也不使用主 `.env` 中的 `FLOWER_*` 配置。需要自定义 Flower 监听地址、端口或账号密码时，使用独立环境变量：

```env
STORY_AI_FLOWER_ADDRESS=127.0.0.1
STORY_AI_FLOWER_PORT=5555
STORY_AI_FLOWER_BASIC_AUTH=admin:change-me
```

启动：

```bash
pip install -r requirements-flower.txt
./scripts/celery_flower.sh
```

服务器部署可使用 `data/story-ai-flower.service`。该服务默认读取可选配置文件 `/etc/story-ai-flower.env`，建议通过 Nginx 做内网反代或只监听 `127.0.0.1`，不要直接把 Flower 暴露到公网。

## 目录

```text
app/
  api/          API 路由
  core/         配置、响应、异常、时间、日志
  db/           SQLAlchemy 会话与 Base
  integrations/ Redis / OSS 客户端
  schemas/      通用响应结构
  services/     业务服务
  tasks/        Celery 任务
alembic/        数据库迁移
```

当前没有创建业务模型，后续可以在 `app/models/` 与 `app/schemas/` 中按业务逐步添加。
