# 弥亚视觉工具架构

## 定位

一个 AstrBot 插件，让 LLM 主动获得读图能力。核心原则：先批量读图落库，再按需查询，避免把大量图片描述塞进上下文。

## 工具

四个工具，差分明显：

- `vision_read`：读图并落库，只返回摘要，不返回每张图的详细内容。
- `vision_query`：查询已落库的结果。列表查询返回轻量列表（list 模式，每行含 `peek` 一句话预览），`result_id` 精确查询返回完整信息（full 模式）。
- `vision_export`：把符合条件的结果导出为 JSON/CSV 文件，方便外部脚本批量处理。
- `see_window`：截取整个屏幕或指定窗口画面，用 VL 模型分析（默认提示词偏向判断用户在干什么），仅支持 Windows。

## 数据流

```
用户请求
  │
  ▼
LLM 调用 vision_read(paths=[...])
  │
  ▼
扫描路径 → 收集图片 → 命中缓存？
  │ 是 → 跳过 VL 调用，命中数 +1
  │ 否  ▼
  │ 调用用户配置的 VL 模型
  │ 解析结果（peek / text / tags）
  │ 写入 SQLite 缓存
  │
  ▼
返回读图摘要 + next_call 提示 LLM 调用 vision_query
  │
  ▼
LLM 调用 vision_query(query/result_id/filename/path/recent/limit/offset)
  │
  ▼
返回 list/full 模式的结果列表
  │
  ▼
如需批量处理大量结果，调用 vision_export 导出 JSON/CSV 文件
```

## 缓存设计

表 `image_cache` 以 `(sha256, model_id, question, detail)` 为逻辑缓存键（内容寻址，不含文件名）：

- `sha256` 保证同内容不重复读，与文件名无关——`see_window` 的时间戳截图文件名每次不同，含 filename 会让缓存永远 miss。
- `model_id` 换模型时重新读，避免不同模型描述差异污染；`model_id` 为空时放宽为任意模型命中。
- `question` 同一个问题命中缓存，不同问题重新读（同图多问题并存为多条记录，不互相覆盖）。
- `detail` 影响模型实际输入（从而影响输出），必须在缓存键内；`''` 与 `'auto'` 语义等价互相兼容（老记录迁移友好）。
- `filename` 仍随记录落库，供查询/展示使用，只是不参与缓存匹配。

sha256 精确未命中后，还有 phash 感知哈希近似兜底（`find_cached_by_phash`）：
同 model + 同 question + 同 detail 的记录中，phash 汉明距离 ≤ 5（64bit）的最近一条视为同一图片。
缩尺/重压缩后 sha256 必变但 phash 几乎不变，靠此兑现「被压缩过的同图也命中缓存」。
防护与透明化：

- 纯色/低信息图片（phash 置 1 比特 < 4 或 > 60）跳过兜底——此类 phash 必然互相误判。
- `see_window` 调用时 `allow_phash=False` 禁用兜底——屏幕内容时刻在变，近似命中会返回过期描述。
- 两阶段查询：先轻列（result_id, phash）扫描候选，再取最佳一行的完整记录，避免重列全拉内存。
- 近似命中的返回附带 `matched_by="phash"` / `phash_distance`，且 `vision_read` 响应中透传 `cached_via_phash` 计数与提示。

追问模式通过 `previous_result_id` 携带上文，但只作用于与之前同一张图片（sha256 相同），避免污染多张图片。

## 结构化输出

读图 prompt 要求模型返回 JSON：`{"peek": "一句话预览（图片类型+主要内容+关键信息）", "text": "完整描述/回答", "tags": [...]}`。


- `tags` 字段真正填充（此前恒为 `[]`），提升 `vision_query` 搜索质量。
- v4fve 额外附加官方 `response_format={"type":"json_object"}`（DeepSeek JSON Output）；其他模型仅靠 prompt 引导。
- `_parse_result` 容错解析：容忍 ```json 围栏与前后杂音，解析失败回退「首行摘要」旧行为——读图永不因解析失败而失败。

## 模块职责

| 文件 | 职责 |
|---|---|
| `main.py` | 插件入口：加载配置、从 AstrBot context 读取已保存模型列表、初始化数据库、注册工具 |
| `tools/_registry.py` | 工厂函数 `make_tool`，注册四个工具实例 |
| `tools/vision_read.py` | 扫描路径、缓存判断、调用 VL、落库、返回摘要 |
| `tools/vision_query.py` | 按多种条件查询缓存结果，支持 list/full 模式 |
| `tools/vision_export.py` | 批量导出已读图结果为 JSON/CSV，方便外部脚本处理 |
| `tools/see_window.py` | Windows 屏幕/窗口截图分析（win32gui 枚举窗口 + PIL 截图），复用 vision_read 读图管线 |
| `tools/_store.py` | 存储抽象层：定义 `VisionStore` 基类，默认 `SQLiteVisionStore` |
| `tools/_vl_client.py` | OpenAI 兼容 VL 客户端 |
| `tools/_helpers.py` | `unwrap`、`proposal_reply`、`run_sync` |
| `tools/config.py` | 插件配置内存管理、provider 降级链解析 |
| `tools/tool_stats.py` | 工具调用统计 |
| `skills/vision-read/SKILL.md` | LLM 工作流提示 |

## 存储后端（计划）

`tools/_store.py` 已定义 `VisionStore` 抽象基类，目前默认实现 `SQLiteVisionStore`。未来接入其他数据库的步骤：

1. 在配置中新增 `db_uri` 字段，例如：
   - `postgresql://user:pass@localhost/irmia_vision`
   - `mongodb://localhost:27017/irmia_vision`
   - `sqlite:///path/to/vision_cache.db`（默认）
2. 在 `tools/_store.py` 中实现：
   - `PostgresVisionStore`（依赖 `psycopg`）
   - `MongoVisionStore`（依赖 `pymongo`）
3. 在 `create_store()` 中根据 `db_uri` 协议前缀分发。

切换后端后，工具调用方无需改动，因为 `vision_read` / `vision_query` 只依赖 `VisionStore` 接口。

## VL 模型配置与降级

插件启动时通过 `context.get_all_providers()` 读取 AstrBot 已保存的所有 CHAT_COMPLETION 类型模型，存入内存，并写入 `_conf_schema.json` 的 `options` 字段供 WebUI 下拉选择。

配置优先级：

1. `vl_provider_1` / `vl_provider_2` / `vl_provider_3`（WebUI 三个下拉框，按优先级排列）→ 合并为降级链。
2. `vl_provider_ids`（高级：手动填写逗号分隔 ID）→ 覆盖下拉框。
3. 以上全空 → 自动使用所有已保存模型（按保存顺序）。
4. 无任何已保存模型 → 回退到 `vl_model` 手动配置。

读图时按降级链顺序尝试：第一个模型失败（超时/报错/无 key）自动切换下一个，直到成功或全部失败。

## 关键约定

- 只处理 `png/jpg/jpeg/webp/gif/bmp`，压缩后 payload 最大 20MB（不限制原始文件大小）。
- `vision_read` 不返回详细内容，强制 LLM 走 `vision_query`。
- 路径支持绝对路径、相对路径、`~` 用户主目录。
- 数据库默认放在 AstrBot 数据目录；取不到时 fallback 到插件目录。
