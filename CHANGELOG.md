# 更新日志

## 1.1.0

### 新增

- **自动读取 AstrBot 已保存模型**：插件启动时通过 `context.get_all_providers()` 自动获取所有 CHAT_COMPLETION 类型模型，无需手动输入 base_url / api_key。
- **`vl_provider_ids` 配置项**：在 WebUI 中填写 AstrBot 模型 ID（逗号分隔），按降级顺序排列。第一个失败时自动切换下一个。
- **多模型降级链**：`vision_read` 按 `vl_provider_ids` 顺序逐个尝试，全部失败才报错。
- `config.py` 新增 `resolve_provider_chain()` 解析降级链，`set_providers()` / `get_providers()` 管理 provider 列表。
- `_vl_client.py` 的 `read_image()` 新增 `vl_config` 参数，支持显式传入 VL 配置。

### 变更

- `_conf_schema.json` 新增 `vl_provider_ids` 字段，推荐优先使用。
- `vl_model` 手动配置降级为回退方案（`vl_provider_ids` 为空时生效）。

## 1.0.2

### 新增

- `vision_export`：批量导出已读图结果为 JSON/CSV，方便脚本批量处理。
- 异步并发 VL 读图：通过 `asyncio.Semaphore` 控制并发，支持 `concurrency` / `timeout` / `max_retries` 配置。
- 大图片自动压缩：长边超过 2048 时按比例缩放，减少上传体积和 token 消耗。
- 自适应并发：未配置 `concurrency` 时，根据 `timeout` 自动估算（最高 200）。
- 存储后端抽象：新增 `tools/_store.py` 的 `VisionStore` 基类，默认 `SQLiteVisionStore`，预留 PostgreSQL / MongoDB 扩展路径。
- 工具描述改为 LLM 视角，明确由 LLM 自主决定何时调用。
- `vision_query` 增加 peek / full 双模式：列表查询只返回 `result_id`/`filename`/`summary`，`result_id` 精确查询才返回完整 `path`/`text`/`tags`。

### 修复

- 单图场景下 `vision_read` 的 `next_call` 直接返回 `result_id`。
- 明确 `previous_result_id` 仅作用于与之前同一张图片（sha256 相同）。
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
