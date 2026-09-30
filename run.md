以下步骤适用于使用 Homebrew 管理本机服务的 macOS 开发环境，需要先安装：

- Python 3.12+
- `uv`
- PostgreSQL 16
- Redis
- `ffprobe`（媒体元数据探测使用）

项目路径用 `/path/to/story-ai` 表示，执行时替换为实际路径。Docker 启动方式及配置说明见 [README](README.md)，独立数据库回归见[测试说明](docs/测试与回归.md)。

## 1. 启动 PostgreSQL 和 Redis

```bash
brew services restart postgresql@16
brew services restart redis
```

检查：

```bash
pg_isready -h 127.0.0.1 -p 5432
redis-cli -h 127.0.0.1 -p 6379 ping
```

Redis 应返回：

```text
PONG
```

## 2. 安装依赖并迁移数据库

```bash
cd /path/to/story-ai
test -f .env || cp .env.example .env

uv sync --extra dev --frozen
uv run --frozen alembic upgrade head
```

迁移前确认 `.env` 中的 `POSTGRES_*` 指向开发数据库，且数据库和账号已创建。`uv sync --extra dev --frozen` 安装锁定版本及开发依赖。

## 3. 启动后端 API

终端一执行：

```bash
cd /path/to/story-ai

uv run --frozen uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  --reload
```

也可以使用现有脚本：

```bash
./scripts/dev.sh
```

访问：

- 健康检查：[http://127.0.0.1:8000/api/v1/health](http://127.0.0.1:8000/api/v1/health)
- Swagger 文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

## 4. 启动 Celery Worker

Agent 分析、图像生成、视频生成都是异步任务，因此必须启动 Worker。

终端二执行：

```bash
cd /path/to/story-ai

uv run --frozen celery -A app.worker.celery_app worker \
  -l info \
  -E \
  --concurrency=2 \
  --queues=celery,story_ai_default,story_ai_text,story_ai_image,story_ai_video,story_ai_delivery,story_ai_query,story_ai_media
```

这个命令适合本地开发，只启动一个 Worker 并监听全部队列。

如果需要模拟生产环境的多 Worker，也可以运行：

```bash
./scripts/celery_workers.sh
```

但该脚本默认并发较高，本地开发不建议优先使用。

## 5. 启动 Celery Beat

后端还必须启动 Celery Beat。Beat 每 10 秒补偿已提交数据库但尚未成功投递的任务，并定期扫描第三方结果回收、Agent 自动推进及待结算对话任务。升级 OPT-12 时应先运行 `alembic upgrade head` 创建 0062 待投递表，再启动新版 API、Worker 和 Beat。

终端三执行：

```bash
cd /path/to/story-ai
./scripts/celery_beat.sh
```

使用`./scripts/celery_workers.sh`时会自动同时启动 Beat，不需要再单独启动。

## 6. 可选：启动 Flower

终端四执行：

```bash
cd /path/to/story-ai

uv sync --extra monitor --frozen
./scripts/celery_flower.sh
```

访问：

[http://127.0.0.1:5555](http://127.0.0.1:5555)

## 本地完整启动顺序

以后每次本地启动，只需要：

```bash
brew services start postgresql@16
brew services start redis
```

然后打开三个终端：

```bash
# 终端一：API
cd /path/to/story-ai
uv run --frozen uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

```bash
# 终端二：Celery
cd /path/to/story-ai
uv run --frozen celery -A app.worker.celery_app worker \
  -l info -E --concurrency=2 \
  --queues=celery,story_ai_default,story_ai_text,story_ai_image,story_ai_video,story_ai_delivery,story_ai_query,story_ai_media
```

```bash
# 终端三：任务补偿调度
cd /path/to/story-ai
./scripts/celery_beat.sh
```

API 可以单独启动，但如果不启动 Celery Worker，Agent 分析和媒体生成任务只会进入等待状态；如果不启动 Celery Beat，待投递任务及丢失的第三方结果回收消息无法被定期补偿。


### 异步媒体回收与转存 Worker

生成结果回收已分离到 `story_ai_query`（厂商查询）和 `story_ai_media`（成品转存）。`scripts/celery_workers.sh` 会启动两个独立 Worker，默认查询并发 4、转存并发 2。上面的单 Worker 全队列命令仅适合本地调试，不能提供队列之间的执行隔离。

systemd 部署需一并安装并启动 `data/story-ai-celery-query.service` 和 `data/story-ai-celery-media.service`，并重启原 Worker、API 与 Beat；只重启原默认 Worker 会导致新增队列无人消费。全局查询上限由 `PROVIDER_QUERY_MAX_CONCURRENCY`、`PROVIDER_QUERY_REQUESTS_PER_SECOND` 控制，与 Worker 数量独立。转存并发按所有实例相加，新增部署副本时需要同步核算。

此改造复用已有任务 extra 和 outbox 表，不增加数据库迁移。滚动发布时应先停止旧版 Worker 并等待正在执行的任务结束，再一起启用新版 Worker，避免旧代码消费新阶段任务；不得清空队列或任务表。
