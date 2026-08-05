# 作品中心接口文档

基础路径：`/api/v1`

权限约定：

- 公开作品列表、公开作品详情，以及公开作品的媒体流和缩略图不要求登录。
- 匿名访问时`liked_by_me=false`。
- 点赞、取消点赞、上传、创建、“我的作品”、编辑和删除必须登录。
- 请求携带了无效或过期的`Authorization`时仍返回401，不会降级成匿名用户。

作品中心所有媒体文件都在 OSS 作品前缀下，并为每个对象显式设置 `private` Object ACL：

```text
{OSS_ROOT_DIRECTORY}/works/
```

例如 `OSS_ROOT_DIRECTORY=story` 时，实际路径前缀为 `story/works/`。

即使 Bucket 本身为 `public-read`，`story/works/` 对象也不能通过无签名 OSS/CDN 地址访问。作品响应只返回后端媒体访问地址用于展示，不返回 OSS `object_key` 或永久 OSS 地址。

## 1. 作品媒体上传

```http
POST /api/v1/works/uploads
```

请求类型：`multipart/form-data`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| file | file | 是 | 只支持图片或视频 |

成功响应：

```json
{
  "upload_id": "上传 UUID",
  "media_type": "image",
  "url": "/api/v1/works/uploads/{upload_id}/preview",
  "filename": "demo.png",
  "content_type": "image/png",
  "size": 123456,
  "preview_url": "/api/v1/works/uploads/{upload_id}/preview"
}
```

说明：

- 文件会直接上传到作品中心用户目录：`{OSS_ROOT_DIRECTORY}/works/{user_id}/uploads/{upload_id}.{ext}`。
- 例如 `OSS_ROOT_DIRECTORY=story` 时：`story/works/{user_id}/uploads/{upload_id}.{ext}`。
- 上传时后端会写入 `x-oss-object-acl: private`，不会继承 Bucket 的公共读权限。
- 前端创建作品时只提交 `upload_id`。
- 返回的 `url` 和 `preview_url` 均为后端预览接口，不返回 `object_key` 或永久 OSS 地址。

### 预览上传文件

```http
GET /api/v1/works/uploads/{upload_id}/preview
```

仅上传者本人可访问。鉴权通过后返回 `307 Temporary Redirect`，`Location` 为短期 OSS 签名地址；浏览器应自动跟随重定向获取图片或视频。接口不会通过应用服务器转发媒体字节。

## 2. 创建作品或保存草稿

```http
POST /api/v1/works
```

请求：

```json
{
  "title": "我的作品",
  "description": "作品描述",
  "visibility": "public",
  "status": "published",
  "media_items": [
    {
      "upload_id": "上传 UUID",
      "sort_order": 0
    }
  ]
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| title | string | 是 | 1-128 字 |
| description | string | 否 | 最长 2000 |
| visibility | string | 否 | `public` 或 `private`，默认 `public` |
| status | string | 否 | `draft` 或 `published`，默认 `published` |
| media_items | array | 否 | 0-20 个媒体；发布作品时至少 1 个，草稿可为空 |
| media_items[].upload_id | UUID | 是 | `/works/uploads` 返回的上传 ID |
| media_items[].sort_order | integer | 否 | 展示排序 |

成功响应为作品详情。

规则：

- `status=draft`：保存草稿，不进入公开作品广场，不允许其他用户查看或点赞。
- `status=published`：发布作品，必须至少提交一个媒体。
- 作品描述使用 `description` 字段，不需要前端另行扩展字段。

创建作品成功后，后端会把已上传文件直接绑定为作品媒体，不再执行临时目录复制或移动。

上传文件所在目录：

```text
{OSS_ROOT_DIRECTORY}/works/{user_id}/uploads/{upload_id}.{ext}
```

例如 `OSS_ROOT_DIRECTORY=story` 时：

```text
story/works/{user_id}/uploads/{upload_id}.{ext}
```

数据库中的作品媒体保存 OSS 对象信息，但用户端响应只返回后端媒体访问地址，不返回 `object_key` 或永久 OSS 地址。

## 3. 公开作品广场

```http
GET /api/v1/works?page=1&page_size=20
```

无需登录。登录用户携带有效Bearer Token时，响应会计算`liked_by_me`；匿名用户固定返回`false`。

查询参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `page` | integer | `1` | 当前页，从 1 开始 |
| `page_size` | integer | `20` | 每页作品数，范围 1-100 |

后端只查询并响应当前页。列表中的每个作品只返回排序第一的媒体作为封面，`media_count`表示该作品的完整媒体数量；查看作品全部媒体时调用作品详情接口。

响应示例：

```json
{
  "items": [
    {
      "id": "作品 UUID",
      "user_id": "作者 UUID",
      "title": "我的作品",
      "description": "作品描述",
      "visibility": "public",
      "status": "published",
      "like_count": 10,
      "view_count": 100,
      "liked_by_me": false,
      "media_count": 4,
      "media_items": [
        {
          "id": "封面媒体 UUID",
          "media_type": "video",
          "url": "/api/v1/works/{work_id}/media/{media_id}/stream",
          "filename": "demo.mp4",
          "content_type": "video/mp4",
          "size": 123456,
          "width": null,
          "height": null,
          "duration_seconds": null,
          "sort_order": 0,
          "stream_url": "/api/v1/works/{work_id}/media/{media_id}/stream",
          "thumbnail_url": null,
          "created_at": "2026-06-04T10:00:00+08:00"
        }
      ],
      "created_at": "2026-06-04T10:00:00+08:00",
      "updated_at": "2026-06-04T10:00:00+08:00"
    }
  ],
  "total": 42,
  "page": 1,
  "page_size": 20
}
```

`media_items`在公开列表中最多包含 1 项，不代表完整媒体列表。

只返回：

```text
visibility=public
status=published
is_enabled=true
```

排序：

```text
like_count desc, created_at desc, id desc
```

## 4. 我的作品中心

```http
GET /api/v1/works/mine?visibility=public
GET /api/v1/works/mine?visibility=private
GET /api/v1/works/mine?status=draft
GET /api/v1/works/mine?status=published
GET /api/v1/works/mine
```

返回当前用户自己的作品。可查看公开、私密、草稿和下架作品。

## 5. 作品详情

```http
GET /api/v1/works/{work_id}
```

权限：

- 公开作品：任何人均可查看，不要求登录。
- 私密作品：作者本人或管理员可查看。
- 草稿作品：作者本人或管理员可查看。
- 下架作品：作者本人和管理员可查看，其他用户不可查看。
- 删除作品：仅管理员可查看。

响应中的媒体返回后端访问地址：

```json
{
  "id": "作品 UUID",
  "user_id": "作者 UUID",
  "title": "我的作品",
  "description": "作品描述",
  "visibility": "public",
  "status": "published",
  "like_count": 10,
  "view_count": 100,
  "liked_by_me": false,
  "media_count": 4,
  "media_items": [
    {
      "id": "媒体 UUID",
      "media_type": "video",
      "url": "/api/v1/works/{work_id}/media/{media_id}/stream",
      "filename": "demo.mp4",
      "content_type": "video/mp4",
      "size": 123456,
      "width": null,
      "height": null,
      "duration_seconds": null,
      "sort_order": 0,
      "stream_url": "/api/v1/works/{work_id}/media/{media_id}/stream",
      "thumbnail_url": null,
      "created_at": "2026-06-04T10:00:00+08:00"
    }
  ],
  "created_at": "2026-06-04T10:00:00+08:00",
  "updated_at": "2026-06-04T10:00:00+08:00"
}
```

详情接口中的`media_items`返回作品全部媒体，`media_count`等于完整媒体数量。只有媒体存在真实`thumbnail_object_key`时才返回`thumbnail_url`，否则返回`null`。

## 6. 修改和删除作品

```http
PATCH /api/v1/works/{work_id}
```

请求：

```json
{
  "title": "新标题",
  "description": "新描述",
  "visibility": "private",
  "status": "published",
  "media_items": [
    {
      "media_id": "已有作品媒体 UUID",
      "sort_order": 0
    },
    {
      "upload_id": "新上传 UUID",
      "sort_order": 1
    }
  ]
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| title | string | 否 | 1-128 字 |
| description | string | 否 | 最长 2000 |
| visibility | string | 否 | `public` 或 `private` |
| status | string | 否 | 用户侧只允许 `draft` 或 `published` |
| media_items | array | 否 | 不传表示保持媒体不变；传入时表示整体替换作品媒体列表，最多 20 个 |
| media_items[].media_id | UUID | 条件必填 | 保留当前作品已有媒体，并可调整排序 |
| media_items[].upload_id | UUID | 条件必填 | 添加一个 `/works/uploads` 新上传且未被使用的文件 |
| media_items[].sort_order | integer | 否 | 展示排序 |

仅作者可修改。`media_items[].media_id` 和 `media_items[].upload_id` 必须且只能传一个。

媒体编辑规则：

- 不传 `media_items`：只修改标题、描述、可见性或状态，作品媒体保持不变。
- 传 `media_items`：以后端收到的列表为准，整体替换当前作品媒体列表。
- 列表中带 `media_id` 的项会保留对应已有媒体，并更新 `sort_order`。
- 列表中带 `upload_id` 的项会把新上传文件绑定为作品媒体，并把该上传文件标记为已使用。
- 当前作品中没有出现在 `media_items` 里的旧媒体会被删除，并同步删除对应 OSS 对象。
- 不能重复提交同一个 `media_id` 或 `upload_id`。
- `media_id` 必须属于当前作品；`upload_id` 必须属于当前登录用户且未被使用。
- 目标状态为 `published` 时，最终作品必须至少有一个媒体。传空数组并发布会返回错误。
- 草稿作品可以没有媒体，也可以通过编辑接口添加、删除或重排媒体。

常见场景：

```json
{
  "media_items": [
    {
      "media_id": "原媒体 A",
      "sort_order": 0
    },
    {
      "upload_id": "新上传 B",
      "sort_order": 1
    }
  ]
}
```

上面的请求表示：保留原媒体 A、添加新上传 B、删除所有其他旧媒体。

```json
{
  "status": "draft",
  "media_items": []
}
```

上面的请求表示：把作品保存为草稿，并清空所有媒体。

```http
DELETE /api/v1/works/{work_id}
```

软删除作品，设置为 `status=deleted`、`is_enabled=false`，并删除该作品对应的 OSS 媒体对象。媒体对象删除失败时，接口会失败，不会继续更新作品删除状态。

## 7. 点赞

```http
POST /api/v1/works/{work_id}/like
DELETE /api/v1/works/{work_id}/like
```

两个接口都必须登录。未携带Bearer Token时返回HTTP 401。

规则：

- 同一用户对同一作品只能点赞一次。
- 点赞数写入 `like_count`，公开列表按点赞数排序。

## 8. 媒体访问

### 8.1 作品媒体

```http
GET /api/v1/works/{work_id}/media/{media_id}/stream
GET /api/v1/works/{work_id}/media/{media_id}/thumbnail
```

处理流程：

```text
前端请求后端媒体地址
→ 后端检查作品状态、可见性及当前用户权限
→ 后端返回 307 Temporary Redirect
→ 浏览器访问 Location 中的短期 OSS 签名地址
→ OSS 直接响应图片、视频或 Range 分段内容
```

权限：

- `published + public + is_enabled=true`：允许匿名访问。
- 私密作品：仅作者本人或管理员可访问。
- 草稿、下架作品：仅作者本人或管理员可访问。
- 已删除或不存在的作品、媒体：拒绝访问。

成功响应：

```http
HTTP/1.1 307 Temporary Redirect
Location: https://{bucket}.{endpoint}/{object_key}?OSSAccessKeyId=...&Expires=...&Signature=...
Cache-Control: private, no-store
```

`Location` 仅用于说明响应格式，前端不得解析、持久化或自行拼接其中的 OSS 参数。

### 8.2 上传预览

```http
GET /api/v1/works/uploads/{upload_id}/preview
```

- 必须登录。
- 仅上传记录所属用户可访问。
- 成功时同样返回 `307 Temporary Redirect` 和短期 OSS 签名地址。

### 8.3 签名有效期与 OSS 配置

默认签名有效期为 600 秒，可通过环境变量调整：

```env
OSS_SIGNED_URL_EXPIRES_SECONDS=600
```

OSS 必须允许实际前端域名跨域读取，并支持 `GET`、`HEAD` 和 Range 响应。建议暴露以下响应头：

```text
Content-Disposition
Content-Length
Content-Range
Accept-Ranges
Content-Type
ETag
Last-Modified
```

说明：

- 公开作品的`url`、`stream_url`和`thumbnail_url`是后端媒体接口地址，匿名用户可以直接用于`<img>`或`<video>`展示。
- 私密、草稿和下架作品的媒体仍只允许作者本人或管理员访问。
- 上传文件的`preview_url`仍要求上传者登录，不能匿名访问。
- 后端仍保存 `object_key` 用于删除 OSS 对象，但不响应给用户端。
- 前端必须使用接口响应中的 `url`、`stream_url` 或 `thumbnail_url`，不得改用历史 OSS 原始地址。
- 前端和 HTTP 客户端必须允许跟随 `307` 重定向。视频 Range 请求由 OSS 直接处理，不经过应用服务器或 Nginx 媒体代理。
- 签名地址到期后重新请求原后端媒体地址即可，前端不应长期缓存签名地址。
- 前端限制下载、保存和右键菜单由前端实现。

前端展示建议：

- 图片使用 `url` 或 `stream_url`。
- 视频使用 `stream_url`。

## 9. 管理端

### 作品列表

```http
GET /api/v1/admin/works
```

查询参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| user_id | UUID | 按作者筛选 |
| visibility | string | `public` 或 `private` |
| status | string | `draft`、`published`、`hidden`、`deleted` |
| page | integer | 页码 |
| page_size | integer | 每页数量 |

管理员列表同样按：

```text
like_count desc, created_at desc, id desc
```

### 作品详情

```http
GET /api/v1/admin/works/{work_id}
```

管理员可查看所有作品。

### 更新作品状态

```http
PATCH /api/v1/admin/works/{work_id}
```

请求：

```json
{
  "visibility": "public",
  "status": "hidden"
}
```

状态：

| status | 说明 |
| --- | --- |
| draft | 草稿 |
| published | 正常发布 |
| hidden | 管理员下架 |
| deleted | 删除；进入该状态时会删除对应 OSS 媒体对象 |

### 审核操作

管理端可把作品管理作为审核流程使用，前端按钮建议直接调用下面的动作接口。

```http
POST /api/v1/admin/works/{work_id}/approve
```

审核通过。作品状态改为 `published`，`is_enabled=true`。作品必须至少有一个媒体。

```http
POST /api/v1/admin/works/{work_id}/hide
```

下架作品。作品状态改为 `hidden`，`is_enabled=true`。下架作品不进入公开作品广场。

```http
DELETE /api/v1/admin/works/{work_id}
```

删除作品。作品状态改为 `deleted`，`is_enabled=false`，并删除对应 OSS 媒体对象。
