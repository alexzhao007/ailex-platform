"""
AiLex Gateway — 万量引擎 API 路由器
完全兼容 OpenAI API 格式，支持 617+ 模型一键切换
12-Factor Agents 合规设计：
  - Factor 1 ✅ NL → Tool Calls
  - Factor 4 ✅ Tools as Structured Outputs
  - Factor 7 ✅ Human-in-the-Loop (v3.2)
  - Factor 8 ✅ Own Control Flow
  - Factor 9 ✅ Compact Errors (v2.1)
  - Factor 10 ✅ Orchestrator (v3.2)
  - Factor 11 ✅ Webhook Triggers (v3.2)
  - Factor 12 ✅ Stateless Reducer
"""

import os
import json
import time
import asyncio
import hmac
import hashlib
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
import httpx

load_dotenv()

# ── Config ──
CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yaml")

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

config = load_config()
MMI_API_KEY = os.getenv("MMI_API_KEY", "")
MMI_BASE_URL = os.getenv("MMI_BASE_URL", "https://millionengine.com/v1")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-4o")

# ── Stats ──
stats = {
    "requests_total": 0,
    "tokens_total": 0,
    "models_used": {},
    "errors_total": 0,
    "start_time": time.time(),
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"AiLex Gateway started — default model: {DEFAULT_MODEL}")
    print(f"Backend: {MMI_BASE_URL}")
    yield

app = FastAPI(title="AiLex Gateway", version="2.0.0", lifespan=lifespan)

# ── Models ──
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    top_p: Optional[float] = None

# ── Helpers ──
def get_task_from_messages(messages) -> str:
    """Heuristic: detect task type from messages"""
    combined = " ".join(m.content.lower() if isinstance(m.content, str) else str(m.content) for m in messages)
    if any(w in combined for w in ["write code", "def ", "function", "python", "javascript", "bug", "debug"]):
        return "code"
    if any(w in combined for w in ["translate", "translation"]):
        return "translation"
    if any(w in combined for w in ["reason", "think step by step", "explain"]):
        return "reasoning"
    return "chat"

def resolve_model(model: str) -> str:
    """Resolve model aliases and task routing"""
    task = get_task_from_messages([ChatMessage(role="user", content=model)])
    task_routing = config.get("routing", {}).get("task_routing", {})
    if model in task_routing:
        return task_routing[model]
    # Check if model is a known millionengine model; if not, fallback
    return model

# ── API Routes ──

@app.get("/health")
async def health():
    uptime = int(time.time() - stats["start_time"])
    return {
        "status": "ok",
        "uptime_seconds": uptime,
        "stats": {
            "requests": stats["requests_total"],
            "tokens": stats["tokens_total"],
            "errors": stats["errors_total"],
            "models_used": stats["models_used"],
        },
    }

@app.get("/v1/models")
async def list_models():
    """Proxy model list from millionengine"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{MMI_BASE_URL}/models",
            headers={"Authorization": f"Bearer {MMI_API_KEY}"},
        )
        if resp.status_code != 200:
            # Fallback: return known models
            models_data = {
                "object": "list",
                "data": [
                    {"id": "gpt-4o", "object": "model"},
                    {"id": "gpt-4o-mini", "object": "model"},
                    {"id": "claude-sonnet-4-20250514", "object": "model"},
                    {"id": "claude-haiku-4-5", "object": "model"},
                    {"id": "claude-opus-4-7", "object": "model"},
                    {"id": "deepseek-v4-flash", "object": "model"},
                    {"id": "deepseek-v4-pro", "object": "model"},
                    {"id": "deepseek-r1", "object": "model"},
                    {"id": "kimi-k2.6", "object": "model"},
                    {"id": "grok-4", "object": "model"},
                    {"id": "gpt-5.2-codex", "object": "model"},
                ]
            }
            return models_data
        return resp.json()

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest, raw_request: Request):
    """OpenAI-compatible chat completions endpoint"""
    stats["requests_total"] += 1

    model = resolve_model(request.model)
    stats["models_used"][model] = stats["models_used"].get(model, 0) + 1

    # Rate limiting check
    rate_limit = config.get("cost_control", {}).get("rate_limit", {})
    if stats["requests_total"] > 0 and stats["requests_total"] % 100 == 0:
        rps = stats["requests_total"] / (time.time() - stats["start_time"])
        max_rpm = rate_limit.get("requests_per_minute", 500)
        if rps * 60 > max_rpm:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

    payload = {
        "model": model,
        "messages": [m.model_dump() for m in request.messages],
        **({"temperature": request.temperature} if request.temperature is not None else {}),
        **({"max_tokens": request.max_tokens} if request.max_tokens is not None else {}),
        **({"top_p": request.top_p} if request.top_p is not None else {}),
    }

    headers = {
        "Authorization": f"Bearer {MMI_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=300) as client:
        try:
            if request.stream:
                return await handle_stream(client, payload, headers)
            else:
                return await handle_nonstream(client, payload, headers)
        except httpx.TimeoutException:
            stats["errors_total"] += 1
            # Factor 9: Compact Error
            return JSONResponse(status_code=504, content={
                "error": {
                    "type": "upstream_timeout",
                    "message": "模型响应超时，请稍后重试或切换到更快的模型（如 gpt-4o-mini）",
                    "recovery": "retry_or_switch_model",
                }
            })
        except Exception as e:
            stats["errors_total"] += 1
            error_msg = str(e)[:200]
            # Factor 9: Compact Error - structured, human-readable
            error_type = "unknown"
            recovery = "retry"
            if "401" in error_msg or "unauthorized" in error_msg.lower():
                error_type = "auth_failed"
                recovery = "check_api_key"
            elif "429" in error_msg or "rate" in error_msg.lower():
                error_type = "rate_limited"
                recovery = "wait_and_retry"
            elif "timeout" in error_msg.lower():
                error_type = "timeout"
                recovery = "retry_or_switch_model"

            return JSONResponse(status_code=502, content={
                "error": {
                    "type": error_type,
                    "message": error_msg,
                    "recovery": recovery,
                }
            })

async def handle_nonstream(client, payload, headers) -> dict:
    resp = await client.post(
        f"{MMI_BASE_URL}/chat/completions",
        json=payload,
        headers=headers,
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Upstream error: {resp.text[:500]}",
        )
    result = resp.json()
    if "usage" in result:
        stats["tokens_total"] += result["usage"].get("total_tokens", 0)
    return result

async def handle_stream(client, payload, headers):
    payload["stream"] = True
    resp = await client.post(
        f"{MMI_BASE_URL}/chat/completions",
        json=payload,
        headers=headers,
    )

    async def generate():
        try:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    yield line + "\n\n"
                    if line.strip() == "data: [DONE]":
                        break
        except Exception:
            pass

    return StreamingResponse(generate(), media_type="text/event-stream")

@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """Proxy embeddings to millionengine"""
    body = await request.json()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{MMI_BASE_URL}/embeddings",
            json=body,
            headers={"Authorization": f"Bearer {MMI_API_KEY}"},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
        return resp.json()

# ══════════════════════════════════════
# Factor 7: Human-in-the-Loop — 人类介入审批
# ══════════════════════════════════════

PENDING_APPROVALS = {}  # approval_id -> request data

class ApprovalRequest(BaseModel):
    action: str
    context: dict
    agent_name: Optional[str] = None
    callback_url: Optional[str] = None
    timeout_seconds: int = 300

@app.post("/v1/approval/request")
async def request_approval(req: ApprovalRequest):
    """Agent 请求人工审批 — 挂起执行等待审批"""
    approval_id = f"aprv_{hashlib.md5(f'{time.time()}{req.action}'.encode()).hexdigest()[:8]}"
    now = time.time()
    PENDING_APPROVALS[approval_id] = {
        "id": approval_id,
        "action": req.action,
        "context": req.context.model_dump() if hasattr(req.context, 'model_dump') else req.context,
        "agent": req.agent_name or "unknown",
        "callback_url": req.callback_url,
        "status": "pending",
        "created_at": now,
        "expires_at": now + req.timeout_seconds,
    }
    return {
        "approval_id": approval_id,
        "status": "pending",
        "message": f"Approval requested for: {req.action}",
        "note": "Use POST /v1/approval/respond to approve or reject",
    }

class ApprovalResponse(BaseModel):
    approval_id: str
    approved: bool
    feedback: Optional[str] = None

@app.post("/v1/approval/respond")
async def respond_approval(req: ApprovalResponse):
    """人工响应审批"""
    approval = PENDING_APPROVALS.get(req.approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval {req.approval_id} not found or expired")
    
    if approval["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Already {approval['status']}")
    
    now = time.time()
    if now > approval["expires_at"]:
        approval["status"] = "expired"
        return {"status": "expired", "message": "Approval request expired"}
    
    approval["status"] = "approved" if req.approved else "rejected"
    approval["responded_at"] = now
    approval["feedback"] = req.feedback
    
    # Call callback if configured
    if approval.get("callback_url"):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(approval["callback_url"], json={
                    "approval_id": req.approval_id,
                    "approved": req.approved,
                    "feedback": req.feedback,
                })
        except:
            pass
    
    return {
        "approval_id": req.approval_id,
        "status": approval["status"],
        "message": f"Action {'approved' if req.approved else 'rejected'}: {approval['action']}",
    }

@app.get("/v1/approval/pending")
async def list_pending_approvals():
    """列出待审批请求"""
    now = time.time()
    # Clean expired
    expired = [k for k, v in PENDING_APPROVALS.items() if now > v["expires_at"]]
    for k in expired:
        PENDING_APPROVALS[k]["status"] = "expired"
    
    pending = [v for v in PENDING_APPROVALS.values() if v["status"] == "pending"]
    return {"pending": pending, "count": len(pending)}

# ══════════════════════════════════════
# Factor 11: Webhook — 任意触发
# ══════════════════════════════════════

# In-memory webhook store (use DB in production)
WEBHOOKS = {}
CRON_JOBS = {}

class WebhookRegister(BaseModel):
    url: str
    secret: Optional[str] = None
    events: List[str] = ["*"]  # event types to listen for
    description: Optional[str] = None

@app.post("/v1/webhooks")
async def register_webhook(wh: WebhookRegister):
    """注册 Webhook 接收端"""
    webhook_id = f"wh_{hashlib.md5(wh.url.encode()).hexdigest()[:8]}"
    WEBHOOKS[webhook_id] = {
        "id": webhook_id,
        "url": wh.url,
        "secret": wh.secret,
        "events": wh.events,
        "description": wh.description,
        "created_at": time.time(),
    }
    return {"webhook_id": webhook_id, "url": wh.url, "events": wh.events}

@app.post("/v1/webhooks/{webhook_id}/test")
async def test_webhook(webhook_id: str):
    """测试 Webhook"""
    wh = WEBHOOKS.get(webhook_id)
    if not wh:
        raise HTTPException(status_code=404)
    
    payload = {
        "event": "test",
        "timestamp": time.time(),
        "data": {"message": "This is a test webhook from AiLex Gateway"},
    }
    
    signature = ""
    if wh.get("secret"):
        signature = hmac.new(wh["secret"].encode(), json.dumps(payload).encode(), hashlib.sha256).hexdigest()
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                wh["url"],
                json=payload,
                headers={
                    "X-Webhook-Signature": signature,
                    "X-Webhook-Event": "test",
                },
            )
            return {"status": resp.status_code, "success": resp.status_code < 400}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}

@app.post("/v1/webhooks/{webhook_id}/trigger")
async def trigger_webhook(webhook_id: str, event: str = "custom", data: dict = None):
    """触发 Webhook 事件"""
    wh = WEBHOOKS.get(webhook_id)
    if not wh:
        raise HTTPException(status_code=404)
    
    if "*" not in wh["events"] and event not in wh["events"]:
        return {"skipped": True, "reason": f"Event '{event}' not subscribed"}
    
    payload = {
        "event": event,
        "timestamp": time.time(),
        "data": data or {},
    }
    
    signature = ""
    if wh.get("secret"):
        signature = hmac.new(wh["secret"].encode(), json.dumps(payload).encode(), hashlib.sha256).hexdigest()
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                wh["url"],
                json=payload,
                headers={
                    "X-Webhook-Signature": signature,
                    "X-Webhook-Event": event,
                },
            )
            return {"status": resp.status_code, "success": resp.status_code < 400}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}

# ══════════════════════════════════════
# Factor 11: Cron — 定时任务
# ══════════════════════════════════════

class CronJob(BaseModel):
    name: str
    schedule: str  # cron expression: "*/5 * * * *"
    webhook_url: str
    payload: Optional[dict] = None
    enabled: bool = True

@app.post("/v1/cron")
async def create_cron(cron: CronJob):
    """创建定时任务"""
    job_id = f"cron_{hashlib.md5(cron.name.encode()).hexdigest()[:8]}"
    CRON_JOBS[job_id] = {
        "id": job_id,
        "name": cron.name,
        "schedule": cron.schedule,
        "webhook_url": cron.webhook_url,
        "payload": cron.payload or {},
        "enabled": cron.enabled,
        "last_run": None,
        "created_at": time.time(),
    }
    return {
        "cron_id": job_id,
        "name": cron.name,
        "schedule": cron.schedule,
        "note": "Cron jobs need an external scheduler (e.g., systemd timer / k8s cronjob) to fire. See docs.",
    }

@app.get("/v1/cron")
async def list_cron():
    """列出定时任务"""
    return {"cron_jobs": list(CRON_JOBS.values())}

@app.get("/v1/cron/next-runs")
async def next_cron_runs():
    """计算下次运行时间（仅显示配置）"""
    return {
        "note": "Cron jobs fire based on external scheduler.",
        "jobs": [{"id": k, "name": v["name"], "schedule": v["schedule"], "enabled": v["enabled"]}
                 for k, v in CRON_JOBS.items()]
    }

@app.get("/v1/dashboard/stats")
async def dashboard_stats():
    """Get usage statistics for the admin dashboard"""
    uptime = int(time.time() - stats["start_time"])
    return {
        "uptime_seconds": uptime,
        "uptime_hours": round(uptime / 3600, 1),
        "total_requests": stats["requests_total"],
        "total_tokens": stats["tokens_total"],
        "total_errors": stats["errors_total"],
        "estimated_cost_usd": round(stats["tokens_total"] * 0.000002, 2),
        "models_ranked": sorted(
            stats["models_used"].items(), key=lambda x: x[1], reverse=True
        ),
        "models_available": config.get("routing", {}).get("task_routing", {}),
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

# ══════════════════════════════════════
# Factor 7 EXT: 多渠道通知网关
# ══════════════════════════════════════

# 企业微信 Bot Webhook
WECOM_BOT_URL = os.getenv("WECOM_BOT_URL", "")
WECOM_APP_NOTIFY = os.getenv("WECOM_APP_NOTIFY", "")

def get_notify_channels():
    """获取配置的通知渠道"""
    channels = []
    if WECOM_BOT_URL:
        channels.append("wecom_bot")
    if WECOM_APP_NOTIFY:
        channels.append("wecom_app")
    channels.append("callback")  # callback 始终可用
    return channels

@app.get("/v1/notify/channels")
async def list_notify_channels():
    """列出已配置的通知渠道"""
    return {
        "channels": get_notify_channels(),
        "note": "Channels are configured via environment variables (WECOM_BOT_URL, WECOM_APP_NOTIFY)"
    }

async def notify_wecom_bot(action: str, context: dict, approval_id: str):
    """通过企业微信群机器人发送审批通知"""
    if not WECOM_BOT_URL:
        return False
    try:
        msg = {
            "msgtype": "markdown",
            "markdown": {
                "content": (
                    f"### 🤖 AiLex 请求审批\n"
                    f"**操作**: {action}\n"
                    f"**审批ID**: {approval_id}\n"
                    f"**上下文**: {json.dumps(context, ensure_ascii=False)[:500]}\n"
                    f"\n"
                    f"**审批链接**: http://127.0.0.1:8080/v1/approval/respond?approval_id={approval_id}\n"
                    f"\n"
                    f"> 使用 `POST /v1/approval/respond` 审批\n"
                    f"> `{{\"approval_id\":\"{approval_id}\", \"approved\": true, \"feedback\":\"ok\"}}`"
                )
            }
        }
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(WECOM_BOT_URL, json=msg)
            return resp.status_code == 200
    except Exception as e:
        print(f"[WECOM_BOT] notify failed: {e}")
        return False

# 给 request_approval 增加通知 (patch 原函数)
# 在 approval 创建后自动通知
ORIG_REQUEST_APPROVAL = request_approval

