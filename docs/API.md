# AiLex Platform API 文档

## Overview

完全兼容 OpenAI API 格式。所有支持 OpenAI 的工具（Cursor、Cline、Claude Code、OpenClaw 等）可直接连接。

**Base URL:** `http://localhost:8080/v1`

## Authentication

通过在 HTTP Header 中传递 API Key：

```
Authorization: Bearer <your-mmi-api-key>
```

## Endpoints

### Chat Completions

```
POST /v1/chat/completions
```

请求体格式与 OpenAI 完全一致：

```json
{
  "model": "gpt-4o",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  "temperature": 0.7,
  "stream": false
}
```

支持流式输出（`stream: true`）。

### List Models

```
GET /v1/models
```

返回可用模型列表。

### Embeddings

```
POST /v1/embeddings
```

兼容 OpenAI embeddings API。

### Health Check

```
GET /health
```

返回服务状态和用量统计。

## 在 Cursor 中使用

```
Settings → Models → API Key: <MMI_API_KEY>
Settings → Models → Base URL: http://localhost:8080/v1
```

## 在 Claude Code 中使用

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080/v1
export ANTHROPIC_API_KEY=<MMI_API_KEY>
```

## 在 OpenClaw 中使用

在 openclaw.json 中配置：

```json
{
  "modelProvider": {
    "baseUrl": "http://localhost:8080/v1",
    "apiKey": "<MMI_API_KEY>"
  }
}
```
