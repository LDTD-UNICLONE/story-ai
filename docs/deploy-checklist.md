# 部署检查清单

## 运行条件

- Python 3.9+
- PostgreSQL 13+
- Redis 6+
- Nginx
- 可访问 Comfly API
- 可访问阿里云 OSS
- 可访问阿里云短信服务

## 必填环境变量

生产环境建议：

```env
APP_ENV=production
APP_DEBUG=false
JWT_SECRET_KEY=替换为强随机字符串
CORS_ORIGINS=["https://你的前端域名"]

POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=story_ai_user
POSTGRES_PASSWORD=强密码
POSTGRES_DB=story-ai

REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_DB=10
CELERY_BROKER_DB=11
CELERY_RESULT_DB=12

COMFLY_BASE_URL=https://ai.comfly.chat
COMFLY_API_KEY=你的 Comfly Key
COMFLY_TIMEOUT_SECONDS=600

OSS_BUCKET_NAME=你的 Bucket
OSS_ACCESS_KEY_ID=你的 AccessKeyId
OSS_ACCESS_KEY_SECRET=你的 AccessKeySecret
OSS_PUBLIC_BASE_URL=https://你的 OSS 访问域名
OSS_ROOT_DIRECTORY=story

ALIYUN_SMS_ACCESS_KEY_ID=你的短信 AccessKeyId，可留空复用 OSS_ACCESS_KEY_ID
ALIYUN_SMS_ACCESS_KEY_SECRET=你的短信 AccessKeySecret，可留空复用 OSS_ACCESS_KEY_SECRET
ALIYUN_SMS_REGION_ID=cn-hangzhou
ALIYUN_SMS_SIGN_NAME=短信签名
ALIYUN_SMS_REGISTER_TEMPLATE_CODE=注册验证码模板 Code
ALIYUN_SMS_REGISTER_TEMPLATE_PARAM_KEY=code
ALIYUN_SMS_CODE_TTL_SECONDS=300
ALIYUN_SMS_SEND_INTERVAL_SECONDS=60
ALIYUN_SMS_MOCK_ENABLED=false

WECHAT_PAY_APPID=微信支付 AppID
WECHAT_PAY_MCHID=微信支付商户号
WECHAT_PAY_API_V3_KEY=微信支付 APIv3 密钥
WECHAT_PAY_MERCHANT_SERIAL_NO=商户 API 证书序列号
WECHAT_PAY_PRIVATE_KEY_PATH=/www/story-ai/certs/apiclient_key.pem
WECHAT_PAY_PLATFORM_CERT_PATH=/www/story-ai/certs/wechatpay_platform.pem
WECHAT_PAY_NOTIFY_URL=https://你的后端域名/api/v1/points/wechat/notify
WECHAT_PAY_TIMEOUT_SECONDS=30
WECHAT_PAY_NATIVE_EXPIRE_MINUTES=30
WECHAT_PAY_MOCK_ENABLED=false
```

下载微信支付平台证书：

```bash
python scripts/download_wechatpay_platform_cert.py
```

脚本会调用微信支付 `/v3/certificates`，用 `WECHAT_PAY_API_V3_KEY` 解密平台证书，并保存到 `WECHAT_PAY_PLATFORM_CERT_PATH`。

## 初始化

```bash
cd /www/story-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
```

## API 启动命令

```bash
/www/story-ai/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 2
```

## Celery 启动命令

```bash
/www/story-ai/.venv/bin/python -m celery -A app.worker.celery_app worker -l info -E --concurrency=2 --queues=celery,story_ai_default
/www/story-ai/.venv/bin/python -m celery -A app.worker.celery_app worker -l info -E --concurrency=16 --queues=story_ai_text --hostname=story-ai-text@%h
/www/story-ai/.venv/bin/python -m celery -A app.worker.celery_app worker -l info -E --concurrency=16 --queues=story_ai_image --hostname=story-ai-image@%h
/www/story-ai/.venv/bin/python -m celery -A app.worker.celery_app worker -l info -E --concurrency=10 --queues=story_ai_video --hostname=story-ai-video@%h
/www/story-ai/.venv/bin/python -m celery -A app.worker.celery_app beat -l info --schedule=/www/story-ai/celerybeat-schedule
```

Celery Beat 必须保持单实例运行，用于定期补偿丢失的第三方任务结果回收消息。systemd 模板见`data/story-ai-celery-beat.service`。

## Flower 启动命令

Flower 用于查看 Celery 队列和任务状态。建议只监听内网地址，并配置基础认证。

```bash
/www/story-ai/.venv/bin/python -m pip install -r /www/story-ai/requirements-flower.txt
/www/story-ai/.venv/bin/python -m celery -A app.worker.celery_app flower --address=127.0.0.1 --port=5555 --basic_auth=admin:change-me
```

Flower 是独立监控服务，不参与主 API 启动校验。不要把 `FLOWER_*` 写入主项目 `.env`，如需自定义 Flower 配置，服务器可创建 `/etc/story-ai-flower.env`：

```env
STORY_AI_FLOWER_ADDRESS=127.0.0.1
STORY_AI_FLOWER_PORT=5555
STORY_AI_FLOWER_BASIC_AUTH=admin:change-me
```

systemd 服务模板见 `data/story-ai-flower.service`。

## Nginx 关键配置

```nginx
client_max_body_size 300M;

location / {
    proxy_pass http://127.0.0.1:8010;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## 上线前检查

```bash
python -m compileall app
python -c "from app.main import app; print(len(app.routes))"
python -c "from app.worker import celery_app; print('tasks.model_generation.run_conversation_generation' in celery_app.tasks)"
python -m celery -A app.worker.celery_app inspect registered
```

## 注意事项

- 不要提交 `.env`、`.env copy`、`.env.production` 等真实配置文件。
- 如果服务器已有其他 Python 项目，请使用独立端口、独立虚拟环境、独立 systemd 服务名。
- 如果服务器已有 Redis 项目，请为本项目使用独立 Redis DB。
- 生产环境 `APP_ENV=production` 时会触发配置强校验，配置不完整会启动失败。
