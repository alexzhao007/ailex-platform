一个人搞了个 AI Agent 基础设施，12-Factor 合规，开源了

---

几个月前我读了 Dex Horthy 的《12-Factor Agents》，醍醐灌顶。

核心洞见很简单：
Agent 首先是软件，AI 只是组件。把 Agent 当成魔法框架来写，必死。当成软件工程来设计，能活。

于是我一个人用业余时间，把这 12 条原则全部变成了可运行的代码。

项目叫 AiLex Platform，今天开源，推到了 GitHub。

https://github.com/alexzhao007/ailex-platform

9 个微服务，31 个文件，Docker Compose 一键部署。

简单说下每个服务做什么：

Gateway (:8080) — API 路由器，接入了 604 个模型。OpenAI、Claude、DeepSeek、Kling、Veo、Sora 随便切。
Memory (:8090) — 三层记忆：短期窗口 + 长期 SQLite + 语义向量搜索。Agent 有了人脑级别的记忆。
Media (:8091) — 视频生成 + TTS。接入了 Kling/Veo/Sora/Vidu/Wan 全部 100+ 媒体模型。
Self-Learning (:8092) — 自动从对话提取经验、优化 prompt、构建知识图谱、遗忘低价值记忆。
Self-Security (:8093) — 端口扫描、文件权限检查、入侵检测、自动修复。
Billing (:8094) — 支付 + API Key 授权 + 一台 Key 只绑一台设备。开源即变现。
Orchestrator (:8095) — DAG 多 Agent 编排引擎，支持拓扑排序和条件分支。
OpenClaw (:3000) — 19 万星的开源 Agent 框架。
Web UI (:3001) — 管理面板。

12-Factor 逐条情况：

1 ✅ NL→Tool Calls — OpenAI 兼容 API
2 ✅ Own Prompts — config.yaml 显式配置
3 ✅ Own Context — Memory 自动压缩摘要
4 ✅ Tools=Structured — JSON Schema
5 ✅ 统一状态 — SQLite 持久化
6 ✅ 暂停/恢复 — pause/resume API
7 ✅ 人类介入 — approval 请求/响应/回调
8 ✅ 控制流 — 显式路由逻辑
9 ✅ 压缩错误 — 结构化错误返回
10 ✅ 小聚焦 Agent — DAG 多 Agent 编排
11 ✅ 任意触发 — Webhook + Cron
12 ✅ 无状态 Reducer — 纯函数设计

从 50% 合规开始，迭代到了 100%。

坦率说，一个人的力量有限。UI 还简陋，文档还不全，Bug 肯定有。
但核心架构已经验证了——12 条原则是可落地的，一个人也能搭起一套生产级的 AI 基础设施。

接下来会补支付配置（¥99/月起），一边开源一边变现。
也欢迎 PR，欢迎 star，欢迎骂。

GitHub: https://github.com/alexzhao007/ailex-platform

（服务器跑在这个 CVM 上，IP 就不放了，怕被打。）
