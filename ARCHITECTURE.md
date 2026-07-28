# 弥亚视觉工具架构

## 定位

一个 AstrBot 插件，让 LLM 主动获得读图能力。核心原则：先批量读图落库，再按需查询，避免把大量图片描述塞进上下文。

## 工具

三个工具，差分明显：

- `vision_read`：读图并落库，只返回摘要，不返回每张图的详细内容。
- `vision_query`：查询已落库的结果。列表查询返回轻量摘要（peek 模式），`result_id` 精确查询返回完整信息（full 模式）。
- `vision_export`：把符合条件的结果导出为 JSON/CSV 文件，方便外部脚本批量处理。

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
  │ 解析结果（summary / text / tags）
  │ 写入 SQLite 缓存
  │
  ▼
返回读图摘要 + next_call 提示 LLM 调用 vision_query
  │
  ▼
LLM 调用 vision_query(query/result_id/filename/path/recent/limit/offset)
  │
  ▼
返回 peek/full 模式的结果列表
  │
  ▼
如需批量处理大量结果，调用 vision_export 导出 JSON/CSV 文件
```

## 缓存设计

表 `image_cache` 以 `(sha256, filename, model_id, question)` 为逻辑缓存键：

- `sha256` 保证同内容不重复读。
- `filename` 保证同内容不同文件名可分别缓存（对文件分类场景重要）。
- `model_id` 换模型时重新读，避免不同模型描述差异污染。
- `question` 同一个问题命中缓存，不同问题重新读。

追问模式通过 `previous_result_id` 携带上文，但只作用于与之前同一张图片（sha256 相同），避免污染多张图片。

## 模块职责

| 文件 | 职责 |
|---|---|
| `main.py` | 插件入口：加载配置、从 AstrBot context 读取已保存模型列表、初始化数据库、注册工具 |
| `tools/_registry.py` | 工厂函数 `make_tool`，注册三个工具实例 |
| `tools/vision_read.py` | 扫描路径、缓存判断、调用 VL、落库、返回摘要 |
| `tools/vision_query.py` | 按多种条件查询缓存结果，支持 peek/full 模式 |
| `tools/vision_export.py` | 批量导出已读图结果为 JSON/CSV，方便外部脚本处理 |
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

- 只处理 `png/jpg/jpeg/webp/gif/bmp`，单张最大 20MB。
- `vision_read` 不返回详细内容，强制 LLM 走 `vision_query`。
- 路径支持绝对路径、相对路径、`~` 用户主目录。
- 数据库默认放在 AstrBot 数据目录；取不到时 fallback 到插件目录。
