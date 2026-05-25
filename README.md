# AiLex Platform

AI Agent 全栈基础设施 — **全球首个基于 12-Factor Agents 框架的生产级实现**

一键部署 8 大服务：Gateway / OpenClaw Agent / Web UI / Memory / Media / Self-Learning / Self-Security / Billing

## 架构

```
                  用户入口
         Web UI :3001 │ API :8080 │ 多渠道
                      │
        ┌─────────────▼──────────────────┐
        │    AiLex Core — OpenClaw Agent  │
        │  技能编排 │ 工具链 │ 记忆       │
        └─────────────┬──────────────────┘
                      │
        ┌─────────────▼──────────────────┐
        │  AiLex Gateway — API 路由器     │
        │  604+ 模型 · 负载均衡 · 成本追踪 │
        └─────────────┬──────────────────┘
                      │
   ┌────────┬─────────┼─────────┬────────┐
   ▼        ▼         ▼         ▼        ▼
 Memory   Media   Self-Learn  Secure  Billing
 :8090    :8091    :8092      :8093   :8094
```

## 8 大服务

| 服务 | 端口 | 功能 |
|------|------|------|
| Gateway | 8080 | 万量引擎 API 路由 (604 模型) |
| OpenClaw | 3000 | Agent 引擎 (19万⭐ 开源框架) |
| Web UI | 3001 | 管理面板 |
| Memory | 8090 | 三层超长记忆体 (短期/长期/语义) |
| Media | 8091 | 视频音频生成 (100+ 模型) |
| Self-Learning | 8092 | 自动经验提取 + Prompt优化 |
| Self-Security | 8093 | 安全审计 + 入侵检测 |
| Billing | 8094 | 支付 + API Key 授权 + 设备绑定 |

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/4565260/ailex-platform.git
cd ailex-platform

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 MMI_API_KEY

# 3. 一键启动
docker compose up -d

# 4. 访问
# 管理面板: http://localhost:3001
# API: http://localhost:8080/v1
```

## 12-Factor Agents 合规

| Factor | 状态 | 实现 |
|--------|------|------|
| 1 NL→Tools | ✅ | OpenAI 兼容 function calling |
| 2 Own Prompts | ✅ | 显式 config.yaml 配置 |
| 3 Own Context | ✅ | 自动压缩 + 滑动窗口 |
| 4 Tools=Structured | ✅ | JSON Schema 工具定义 |
| 5 统一状态 | ✅ | Memory SQLite 持久化 |
| 6 暂停/恢复 | ✅ | pause/resume API |
| 9 压缩错误 | ✅ | 结构化错误响应 |
| 10 小聚焦 Agent | ⚠️ | 多Agent编排推进中 |
| 11 任意触发 | ⚠️ | Webhook推进中 |
| 12 无状态 | ✅ | Memory 纯函数设计 |

## 开源许可

MIT License
