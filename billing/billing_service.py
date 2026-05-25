#!/usr/bin/env python3
"""
AiLex Billing — 支付与 API Key 授权系统
设备绑定 + 用量控制 + 收款对接

架构：
  用户下单 → 收款确认 → 生成 API Key → 绑定设备 → 限流使用

适配的收款方：
  - 支付宝当面付（个人）
  - 微信支付（个人/商户）
  - Stripe（全球）
"""

import os
import json
import time
import sqlite3
import hashlib
import secrets
import re
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request, Header
from pydantic import BaseModel
import httpx

# ── Config ──
DB_PATH = os.getenv("BILLING_DB_PATH", "/app/data/billing.db")
MMI_API_KEY = os.getenv("MMI_API_KEY", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", secrets.token_hex(16))

# Payment provider config
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "none")  # alipay | wechat | stripe | none
ALIPAY_APP_ID = os.getenv("ALIPAY_APP_ID", "")
ALIPAY_PRIVATE_KEY = os.getenv("ALIPAY_PRIVATE_KEY", "")
WECHAT_MCH_ID = os.getenv("WECHAT_MCH_ID", "")
WECHAT_API_KEY = os.getenv("WECHAT_API_KEY", "")
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY", "")
MERCHANT_NOTIFY_URL = os.getenv("MERCHANT_NOTIFY_URL", "")

# Pricing
PRICING = {
    "starter": {"price": 99, "days": 30, "requests_per_day": 1000, "models": "basic"},
    "pro": {"price": 399, "days": 30, "requests_per_day": 10000, "models": "all"},
    "enterprise": {"price": 2999, "days": 365, "requests_per_day": 100000, "models": "all"},
}

# ── DB ──
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            plan TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'CNY',
            status TEXT DEFAULT 'pending',  -- pending | paid | expired | refunded
            provider_order_id TEXT,
            provider TEXT DEFAULT 'none',
            customer_email TEXT,
            customer_phone TEXT,
            created_at REAL,
            paid_at REAL,
            expired_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_provider ON orders(provider_order_id);
        
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            key_hash TEXT UNIQUE NOT NULL,  
            key_prefix TEXT NOT NULL,        -- e.g. "alx_xxxx"
            plan TEXT NOT NULL,
            owner TEXT NOT NULL,             -- email or phone
            status TEXT DEFAULT 'active',    -- active | suspended | expired
            max_devices INTEGER DEFAULT 1,
            device_bindings TEXT DEFAULT '[]', -- JSON array of bound device fingerprints
            requests_today INTEGER DEFAULT 0,
            total_requests INTEGER DEFAULT 0,
            daily_limit INTEGER DEFAULT 1000,
            created_at REAL,
            expires_at REAL,
            last_used REAL
        );
        CREATE INDEX IF NOT EXISTS idx_keys_hash ON api_keys(key_hash);
        CREATE INDEX IF NOT EXISTS idx_keys_owner ON api_keys(owner);
        
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id TEXT,
            endpoint TEXT,
            device_fingerprint TEXT,
            model TEXT,
            tokens_used INTEGER DEFAULT 0,
            status_code INTEGER,
            created_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_log(key_id);
        CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_log(created_at);
        
        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ── Models ──
class OrderRequest(BaseModel):
    plan: str = "starter"
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    provider: str = "alipay"  # alipay | wechat | stripe

class PaymentConfirm(BaseModel):
    provider_order_id: str
    provider: str

class ApiKeyCreate(BaseModel):
    plan: str
    owner: str
    max_devices: int = 1

class DeviceBind(BaseModel):
    api_key: str
    device_fingerprint: str
    device_name: Optional[str] = None

class UsageRequest(BaseModel):
    api_key: str
    endpoint: str = "chat/completions"
    model: str = "gpt-4o"
    device_fingerprint: str = ""

# ══════════════════════════════════════
# App
# ══════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"AiLex Billing started — DB: {DB_PATH}")
    print(f"Pricing: {json.dumps(PRICING, indent=2)}")
    print(f"Payment provider: {PAYMENT_PROVIDER}")
    yield

app = FastAPI(title="AiLex Billing", version="2.0.0", lifespan=lifespan)

def gen_api_key() -> tuple:
    """Generate API key: alx_xxx format"""
    raw = "alx_" + secrets.token_hex(24)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:12] + "..."
    return raw, key_hash, prefix

# ══════════════════════════════════════
# 1. 定价方案
# ══════════════════════════════════════

@app.get("/pricing")
async def get_pricing():
    """获取定价方案"""
    return {"plans": PRICING, "currency": "CNY"}

# ══════════════════════════════════════
# 2. 订单/支付
# ══════════════════════════════════════

@app.post("/order/create")
async def create_order(req: OrderRequest):
    """创建订单"""
    if req.plan not in PRICING:
        raise HTTPException(status_code=400, detail=f"Invalid plan: {req.plan}. Options: {list(PRICING.keys())}")
    
    plan = PRICING[req.plan]
    order_id = "ord_" + secrets.token_hex(12)
    now = time.time()
    
    conn = get_db()
    conn.execute(
        "INSERT INTO orders (id, plan, amount, status, provider, customer_email, customer_phone, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [order_id, req.plan, plan["price"], "pending", req.provider, req.customer_email, req.customer_phone, now]
    )
    conn.commit()
    conn.close()
    
    # Generate payment URL/method based on provider
    payment_info = {
        "alipay": {"type": "qrcode", "note": "Generate Alipay QR code with order"},
        "wechat": {"type": "qrcode", "note": "Generate WeChat Pay QR code with order"},
        "stripe": {"type": "checkout_url", "note": "Generate Stripe Checkout session link"},
    }.get(req.provider, {"type": "manual", "note": "Manual payment"})
    
    return {
        "order_id": order_id,
        "plan": req.plan,
        "amount": plan["price"],
        "currency": "CNY",
        "payment": payment_info,
        "note": f"Send this payment info to your payment system. After payment, POST /order/confirm with provider_order_id.",
    }

@app.post("/order/confirm")
async def confirm_payment(req: PaymentConfirm):
    """确认支付（收到支付回调后调用）"""
    conn = get_db()
    order = conn.execute(
        "SELECT * FROM orders WHERE provider_order_id=? AND status='pending'",
        [req.provider_order_id]
    ).fetchone()
    
    if not order:
        conn.close()
        raise HTTPException(status_code=404, detail="Order not found or already paid")
    
    # Calculate expiry
    plan_config = PRICING.get(order["plan"], PRICING["starter"])
    now = time.time()
    expires_at = now + plan_config["days"] * 86400
    
    # Mark order as paid
    conn.execute(
        "UPDATE orders SET status='paid', paid_at=? WHERE id=?",
        [now, order["id"]]
    )
    
    # Generate API key automatically
    api_key_raw, key_hash, key_prefix = gen_api_key()
    key_id = "key_" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO api_keys (id, key_hash, key_prefix, plan, owner, max_devices, daily_limit, created_at, expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
        [key_id, key_hash, key_prefix, order["plan"], order.get("customer_email") or order.get("customer_phone") or "unknown",
         1, plan_config["requests_per_day"], now, expires_at]
    )
    
    conn.commit()
    conn.close()
    
    return {
        "status": "paid",
        "order_id": order["id"],
        "plan": order["plan"],
        "api_key": api_key_raw,
        "api_key_display": key_prefix,
        "expires_at": expires_at,
        "note": "Save your API key! It will not be shown again.",
    }

@app.get("/order/{order_id}")
async def get_order(order_id: str):
    """查询订单"""
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", [order_id]).fetchone()
    conn.close()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return dict(order)

# ══════════════════════════════════════
# 3. API Key 管理 + 设备绑定
# ══════════════════════════════════════

@app.post("/key/create")
async def create_api_key(req: ApiKeyCreate, authorization: str = Header(None)):
    """管理员创建 API Key"""
    # Simple admin check  
    if authorization != f"Bearer {ADMIN_TOKEN}":
        # Allow order-based creation via payment
        raise HTTPException(status_code=403, detail="Admin access required. Create via /order/create")
    
    api_key_raw, key_hash, key_prefix = gen_api_key()
    key_id = "key_" + secrets.token_hex(8)
    now = time.time()
    plan_config = PRICING.get(req.plan, PRICING["starter"])
    
    conn = get_db()
    conn.execute(
        "INSERT INTO api_keys (id, key_hash, key_prefix, plan, owner, max_devices, daily_limit, created_at, expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
        [key_id, key_hash, key_prefix, req.plan, req.owner, req.max_devices, plan_config["requests_per_day"], now, now + plan_config["days"] * 86400]
    )
    conn.commit()
    conn.close()
    
    return {"api_key": api_key_raw, "key_prefix": key_prefix, "plan": req.plan}

@app.post("/key/bind")
async def bind_device(req: DeviceBind):
    """绑定设备 — 一台 API Key 仅限一台设备"""
    key_hash = hashlib.sha256(req.api_key.encode()).hexdigest()
    conn = get_db()
    
    key = conn.execute("SELECT * FROM api_keys WHERE key_hash=?", [key_hash]).fetchone()
    if not key:
        conn.close()
        raise HTTPException(status_code=404, detail="Invalid API key")
    
    if key["status"] != "active":
        conn.close()
        raise HTTPException(status_code=403, detail="API key is not active")
    
    now = time.time()
    if key["expires_at"] and now > key["expires_at"]:
        conn.close()
        raise HTTPException(status_code=403, detail="API key expired")
    
    # Check device bindings
    bindings = json.loads(key["device_bindings"])
    
    # If device already bound, update name
    for b in bindings:
        if b["fingerprint"] == req.device_fingerprint:
            if req.device_name:
                b["name"] = req.device_name
                conn.execute("UPDATE api_keys SET device_bindings=? WHERE id=?", [json.dumps(bindings), key["id"]])
                conn.commit()
            conn.close()
            return {"bound": True, "note": "Device already registered"}
    
    # New device binding
    if len(bindings) >= key["max_devices"]:
        conn.close()
        raise HTTPException(status_code=403, detail=f"Max devices ({key['max_devices']}) reached. Current: {[b.get('name','') for b in bindings]}")
    
    bindings.append({
        "fingerprint": req.device_fingerprint,
        "name": req.device_name or f"Device-{len(bindings)+1}",
        "bound_at": now,
    })
    
    conn.execute("UPDATE api_keys SET device_bindings=? WHERE id=?", [json.dumps(bindings), key["id"]])
    conn.commit()
    conn.close()
    
    return {"bound": True, "device_fingerprint": req.device_fingerprint, "total_devices": len(bindings)}

@app.get("/key/status")
async def check_key_status(api_key: str = "", device_fingerprint: str = ""):
    """查询 API Key 状态"""
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key required")
    
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    conn = get_db()
    
    key = conn.execute("SELECT * FROM api_keys WHERE key_hash=?", [key_hash]).fetchone()
    if not key:
        conn.close()
        raise HTTPException(status_code=404, detail="Invalid API key")
    
    # Check device binding if fingerprint provided
    device_ok = True
    if device_fingerprint:
        bindings = json.loads(key["device_bindings"])
        device_ok = any(b["fingerprint"] == device_fingerprint for b in bindings)
    
    now = time.time()
    expired = key["expires_at"] and now > key["expires_at"]
    
    conn.close()
    return {
        "plan": key["plan"],
        "status": key["status"],
        "active": key["status"] == "active" and not expired,
        "expired": bool(expired),
        "expires_at": key["expires_at"],
        "bound_devices": json.loads(key["device_bindings"]),
        "device_verified": device_ok,
        "requests_today": key["requests_today"],
        "daily_limit": key["daily_limit"],
        "total_requests": key["total_requests"],
    }

@app.get("/admin/api-keys")
async def list_api_keys(authorization: str = Header(None)):
    """管理员列出所有 Key"""
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    conn = get_db()
    keys = conn.execute("SELECT id, key_prefix, plan, owner, status, max_devices, device_bindings, requests_today, daily_limit, total_requests, created_at, expires_at FROM api_keys ORDER BY created_at DESC LIMIT 50").fetchall()
    conn.close()
    
    return {"keys": [dict(k) for k in keys]}

# ══════════════════════════════════════
# 4. API 使用验证（集成到 Gateway）
# ══════════════════════════════════════

@app.post("/verify")
async def verify_and_use(req: UsageRequest):
    """验证 API Key + 设备绑定 + 减少配额 — Gateway 中间件用"""
    if not req.api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    key_hash = hashlib.sha256(req.api_key.encode()).hexdigest()
    conn = get_db()
    
    key = conn.execute("SELECT * FROM api_keys WHERE key_hash=?", [key_hash]).fetchone()
    if not key:
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Check status
    if key["status"] != "active":
        conn.close()
        raise HTTPException(status_code=403, detail=f"API key status: {key['status']}")
    
    # Check expiry
    now = time.time()
    if key["expires_at"] and now > key["expires_at"]:
        conn.close()
        raise HTTPException(status_code=403, detail="API key expired")
    
    # Check device binding
    if req.device_fingerprint:
        bindings = json.loads(key["device_bindings"])
        if not any(b["fingerprint"] == req.device_fingerprint for b in bindings):
            conn.close()
            raise HTTPException(
                status_code=403,
                detail=f"Device not bound. Register first with POST /key/bind. Your fingerprint: {req.device_fingerprint}"
            )
    
    # Check daily limit
    # Reset counter if new day
    key_today = key["requests_today"]
    
    if key_today >= key["daily_limit"]:
        conn.close()
        raise HTTPException(status_code=429, detail=f"Daily limit ({key['daily_limit']}) reached. Upgrade your plan.")
    
    # Consume quota
    conn.execute(
        "UPDATE api_keys SET requests_today=requests_today+1, total_requests=total_requests+1, last_used=? WHERE id=?",
        [now, key["id"]]
    )
    
    # Log usage
    conn.execute(
        "INSERT INTO usage_log (key_id, endpoint, device_fingerprint, model, created_at) VALUES (?, ?, ?, ?, ?)",
        [key["id"], req.endpoint, req.device_fingerprint, req.model, now]
    )
    
    conn.commit()
    conn.close()
    
    return {
        "verified": True,
        "plan": key["plan"],
        "requests_remaining": key["daily_limit"] - key_today - 1,
        "expires_at": key["expires_at"],
    }

@app.get("/usage/{key_id}")
async def get_usage(key_id: str, days: int = 7):
    """查询使用统计"""
    conn = get_db()
    cutoff = time.time() - days * 86400
    logs = conn.execute(
        "SELECT DATE(created_at, 'unixepoch') as date, COUNT(*) as count, COUNT(DISTINCT device_fingerprint) as devices FROM usage_log WHERE key_id=? AND created_at>? GROUP BY date ORDER BY date",
        [key_id, cutoff]
    ).fetchall()
    conn.close()
    
    return {"key_id": key_id, "days": days, "daily_usage": [dict(r) for r in logs]}

# ══════════════════════════════════════
# 5. 余额/用量重置
# ══════════════════════════════════════

@app.post("/admin/reset-quota/{key_id}")
async def reset_quota(key_id: str, authorization: str = Header(None)):
    """管理员重置每日配额"""
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    conn = get_db()
    conn.execute("UPDATE api_keys SET requests_today=0 WHERE id=?", [key_id])
    conn.commit()
    conn.close()
    return {"reset": True, "key_id": key_id}

# ══════════════════════════════════════
# Stats & Health
# ══════════════════════════════════════

@app.get("/stats")
async def get_stats():
    conn = get_db()
    counts = {
        "total_orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        "paid_orders": conn.execute("SELECT COUNT(*) FROM orders WHERE status='paid'").fetchone()[0],
        "active_keys": conn.execute("SELECT COUNT(*) FROM api_keys WHERE status='active'").fetchone()[0],
        "total_usage_30d": conn.execute(f"SELECT COUNT(*) FROM usage_log WHERE created_at>?", [time.time() - 30*86400]).fetchone()[0],
        "revenue": conn.execute("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status='paid'").fetchone()[0],
    }
    conn.close()
    return counts

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("BILLING_PORT", "8094"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
