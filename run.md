你当前是 macOS，本机已经安装了：

- Python 3.12
- `uv`
- PostgreSQL 16
- Redis
- 项目 `.env` 也已经存在

不需要 Docker，按下面启动即可。

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
cd /Users/haozi/Desktop/story/story-ai

uv sync --extra dev --frozen
uv run --frozen alembic upgrade head
```

`uv sync` 这一步不能跳过，否则可能再次出现：

```text
ModuleNotFoundError: No module named 'nh3'
```

## 3. 启动后端 API

终端一执行：

```bash
cd /Users/haozi/Desktop/story/story-ai

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
cd /Users/haozi/Desktop/story/story-ai

uv run --frozen celery -A app.worker.celery_app worker \
  -l info \
  -E \
  --concurrency=2 \
  --queues=celery,story_ai_default,story_ai_text,story_ai_image,story_ai_video,story_ai_delivery
```

这个命令适合本地开发，只启动一个 Worker 并监听全部队列。

如果需要模拟生产环境的多 Worker，也可以运行：

```bash
./scripts/celery_workers.sh
```

但该脚本默认并发较高，本地开发不建议优先使用。

## 5. 启动 Celery Beat

后端还必须启动 Celery Beat。Beat 负责定期扫描已提交给第三方、但单次回收消息丢失的任务。

终端三执行：

```bash
cd /Users/haozi/Desktop/story/story-ai
./scripts/celery_beat.sh
```

使用`./scripts/celery_workers.sh`时会自动同时启动 Beat，不需要再单独启动。

## 6. 可选：启动 Flower

终端四执行：

```bash
cd /Users/haozi/Desktop/story/story-ai

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
cd /Users/haozi/Desktop/story/story-ai
uv run --frozen uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

```bash
# 终端二：Celery
cd /Users/haozi/Desktop/story/story-ai
uv run --frozen celery -A app.worker.celery_app worker \
  -l info -E --concurrency=2 \
  --queues=celery,story_ai_default,story_ai_text,story_ai_image,story_ai_video,story_ai_delivery
```

```bash
# 终端三：任务补偿调度
cd /Users/haozi/Desktop/story/story-ai
./scripts/celery_beat.sh
```

API 可以单独启动，但如果不启动 Celery Worker，Agent 分析和媒体生成任务只会进入等待状态；如果不启动 Celery Beat，丢失的第三方结果回收消息无法被定期补偿。
