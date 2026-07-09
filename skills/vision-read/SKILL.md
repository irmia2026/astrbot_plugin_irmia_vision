---
name: vision-read
description: >
  主动读图工作流。触发：用户提到图片、文件夹、截图、发票、UI、照片、视频帧等需要视觉理解的场景。
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

- 用户说"帮我看看这些图"、"读一下这个文件夹里的图片"
- 用户提到"发票"、"截图"、"UI"、"照片"、"视频帧"
- 任务需要对图片进行分类、筛选、提取文字、定位内容
- 用户想根据图片内容移动/整理文件

## 标准工作流

### 1. 读图

调用 `vision_read`：

```json
{
  "paths": ["/path/to/file.png", "/path/to/folder"]
}
```

默认不需要传 `question`，工具会自动使用专业图片描述 prompt 读取图片。只有当你需要针对图片提出特定问题时，才传 `question`。

追问模式：

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
- 只有需要针对图片提特定问题时，才传 `question`，例如"判断是发票、截图、照片还是其他，并提取关键文字"。
- 同一个 `question` 会命中缓存；换问题会重新读图。
想追问细节时（仅适用于**同一张图片**，即 `paths` 只包含那张图）：

- 想换角度重新读时，传 `force_reread=true` 忽略缓存。
- `previous_result_id` 只对与之前同一张图片（sha256 相同）生效；如果传了多个不同图片，只有匹配的那张会进入追问模式，其他图正常读取。

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

如果读图失败，会返回 `status: "partial"` 或 `failed"`，并附带 `failed_paths` 说明哪些文件出错。

### 3. 查询结果

根据任务需求调用 `vision_query`：

#### 自然语言搜索

```json
{
  "query": "发票",
  "limit": 20
}
```

#### 按文件名查询

```json
{
  "filename": "invoice_001.png"
}
```

#### 按路径查询

```json
{
  "path": "C:/Users/me/Pictures",
  "limit": 20
}
```

#### 最近结果

```json
{
  "recent": 10
}
```

#### 分页

```json
{
  "query": "发票",
  "limit": 20,
  "offset": 20
}
```

### 4. 迭代深入

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
