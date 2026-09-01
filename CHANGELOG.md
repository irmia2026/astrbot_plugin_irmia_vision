# 更新日志

## 1.0.6

### 新增

- **phash 感知哈希近似缓存命中**：sha256 精确未命中后，自动用已落库的 phash 做近似匹配（同 model + 同 question，汉明距离 ≤ 5）。缩尺/重压缩的同一张图不再重复调用 VL 模型，兑现「甚至被压缩过的同图片也命中缓存」的承诺。近似命中返回 `matched_by` / `phash_distance` 供甄别。

### 修复

- **see_window 截图缓存永远 miss**：截图文件名带微秒时间戳，而缓存键含 filename 维度，导致同一画面每次截图都重复调用 VL 模型。缓存键改为 `(sha256, model_id, question)` 纯内容寻址，与文件名无关；filename 仍落库供查询展示。
- **20MB 大小限制误杀大图**：原检查按原始文件字节数在压缩前拦截，25MB 的照片压缩后仅 1-2MB 却被拒绝。大小检查移到 `_compress_image` 压缩结果上，不再限制原始文件；同时删除 `vision_read` 中的重复前置检查。
- **批量读图冻结 AstrBot 事件循环**：`sha256_of_file` / `find_cached` / `insert` / `get_by_result_id` / `_compute_phash` 及 VL 调用内的 `encode_image` 全部是事件循环上的同步 I/O，实测 100 张图造成 5.6 秒连续冻结。现统一通过 `run_sync()` offload 到线程池（同步修复 `run_sync` 弃用的 `get_event_loop` 并真正启用它），同场景最长停滞降至 ~17ms。`SQLiteVisionStore` 相应增加 `threading.RLock` 串行化跨线程访问。

## 1.0.5

### 新增

- **`see_window` 工具：快速查看电脑屏幕或指定窗口**。截取整个屏幕或按窗口标题关键词（支持 `vs code` / `qq` / `wechat` 等常见缩写）截取指定窗口画面，用 VL 模型分析并落库。默认提示词偏向「干活」——搞清楚用户在干什么、屏幕上发生了什么。找不到窗口时返回当前可见窗口列表。仅支持 Windows（非 Windows 平台返回明确错误提示）。截图、读图、缓存、降级链、落库全部复用 `vision_read` 管线，不重复造轮子。

### 修复

- **DeepSeek 推理型视觉模型（`deepseek-v4-flash-vision-exp`）支持**：该模型为思考模式，思维链走 `reasoning_content` 字段（与 `content` 同级）；当 `max_tokens` 不足时思考过程耗尽额度，`content` 为空。修复：`content` 为空时回退到 `reasoning_content`，默认 `max_tokens` 2048 → 4096。已实测确认该模型视觉 API 完全可用。
- **降级链被「空内容」误判为成功而中断**：首选模型返回空 content 时，旧逻辑误判为成功并 `break` 退出整个循环，导致后续降级模型根本没有机会执行，最终误报「所有 VL 模型均未配置 api_key」。现在空内容视为该模型失败，继续降级下一个模型；错误文案同步修正为「所有 VL 模型均返回空内容」。

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
