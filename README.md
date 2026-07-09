# 弥亚视觉工具

一个 AstrBot 插件，让 LLM 主动获得读图能力。

## 功能

- `vision_read`：读取图片或文件夹中的所有图片，调用 VL 模型理解内容，结果存入本地数据库。
- `vision_query`：查询已读图的结果，支持关键词、文件名、路径、最近结果、分页。

## 安装

1. 将插件放入 AstrBot 的插件目录。
2. 安装依赖：`pip install -r requirements.txt`
3. 在 AstrBot WebUI 的插件配置中填写 VL 模型信息。

## 配置示例

```json
{
  "vl_model": {
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-xxxxxxxx",
    "model": "gpt-4o"
  }
}
```

也支持任何 OpenAI 兼容 API，例如 Gemini、本地 vLLM、OneAPI 等。

## 使用示例

用户说："帮我看看 ~/Pictures 里的图"

1. LLM 调用 `vision_read({"paths": ["~/Pictures"]})`
2. 工具返回读图完成摘要
3. LLM 调用 `vision_query({"recent": 5})` 查看最近结果

## 注意事项

- 只处理 `png/jpg/jpeg/webp/gif/bmp` 图片
- 单张图片最大 20MB
- 同一张图（按内容 hash + 文件名 + 模型 + 问题）读过会命中缓存，不再重复调用 VL 模型

