# 弥亚主动视觉工具

为 Astrbot agent 提供**主动读图工具**，使Agent可自行阅读在文件系统中发现的图片。

插件利用本地数据库设置缓存机制，当阅读相同图片（甚至被压缩过的同图片）时，将直接命中缓存并跳过对VL模型的请求，从本地数据库中阅读结果。

## 功能

- `vision_read`：读取图片或文件夹中的所有图片，调用用户配置的 VL 模型理解内容，结果存入本地数据库。
- `vision_query`：查询已读图的结果，支持关键词、文件名、路径、最近结果、分页。
- `vision_export`：将大量结果导出为 JSON/CSV，方便交给 Python 脚本批量处理。
- `vision_compare`：多图对比询问。多张图片在同一次 VL 请求中发送（模型同时看到全部图），找不同、横向对比、前后变化分析，结论直接返回并落库。
- `see_window`：截取整个屏幕或指定窗口画面并用 VL 模型分析，快速了解用户在干什么（仅 Windows）。
- 结构化读图结果：要求模型返回 JSON（`peek` 一句话预览 + `text` 完整内容 + `tags` 内容标签），插件容错解析，模型不遵守格式时自动回退。
- 异步并发 VL 调用，自适应并发数。
- 大图片自动压缩后上传。
- 同一张图读过会命中缓存，避免重复调用 VL 模型。

## 安装

1. 将插件目录放入 AstrBot 的 `plugins/` 目录。
2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 在 AstrBot WebUI 的插件配置中填写 VL 模型信息。

## 配置示例

### 推荐方式：WebUI 下拉框选择模型

在 AstrBot WebUI 插件配置中，通过三个下拉框按优先级选择模型：

- **首选 VL 模型**（`vl_provider_1`）：第一个尝试的模型
- **次选 VL 模型**（`vl_provider_2`）：首选不通/打挂时自动切换
- **再次选 VL 模型**（`vl_provider_3`）：次选也不可用时再降级

下拉框选项由插件启动时自动从 AstrBot 已保存模型中拉取，刷新 WebUI 即可看到。

- **全部留空 = 零配置**：自动使用 AstrBot 中所有已保存的模型。
- 如果某个模型无 API Key 或调用失败，会自动跳过/降级到下一个。

### 高级方式：手动填写 ID

如需更精细控制，可在 `vl_provider_ids` 字段手动填写模型 ID（逗号分隔，按降级顺序），填写后覆盖下拉框选择：

```
vl_provider_ids: my-gpt4o, my-qwen-vl, my-gemini
```

### 回退方式：手动配置

如果以上所有配置均为空，则使用 `vl_model` 手动配置：

```json
{
  "vl_model": {
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-xxxxxxxx",
    "model": "gpt-4o",
    "timeout": 120.0,
    "concurrency": 50,
    "max_retries": 2
  }
}
```

也支持任何 OpenAI 兼容 API，例如 Gemini、本地 vLLM、OneAPI 等。

配置项说明：

| 字段 | 说明 |
|---|---|
| `vl_provider_1` | 首选 VL 模型（WebUI 下拉框选择）。 |
| `vl_provider_2` | 次选 VL 模型（首选不可用时降级）。 |
| `vl_provider_3` | 再次选 VL 模型（次选也不可用时降级）。 |
| `vl_provider_ids` | 高级：手动填写模型 ID（逗号分隔），覆盖下拉框。 |
| `max_batch` | 单次批量读图数量上限，默认 2000（账单保险丝）。 |
| `provider` | 提供商标识，目前仅用于日志展示。 |
| `base_url` | OpenAI 兼容 API 的 base URL。 |
| `api_key` | API 密钥。 |
| `model` | VL 模型名称，例如 `gpt-4o`、`gemini-1.5-pro` 等。 |
| `timeout` | 单次 VL 请求超时时间（秒），默认 120。 |
| `concurrency` | 并发请求数。留空时根据 `timeout` 自适应，最高 200。 |
| `max_retries` | 单张图失败重试次数，默认 2。 |
| `detail` | 图片细节级别：`low` 更快更省（客户端对齐服务端压到 512×512）、`auto` 自动（默认）、`original` 保留原图（DeepSeek 的 `high` 等价 `original`）。 |
| `reasoning_effort` | DeepSeek 思考强度（仅 deepseek-flash / v4fve 生效）：`low` 更快更省、截断风险最低（默认，读图任务足够）、`high`/`max` 更深思虑、`none` 关闭思考模式。 |

## 使用示例

**用户说**："帮我看看 ~/Pictures 里的图"

1. LLM 判断需要读图，调用 `vision_read({"paths": ["~/Pictures"]})`。
2. 工具返回读图完成摘要。
3. LLM 调用 `vision_query({"recent": 5})` 查看最近结果。

**分类场景**：

1. LLM 调用 `vision_read({"paths": ["/source/folder"], "question": "判断图片类别：invoice、screenshot、photo、other"})`。
2. LLM 调用 `vision_query({"query": "invoice"})` 获取发票列表。
3. 输出分类 → 文件路径映射，由外部系统或用户执行移动。

**追问单张图**：

1. LLM 调用 `vision_query({"recent": 5})` 找到目标图的 `result_id`。
2. LLM 调用 `vision_read({"paths": ["/path/to/image.png"], "question": "发票金额是多少？", "previous_result_id": "res_xxx"})`。

**批量处理场景**：

1. `vision_read({"paths": ["/source/folder"]})` 批量读图。
2. `vision_export({"path": "/source/folder", "fmt": "json", "limit": 10000})` 导出 JSON。
3. 导出文件路径会返回给 LLM，可交给 Python 脚本进行批量分类、移动、统计等处理。

**多图对比场景**：

1. LLM 调用 `vision_compare({"paths": ["/shots/v1.png", "/shots/v2.png"], "question": "两版 UI 有什么差异？"})`。
2. 结论直接返回（peek + text + tags），同时落库，可用 `vision_query` 复查。
3. 追问同组图片：`vision_compare({"paths": [同一组路径], "question": "哪个改动最影响可用性？", "previous_result_id": "cmp_xxx"})`。

## 工具参数

### vision_read

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `paths` | `list[string]` | 是 | 图片或文件夹路径，支持多个。 |
| `question` | `string` | 否 | 高级用法。默认自动描述图片；如需追问特定问题，可传入。 |
| `force_reread` | `boolean` | 否 | 强制忽略缓存重新读。 |
| `previous_result_id` | `string` | 否 | 追问模式。只作用于与之前同一张图片。 |

### vision_query

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `result_id` | `string` | 否 | 精确查询单条结果（full 模式：包含 path、完整描述 text、tags 等）。 |
| `result_ids` | `list[string]` | 否 | 按 id 列表批量精确查询（如 vision_read 批量命中返回的 id 集合），返回顺序与传入一致（list 模式，最多 100 个）。 |
| `query` | `string` | 否 | 字面子串搜索（非语义）：空格/逗号分隔多词为 AND（每词都需命中）；按读取时间倒序（list 模式：返回 result_id/filename/peek/question）。 |
| `filename` | `string` | 否 | 按文件名查询（list 模式）。 |
| `path` | `string` | 否 | 按路径前缀/包含字符串查询（list 模式）。 |
| `recent` | `integer` | 否 | 最近 N 条（list 模式）。 |
| `limit` | `integer` | 否 | 最多返回条数，默认 20，最大 100。 |
| `offset` | `integer` | 否 | 分页偏移，默认 0。 |

### vision_compare

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `paths` | `list[string]` | 是 | 2-16 张图片或文件夹路径。相同内容的图片自动去重。 |
| `question` | `string` | 否 | 对比问题。留空使用默认对比分析 prompt（逐图要点→相同点→不同点→结论）。 |
| `force_reread` | `boolean` | 否 | 忽略组缓存强制重新对比。 |
| `previous_result_id` | `string` | 否 | 追问模式。仅对同一组图片（组指纹相同的 `cmp_` 记录）生效。 |

### vision_export

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | `string` | 否 | 自然语言搜索筛选。 |
| `filename` | `string` | 否 | 按文件名筛选。 |
| `path` | `string` | 否 | 按路径筛选。 |
| `recent` | `integer` | 否 | 导出最近 N 条。 |
| `limit` | `integer` | 否 | 最多导出条数，默认 1000，最大 10000。 |
| `offset` | `integer` | 否 | 分页偏移。 |
| `output_path` | `string` | 否 | 输出文件路径，默认当前工作目录下 `vision_export_时间戳.json`。 |
| `fmt` | `string` | 否 | 格式：`json` 或 `csv`，默认 `json`。 |

## 注意事项

- 只处理 `png/jpg/jpeg/webp/gif/bmp` 图片；GIF 动图只读取第一帧（动画内容不会被完整理解）。损坏文件（0 字节/截断）单独计 `decode_failed`——不归因模型失败、不占用降级链。
- 单次批量读图默认上限 2000 张（配置项 `max_batch`）：误传大目录会报错并提示分批，避免失控的 VL 调用费用。确需更大批量时调大该配置。
- 大图片会自动压缩到长边 2048 后上传；仅当压缩后仍超过 20MB 才报错（不限制原始文件大小）。
- 同一张图（按内容 hash + 模型 + 问题 + detail 档位，与文件名无关）读过会命中缓存，不再重复调用 VL 模型；缩尺/重压缩过的同图会经 phash 感知哈希近似命中（纯色图除外；see_window 为保证屏幕内容新鲜禁用近似命中）。近似命中会在响应中明确标注 `cached_via_phash`。
- **phash 的已知盲区**：实测小面积改动可能完全失明——0.011% 面积的标记点改动（12px 红点/800×1200 图）phash 距离为 0 被误判同图；1.5% 面积的色块距离 10 可正确区分。对精度敏感的场景（找微小差异）请用 `force_reread` 重读或 `vision_compare` 对比（后者走单次多图请求，模型可逐像素级指出差异）。
- `vision_compare` 的缓存键是**组指纹**（成员图片内容 hash 排序后联合哈希）：同一组图片任意顺序传入都命中，换问题/换模型/换 detail 重新对比；phash 近似命中不适用于图组。对比结论直接返回（上限 4000 字），同时落库供复查。单次上限 16 张、内联总量 40MB（DeepSeek 请求体上限 48MiB 留余量）。
- `vision_read` 只返回读取计数与下一步建议（不返回每张图内容）：单图建议直接 full 查，批量建议先 list 浏览。详细内容请用 `vision_query` 查询。
- 路径支持绝对路径、相对路径和 `~` 用户主目录。**注意**：相对路径按 AstrBot 进程的工作目录解析（agent 难以预测），建议优先用绝对路径或 `~`；未找到/不支持的传入路径会在报错中列出、成功响应带 `missing_paths` 回显。`vision_compare` 的传入顺序即图1/图2…编号顺序（before/after 对比请注意顺序）。
- 并发数默认根据 `timeout` 自适应，避免把慢 API 打挂。如需固定，可配置 `concurrency`。
- 支持多模型降级：三个下拉框按优先级排列，靠前的模型失败时自动切换到下一个。全部留空则自动使用所有已保存模型。
- DeepSeek v4fve（`deepseek-v4-flash-vision-exp`）与现行 `deepseek-flash` 适配：检测到思考型模型时压缩长边自动从 2048 降为 1024（其服务端会将图片缩放到总像素约 1300×1300、每张 token 上限 1024，更大输入无收益只费带宽）；默认以 `reasoning_effort=low` 调用（读图是感知任务，低强度思考足够，显著降低截断风险与费用）；`max_tokens` 基线抬到 8192（思维链与答案共享额度，过小会被思考吃光导致截断）；输出被截断（`finish_reason=length`）时自动放大额度重试一次，仍截断则计为失败并降级，不会把截断内容（含思维链）落库。

## 开发

```bash
python -m py_compile main.py tools/*.py
pytest tests/
```

## 架构

详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)。
