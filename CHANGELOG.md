# 更新日志

## 1.0.4

### 修复

- **插件加载早于 provider 初始化时读图失败**：`get_all_providers()` 返回空时从磁盘 `cmd_config.json` 兜底读取 provider 配置，保证降级链能解析出带 api_key 的模型。
- **修复配置文件 UTF-8 BOM 导致兜底解析失败**：磁盘读取改用 `utf-8-sig` 兼容 BOM。
- **`vl_model` 无 `api_key` 时不再进入降级链**：避免空 key 配置导致读图全部失败。
- **失败诊断增强**：全部模型调用失败时返回降级链详情与具体错误信息，方便排查。
- **缓存命中不再依赖 VL 模型配置**：未配置可用模型时仍可命中已读图片的缓存（`find_cached` 对空 `model_id` 放宽为任意模型匹配）。

## 1.0.3

### 新增

- **自动读取 AstrBot 已保存模型**：插件启动时通过 `context.get_all_providers()` 自动获取所有 CHAT_COMPLETION 类型模型，无需手动输入 base_url / api_key。
- **WebUI 三下拉框选择模型**：`vl_provider_1`（首选）、`vl_provider_2`（次选）、`vl_provider_3`（再次选），插件加载时自动拉取可用模型列表写入下拉选项。
- **多模型降级链**：读图时按首选 → 次选 → 再次选顺序逐个尝试，每个模型独立重试，全部失败才报错。
- **零配置开箱即用**：三个下拉框全部留空时，自动使用 AstrBot 中所有已保存的模型。
- `config.py` 新增 `resolve_provider_chain()` 解析降级链，`set_providers()` / `get_providers()` 管理 provider 列表。
- `_vl_client.py` 的 `read_image()` 新增 `vl_config` 参数，支持显式传入 VL 配置。
- `vl_provider_ids` 高级字段保留，手动填写后覆盖下拉框选择。

### 修复

- fallback provider 的 timeout 不再被 primary 的共享 client 截断（取链中最大值）。
- retry sleep 移到 semaphore 外，避免浪费并发槽位。
- `model_id` 缓存标记改为实际成功的 provider，而非始终取 chain[0]。
- provider 的 `key` 字段为字符串时也能正确提取（不再假设一定是列表）。

### 变更

- `_conf_schema.json` 新增 `vl_provider_1/2/3` 三个下拉框字段。
- `vl_model` 手动配置降级为最终回退方案。

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
