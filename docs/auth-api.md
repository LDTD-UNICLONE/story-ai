# 登录认证接口文档

基础路径：`/api/v1`

统一响应格式：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "timestamp": "2026-05-14T16:00:00+08:00"
}
```

## 认证方式

登录成功后接口会返回 `access_token`。后续需要登录的接口，在请求头中传：

```http
Authorization: Bearer <access_token>
```

Token 类型为 JWT，默认有效期由 `.env` 中的 `ACCESS_TOKEN_EXPIRE_MINUTES` 控制。

## 注册

### POST `/auth/register/sms-code`

发送注册手机验证码。每个手机号每 60 秒只能发送一次，验证码默认 5 分钟有效。

请求体：

```json
{
  "phone": "13800000000"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| phone | string | 是 | 中国大陆手机号，注册时必须使用该手机号 |

成功响应：

```json
{
  "code": 0,
  "message": "验证码已发送",
  "data": null,
  "timestamp": "2026-05-22T16:00:00+08:00"
}
```

常见错误：

| HTTP 状态码 | code | message |
| --- | --- | --- |
| 409 | 40902 | 手机号已存在 |
| 429 | 42910 | 验证码发送过于频繁，请 N 秒后再试 |
| 422 | 42200/42210 | 手机号格式不正确 |
| 500/502 | 50020/50021/50022 | 短信服务不可用 |

### POST `/auth/register`

注册普通用户。手机号和手机验证码必填；注册成功后不会自动登录，需要再调用登录接口获取 token。

请求体：

```json
{
  "account": "demo",
  "password": "123456",
  "nickname": "Demo",
  "avatar": "https://example.com/avatar.png",
  "phone": "13800000000",
  "sms_code": "123456",
  "email": "demo@example.com"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| account | string | 是 | 账号，3-64 位，唯一 |
| password | string | 是 | 密码，6-128 位 |
| nickname | string | 是 | 昵称，1-64 位 |
| avatar | string | 否 | 头像地址，不传使用默认头像 |
| phone | string | 是 | 手机号，唯一，必须先调用 `/auth/register/sms-code` 获取验证码 |
| sms_code | string | 是 | 手机短信验证码 |
| email | string | 否 | 邮箱，唯一 |

默认头像：

```text
https://wpimg.wallstcn.com/f778738c-e4f8-4870-b634-56703b4acafe.gif
```

成功响应：

```json
{
  "code": 0,
  "message": "注册成功",
  "data": {
    "id": "用户 UUID",
    "account": "demo",
    "nickname": "Demo",
    "avatar": "https://example.com/avatar.png",
    "phone": "13800000000",
    "email": "demo@example.com",
    "is_admin": false,
    "is_enabled": true,
    "points_balance": 0
  },
  "timestamp": "2026-05-14T16:00:00+08:00"
}
```

常见错误：

| HTTP 状态码 | code | message |
| --- | --- | --- |
| 400 | 40010 | 手机验证码错误或已过期 |
| 409 | 40901 | 账号、手机号或邮箱已存在 |
| 422 | 42200 | 参数校验失败 |

## 登录

### POST `/auth/login`

用户登录。`identifier` 支持账号、手机号、邮箱。

请求体：

```json
{
  "identifier": "demo",
  "password": "123456"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| identifier | string | 是 | 账号、手机号或邮箱 |
| password | string | 是 | 密码 |

成功响应：

```json
{
  "code": 0,
  "message": "登录成功",
  "data": {
    "access_token": "jwt token",
    "token_type": "bearer",
    "expires_in": 604800,
    "user": {
      "id": "用户 UUID",
      "account": "demo",
      "nickname": "Demo",
      "avatar": "https://example.com/avatar.png",
      "phone": "13800000000",
      "email": "demo@example.com",
      "is_admin": false,
      "is_enabled": true,
      "points_balance": 0
    }
  },
  "timestamp": "2026-05-14T16:00:00+08:00"
}
```

说明：

- `expires_in` 单位为秒。
- 密码在数据库中以哈希形式保存，不保存明文。
- 用户被禁用时不能登录。

常见错误：

| HTTP 状态码 | code | message |
| --- | --- | --- |
| 401 | 40101 | 账号或密码错误 |
| 403 | 40302 | 账号已被禁用 |
| 422 | 42200 | 参数校验失败 |

## 获取当前用户

### GET `/auth/me`

获取当前登录用户信息。

请求头：

```http
Authorization: Bearer <access_token>
```

成功响应：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "id": "用户 UUID",
    "account": "demo",
    "nickname": "Demo",
    "avatar": "https://example.com/avatar.png",
    "phone": "13800000000",
    "email": "demo@example.com",
    "is_admin": false,
    "is_enabled": true,
    "points_balance": 0
  },
  "timestamp": "2026-05-14T16:00:00+08:00"
}
```

常见错误：

| HTTP 状态码 | code | message |
| --- | --- | --- |
| 401 | 40102 | 无效的登录凭证 |
| 401 | 40103 | 用户不存在或已被删除 |
| 403 | 40302 | 账号已被禁用 |

### PATCH `/users/me/profile`

修改当前登录用户资料。只能修改昵称和头像，不能修改账号、手机号、邮箱、密码、管理员状态、启用状态或积分余额。

鉴权：需要登录。

请求头：

```http
Authorization: Bearer <access_token>
```

请求体：

```json
{
  "nickname": "新的昵称",
  "avatar": "https://example.com/avatar.png"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| nickname | string | 否 | 昵称，1-64 位 |
| avatar | string | 否 | 头像 URL，最大 512 位 |

成功响应：同 `GET /auth/me`，返回更新后的用户信息。

常见错误：

| HTTP 状态码 | code | message |
| --- | --- | --- |
| 401 | 40102 | 无效的登录凭证 |
| 401 | 40103 | 用户不存在或已被删除 |
| 403 | 40302 | 账号已被禁用 |
| 422 | 42200 | 参数校验失败 |

## 前端调用流程

1. 调用 `/auth/register/sms-code` 给手机号发送验证码。
2. 调用 `/auth/register`，传入手机号和 `sms_code` 注册用户。
3. 调用 `/auth/login` 登录。
4. 保存 `data.access_token`。
5. 调用需要登录的接口时添加 `Authorization` 请求头。
6. 当接口返回 `40102`、`40103` 时，清除本地 token 并跳转登录页。
7. 当接口返回 `40302` 时，提示账号已被禁用。

## 短信服务配置

注册验证码使用阿里云短信服务。生产环境需要配置：

```env
ALIYUN_SMS_ACCESS_KEY_ID=你的 AccessKeyId
ALIYUN_SMS_ACCESS_KEY_SECRET=你的 AccessKeySecret
ALIYUN_SMS_REGION_ID=cn-hangzhou
ALIYUN_SMS_SIGN_NAME=短信签名
ALIYUN_SMS_REGISTER_TEMPLATE_CODE=注册验证码模板 Code
ALIYUN_SMS_REGISTER_TEMPLATE_PARAM_KEY=code
ALIYUN_SMS_CODE_TTL_SECONDS=300
ALIYUN_SMS_SEND_INTERVAL_SECONDS=60
ALIYUN_SMS_MOCK_ENABLED=false
```

如果 `ALIYUN_SMS_ACCESS_KEY_ID` 和 `ALIYUN_SMS_ACCESS_KEY_SECRET` 留空，后端会自动复用 `OSS_ACCESS_KEY_ID` 和 `OSS_ACCESS_KEY_SECRET`。但该 AccessKey 必须拥有阿里云短信发送权限。

模板变量名如果不是 `code`，需要同步修改 `ALIYUN_SMS_REGISTER_TEMPLATE_PARAM_KEY`。
