# 常量提示词

这里存放业务生成时需要拼接的固定提示词。

Agent 资产图像生成通过共享服务使用以下模板；普通画布图像生成不会拼接这些内容：

- `asset_image_generation/character/{generation_mode}.md`
- `asset_image_generation/scene/{generation_mode}.md`
- `asset_image_generation/prop/{generation_mode}.md`

默认生成模式为 `general`。

内置模式：

- `general`：通用生成模式。
- `profile_card`：资料卡模式。

资料卡模式兼容请求别名：`card`、`data_card`、`profile-card`、`资料卡`、`资料卡模式`。
