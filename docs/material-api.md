# 素材库用户接口文档

基础路径：`/api/v1`

素材库接口用于给用户端展示和使用预设图像资源。所有接口都需要登录，请在请求头中传：

```http
Authorization: Bearer <access_token>
```

统一响应格式：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "timestamp": "2026-05-29T18:30:00+08:00"
}
```

分页响应通用结构：

```json
{
  "items": [],
  "total": 0,
  "page": 1,
  "page_size": 20
}
```

## 安全说明

- 用户端接口只返回已启用素材。
- `image_url` 直接返回 OSS URL，前端可直接用于图片展示。
- 前端限制下载、保存和右键菜单由前端实现。

## 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | UUID | 素材 ID |
| name | string | 图像名 |
| category | string | 分类 |
| description | string/null | 图像描述 |
| tags | string[] | 标签 |
| image_url | string | OSS 图像 URL |
| filename | string | 原始文件名 |
| content_type | string | MIME 类型，例如 `image/png` |
| size | integer | 文件大小，单位字节 |
| sort_order | integer | 排序值 |

## GET `/materials`

查询素材列表。只返回已启用素材，默认按 `sort_order` 从小到大、`created_at` 从晚到早返回。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| keyword | string | 否 | - | 关键词，最长 255；匹配图像名、分类、描述 |
| category | string | 否 | - | 分类，最长 64；精确匹配 |
| page | integer | 否 | 1 | 页码，大于等于 1 |
| page_size | integer | 否 | 20 | 每页数量，1-100 |

请求示例：

```http
GET /api/v1/materials?category=scene&page=1&page_size=20
```

成功响应 `data`：

```json
{
  "items": [
    {
      "id": "素材 UUID",
      "name": "森林背景",
      "category": "scene",
      "description": "适合作为童话森林场景",
      "tags": ["森林", "背景", "自然"],
      "image_url": "https://oss.example.com/story/material/forest.png",
      "filename": "forest.png",
      "content_type": "image/png",
      "size": 123456,
      "sort_order": 10
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

前端建议：

- 先用 `GET /materials/categories` 渲染分类筛选，再请求列表。
- 图片预览直接使用 `image_url`。

## GET `/materials/categories`

查询已启用素材的分类列表。

请求示例：

```http
GET /api/v1/materials/categories
```

成功响应 `data`：

```json
["character", "prop", "scene"]
```

## GET `/materials/{material_id}`

查询素材详情。只可查询已启用素材。

路径参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| material_id | UUID | 是 | 素材 ID |

请求示例：

```http
GET /api/v1/materials/00000000-0000-0000-0000-000000000000
```

成功响应 `data`：

```json
{
  "id": "素材 UUID",
  "name": "森林背景",
  "category": "scene",
  "description": "适合作为童话森林场景",
  "tags": ["森林", "背景", "自然"],
  "image_url": "https://oss.example.com/story/material/forest.png",
  "filename": "forest.png",
  "content_type": "image/png",
  "size": 123456,
  "sort_order": 10
}
```

## 常见错误

| code | HTTP 状态码 | 说明 |
| --- | --- | --- |
| 40102 | 401 | 无效的登录凭证 |
| 40103 | 401 | 用户不存在或已被删除 |
| 40302 | 403 | 账号已被禁用 |
| 40410 | 404 | 素材不存在或未启用 |
| 50010 | 500 | OSS 配置不完整 |
