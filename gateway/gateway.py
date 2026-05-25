"""
AiLex Gateway — 万量引擎 API 路由器
完全兼容 OpenAI API 格式，支持 617+ 模型一键切换
12-Factor Agents 合规设计：
  - Factor 1 ✅ NL → Tool Calls
  - Factor 4 ✅ Tools as Structured Outputs
  - Factor 8 ✅ Own Control Flow
  - Factor 9 ✅ Compact Errors (v2.1)
  - Factor 12 ✅ Stateless Reducer
"""

import os
import json
import time
import asyncio
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
