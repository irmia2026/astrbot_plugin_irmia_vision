# 更新日志

## 1.0.1

### 修复

- 单图场景下 `vision_read` 的 `next_call` 直接返回 `result_id`，避免返回 `recent` 造成多余查询。
- 明确 `previous_result_id` 仅作用于与之前同一张图片（sha256 相同），避免多图路径下误导。
- 为 `.bmp` 图片补充 `image/bmp` MIME 类型。

## 1.0.0

### 新增

- 插件初始化：AstrBot 插件入口、配置加载、数据库初始化。
- `vision_read`：批量读取图片，支持文件/文件夹/多路径，命中缓存则跳过 VL 调用。
- `vision_query`：按 `result_id`、`filename`、`path`、`query`、`recent` 查询已读图结果，支持分页。
- 缓存策略：按 `(sha256, filename, model_id, question)` 缓存，换文件名/模型/问题会重新读图。
- 追问模式：通过 `previous_result_id` 基于已有理解继续提问。
- 强制重读：`force_reread` 忽略缓存。
- 默认中文图片描述 prompt，客观、生动、逐字提取可见文字。
- 单张图片 20MB 大小限制。
- `skills/vision-read/SKILL.md` 提供 LLM 工作流提示。
