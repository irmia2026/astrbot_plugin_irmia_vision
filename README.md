# 弥亚视觉工具

一个 AstrBot 插件，让 LLM 主动获得读图能力。

## 功能

- `vision_read`：读取图片或文件夹中的所有图片，调用用户配置的 VL 模型理解内容，结果存入本地数据库。
- `vision_query`：查询已读图的结果，支持关键词、文件名、路径、最近结果、分页。
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
| `provider` | 提供商标识，目前仅用于日志展示。 |
| `base_url` | OpenAI 兼容 API 的 base URL。 |
| `api_key` | API 密钥。 |
| `model` | VL 模型名称，例如 `gpt-4o`、`gemini-1.5-pro` 等。 |
| `timeout` | 单次 VL 请求超时时间（秒），默认 120。 |
| `concurrency` | 并发请求数。留空时根据 `timeout` 自适应，最高 200。 |
| `max_retries` | 单张图失败重试次数，默认 2。 |

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
| `query` | `string` | 否 | 自然语言搜索。 |
| `result_id` | `string` | 否 | 精确查询单条结果。 |
| `filename` | `string` | 否 | 按文件名查询。 |
| `path` | `string` | 否 | 按路径前缀/包含字符串查询。 |
| `recent` | `integer` | 否 | 最近 N 条。 |
| `limit` | `integer` | 否 | 最多返回条数，默认 20，最大 100。 |
| `offset` | `integer` | 否 | 分页偏移，默认 0。 |

## 注意事项

- 只处理 `png/jpg/jpeg/webp/gif/bmp` 图片。
- 单张图片最大 20MB，超过会报错；大图片会自动压缩到长边 2048 后上传。
- 同一张图（按内容 hash + 文件名 + 模型 + 问题）读过会命中缓存，不再重复调用 VL 模型。
- `vision_read` 只返回摘要，详细内容请用 `vision_query` 查询。
- 路径支持绝对路径、相对路径和 `~` 用户主目录。
- 并发数默认根据 `timeout` 自适应，避免把慢 API 打挂。如需固定，可配置 `concurrency`。

## 开发

```bash
python -m py_compile main.py tools/*.py
pytest tests/
```

## 架构

详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)。
