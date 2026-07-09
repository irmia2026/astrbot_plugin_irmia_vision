---
name: vision-read
description: >
  主动读图工作流。触发：当你在任何场景下需要获取图片中的视觉信息——包括用户明确要求看图片、以及你在文件操作（dir_list、es_search、目录遍历等）中自行发现了 .jpg/.png/.webp/.bmp 等图片文件并认为其内容可能与当前任务相关。
  核心原则：先批量读图落库，再按需查询结果，避免把大量图片描述塞进上下文。
  可用工具：vision_read、vision_query。
---

# 视觉读图工作流

## 核心原则

```
vision_read 只负责把图读完存进数据库。
具体结果不直接返回，必须再用 vision_query 查询。
同一张图读过就不再读，命中缓存时直接跳过 VL 调用。
批量读图时不要一次性看完所有结果，先看聚合摘要和样例，再按需深入。
```

## 什么时候触发

### 用户明示

- 用户提到图片、文件夹、截图、发票、UI、照片、视频帧
- 任务需要对图片进行分类、筛选、提取文字、定位内容
- 用户想根据图片内容移动/整理文件

### 你主动判断（重要！）

**你在执行文件操作时遇到图片，应自己判断是否需要读内容**，而非等待用户指令。典型场景：

- 你用 `dir_list` 浏览目录，发现里面有 `.jpg` / `.png` 等图片文件 → 图片文件名可能是时间戳、编号等，内容才是关键信息；应调用 `vision_read` 了解实际内容
- 你用 `es_search` 搜索文件，结果中包含图片路径 → 如果当前任务需要了解文件内容（如审计、整理、分类），应主动读图
- 你在处理一个混合目录（同时有文档和照片） → 照片往往是文档的视觉证据，主动读图可能发现文档中没有的关键信息
- 你遍历项目文件夹，看到截图、UI mockup、发票扫描件 → 这些图片内容直接影响你对项目的理解
- 你在整理/归档文件时看到图片 → 仅靠文件名可能判断不出图片所属类别，读图后才能准确分类

**决策原则**：如果你看着一个图片路径，心里想"不知道里面是什么"——那就该调 `vision_read`。

## 标准工作流

### 1. 读图

调用 `vision_read`：

```json
{
  "paths": ["/path/to/file.png", "/path/to/folder"]
}
```

默认不需要传 `question`，工具会自动使用专业图片描述 prompt 读取图片。只有当你需要追问特定问题时，才传 `question`。

追问模式（仅适用于**同一张图片**，即 `paths` 只包含那张图）：

```json
{
  "paths": ["/path/to/file.png"],
  "question": "这个发票的日期和金额分别是多少？",
  "previous_result_id": "res_xxx"
}
```

强制重新读图：

```json
{
  "paths": ["/path/to/file.png"],
  "question": "从 UI 设计的角度评价这张图",
  "force_reread": true
}
```

要点：

- `paths` 可以是文件路径或文件夹路径，支持多个。
- **默认不要传 `question`**，工具会用专业图片描述 prompt 自动读取。
- 只有需要追问特定问题时，才传 `question`。
- 同一个 `question` 会命中缓存；换问题会重新读图。
- `previous_result_id` 只对与之前同一张图片（sha256 相同）生效；如果传了多个不同图片，只有匹配的那张会进入追问模式，其他图正常读取。
- 想换角度重新读时，传 `force_reread=true` 忽略缓存。

### 2. 获取结果

`vision_read` 返回的是完成摘要，例如：

```json
{
  "ok": true,
  "status": "success",
  "total": 1250,
  "cached": 980,
  "read": 270,
  "failed": 0,
  "result_id_hint": "新结果 result_id 范围: res_abc123 ~ res_xyz789",
  "proposal": "读图完成。请用 vision_query 查看具体结果。",
  "next_call": {
    "tool": "vision_query",
    "arguments": {"result_id": "res_abc123", "recent": 10}
  }
}
```

如果读图失败，会返回 `status: "partial"` 或 `"failed"`，并附带 `failed_paths` 说明哪些文件出错。

### 3. 查询结果

`vision_query` 有两种返回模式：

- **peek 模式**：列表查询（`query`、`filename`、`path`、`recent`），只返回 `result_id`、`filename`、`summary` 三项。适合翻页、筛选、分类。
- **full 模式**：`result_id` 精确查询，返回完整信息，包括 `path`、`text`、`tags`、`read_at`、`hit_count`。

#### 自然语言搜索（peek）

```json
{
  "query": "发票",
  "limit": 20
}
```

#### 按文件名查询（peek）

```json
{
  "filename": "invoice_001.png"
}
```

#### 按路径查询（peek）

```json
{
  "path": "C:/Users/me/Pictures",
  "limit": 20
}
```

#### 最近结果（peek）

```json
{
  "recent": 10
}
```

#### 分页（peek）

```json
{
  "query": "发票",
  "limit": 20,
  "offset": 20
}
```

#### 查看完整描述（full）

当你从 peek 列表中找到目标结果后，用 `result_id` 查询完整信息：

```json
{
  "result_id": "res_xxx"
}
```

### 5. 批量导出

当你需要批量处理大量结果（例如交给 Python 脚本分类、移动、统计）时，调用 `vision_export`：

```json
{
  "path": "C:/Users/me/Pictures",
  "fmt": "json",
  "limit": 10000
}
```

工具会把结果写入本地文件，并返回文件路径。之后你可以：

- 用 Python 读取该文件并批量处理。
- 或继续用 `vision_query` 翻页查看。

- 结果太多 → 加 `limit` 或更精确的 `query`
- 想看某条完整结果 → 用 `result_id` 精确查询
- 分类不准 → 调整 `question` 重新调用 `vision_read`（同内容会命中缓存，只重读新规则）

## 分类移动场景

当用户想根据图片内容移动文件时：

1. `vision_read(paths=["/source/folder"], question="判断图片类别：invoice、screenshot、photo、other，并提取关键文字")`
2. `vision_query(query="invoice")` 获取发票列表
3. `vision_query(query="screenshot")` 获取截图列表
4. 输出分类 → 文件路径的映射，由外部系统或用户执行移动

## 注意事项

- 不要等 `vision_read` 返回每张图的详细内容，它只返回摘要。
- 不要重复读已经缓存的图，命中缓存时会直接跳过。
- 查询时优先用自然语言关键词，或按 `path`/`filename` 查询。
- 路径可以是相对路径或绝对路径，文件夹会自动递归识别常见图片格式。
- 只处理图片：`png`、`jpg`、`jpeg`、`webp`、`gif`、`bmp`。

## 反模式

- ❌ 调用 `vision_read` 后期待返回所有图片的详细描述。
- ❌ 不查缓存直接让 VL 模型重复读同一张图。
- ❌ 把大量图片结果塞进上下文，而不是用 `vision_query` 分批查询。
- ❌ 用 `vision_query` 的 `sha256` 字段搜索（已移除）。
- ❌ 把 `vision_read` 和 `vision_query` 合并成一次调用。
