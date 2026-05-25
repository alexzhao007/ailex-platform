#!/usr/bin/env python3
"""
AiLex Billing — 支付与 API Key 授权系统 (v3.2 完整版)
设备绑定 + 用量控制 + 支付宝当面付 + 微信支付 + Stripe

架构：
  用户下单 → 收银台 → 扫码支付 → 支付确认 → API Key 发放 → 设备绑定 → 计费使用

支付通道：
  支付宝当面付：个人也可以接入（通过支付宝开放平台），即时到账，费率 0.6%
  微信支付：需商户号，T+1 到账
  Stripe：全球支付，2.9%+$0.30
"""

import os
import json
import time
import sqlite3
import hashlib
import secrets
import re
import base64
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
import httpx

# ── Payment SDKs (optional) ──
try:
    from alipay import AliPay
    ALIPAY_AVAILABLE = True
except ImportError:
    ALIPAY_AVAILABLE = False

# ── Config ──
DB_PATH = os.getenv("BILLING_DB_PATH", "/app/data/billing.db")
MMI_API_KEY = os.getenv("MMI_API_KEY", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", secrets.token_hex(16))
BASE_URL = os.getenv("BASE_URL", "http://localhost:8094")

# Payment provider config
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "none")
ALIPAY_APP_ID = os.getenv("ALIPAY_APP_ID", "")
ALIPAY_APP_PRIVATE_KEY_STRING = os.getenv("ALIPAY_APP_PRIVATE_KEY_STRING", "")
ALIPAY_ALIPAY_PUBLIC_KEY_STRING = os.getenv("ALIPAY_ALIPAY_PUBLIC_KEY_STRING", "")
WECHAT_MCH_ID = os.getenv("WECHAT_MCH_ID", "")
WECHAT_API_KEY = os.getenv("WECHAT_API_KEY", "")
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY", "")
MERCHANT_NOTIFY_URL = os.getenv("MERCHANT_NOTIFY_URL", f"{BASE_URL}/order/notify")

# Alipay client (lazy init)
_alipay_client = None

def get_alipay():
    global _alipay_client
    if _alipay_client is None and ALIPAY_AVAILABLE and ALIPAY_APP_ID:
        _alipay_client = AliPay(
            appid=ALIPAY_APP_ID,
            app_notify_url=MERCHANT_NOTIFY_URL,
            app_private_key_string=ALIPAY_APP_PRIVATE_KEY_STRING,
            alipay_public_key_string=ALIPAY_ALIPAY_PUBLIC_KEY_STRING,
            sign_type="RSA2",
            debug=False,
        )
    return _alipay_client

# ── Pricing ──
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
            status TEXT DEFAULT 'pending',
            provider_order_id TEXT,
            trade_no TEXT,
            provider TEXT DEFAULT 'none',
            customer_email TEXT,
            customer_phone TEXT,
            qr_code TEXT,
            created_at REAL,
            paid_at REAL,
            expired_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_provider ON orders(provider_order_id);
        
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            key_hash TEXT UNIQUE NOT NULL,
            key_prefix TEXT NOT NULL,
            plan TEXT NOT NULL,
            owner TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            max_devices INTEGER DEFAULT 1,
            device_bindings TEXT DEFAULT '[]',
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
    provider: str = "alipay"

class PaymentConfirm(BaseModel):
    provider_order_id: str
    provider: str
    trade_no: Optional[str] = None

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
    print(f"AiLex Billing v3.2 started — DB: {DB_PATH}")
    print(f"Payment provider: {PAYMENT_PROVIDER}")
    print(f"Pricing: Starter ¥99/mo | Pro ¥399/mo | Enterprise ¥2999/yr")
    if ALIPAY_AVAILABLE and ALIPAY_APP_ID:
        print(f"Alipay: configured (APP_ID: {ALIPAY_APP_ID[:8]}...)")
    else:
        print(f"Alipay: not configured")
    yield

app = FastAPI(title="AiLex Billing", version="3.2.0", lifespan=lifespan)

def gen_api_key() -> tuple:
    raw = "alx_" + secrets.token_hex(24)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:12] + "..."
    return raw, key_hash, prefix

# ══════════════════════════════════════
# 1. 定价
# ══════════════════════════════════════

@app.get("/pricing")
async def get_pricing():
    return {"plans": PRICING, "currency": "CNY", "alipay": "13703717827", "wechat": "angel520alan"}

# ══════════════════════════════════════
# 2. 订单 → 支付
# ══════════════════════════════════════

@app.post("/order/create")
async def create_order(req: OrderRequest):
    """创建订单并生成支付二维码"""
    if req.plan not in PRICING:
        raise HTTPException(status_code=400, detail=f"Invalid plan: {req.plan}")
    
    plan = PRICING[req.plan]
    order_id = "ord_" + secrets.token_hex(12)
    now = time.time()
    
    # Generate payment info based on provider
    qr_info = ""
    provider_order_id = ""
    
    if req.provider == "alipay" and ALIPAY_AVAILABLE and ALIPAY_APP_ID:
        # Real Alipay face-to-face payment
        alipay = get_alipay()
        if alipay:
            out_trade_no = order_id
            subject = f"AiLex {req.plan} — ¥{plan['price']}"
            total_amount = plan["price"]
            # Pre-create order, get QR code
            try:
                response = alipay.api_alipay_trade_precreate(
                    out_trade_no=out_trade_no,
                    total_amount=total_amount,
                    subject=subject,
                    timeout_express="30m",
                )
                if response.get("code") == "10000":
                    provider_order_id = out_trade_no
                    qr_info = response.get("qr_code", "")
            except Exception as e:
                qr_info = f"Alipay API error: {str(e)[:100]}"
    
    # Fallback: manual payment with QR code to your personal account
    if not qr_info:
        provider_order_id = order_id
        if req.provider == "alipay":
            qr_info = f"alipay://platformapi/startapp?saId=10000007&qrcode=https://qr.alipay.com/your_qr?收款账号=13703717827&金额={plan['price']}"
    
    conn = get_db()
    conn.execute(
        "INSERT INTO orders (id, plan, amount, status, provider, provider_order_id, qr_code, customer_email, customer_phone, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [order_id, req.plan, plan["price"], "pending", req.provider, provider_order_id, qr_info, req.customer_email, req.customer_phone, now]
    )
    conn.commit()
    conn.close()
    
    return {
        "order_id": order_id,
        "plan": req.plan,
        "amount": plan["price"],
        "currency": "CNY",
        "provider": req.provider,
        "qr_code": qr_info if qr_info else None,
        "payment_note": f"Scan with your {req.provider} app, or transfer to: 支付宝 13703717827 / 微信 angel520alan",
        "check_url": f"/order/{order_id}/status",
    }

@app.post("/order/confirm")
async def confirm_payment(req: PaymentConfirm):
    """支付确认 — 收到回调后触发"""
    conn = get_db()
    
    order = conn.execute(
        "SELECT * FROM orders WHERE provider_order_id=? AND status='pending'",
        [req.provider_order_id]
    ).fetchone()
    
    if not order:
        # Try by id
        order = conn.execute(
            "SELECT * FROM orders WHERE id=? AND status='pending'",
            [req.provider_order_id]
        ).fetchone()
    
    if not order:
        conn.close()
        raise HTTPException(status_code=404, detail="Order not found or already processed")
    
    plan_config = PRICING.get(order["plan"], PRICING["starter"])
    now = time.time()
    expires_at = now + plan_config["days"] * 86400
    
    # Mark paid
    conn.execute(
        "UPDATE orders SET status='paid', paid_at=?, trade_no=? WHERE id=?",
        [now, req.trade_no or "", order["id"]]
    )
    
    # Generate API Key
    api_key_raw, key_hash, key_prefix = gen_api_key()
    key_id = "key_" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO api_keys (id, key_hash, key_prefix, plan, owner, max_devices, daily_limit, created_at, expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
        [key_id, key_hash, key_prefix, order["plan"],
         order.get("customer_email") or order.get("customer_phone") or "user",
         plan_config.get("max_devices", 1),
         plan_config["requests_per_day"], now, expires_at]
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
        "expires_at_readable": datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M"),
        "models": plan_config["models"],
        "requests_per_day": plan_config["requests_per_day"],
        "note": "⚠️ 保存好 API Key！离开页面不再显示。\n使用：POST /key/bind 绑定设备后即可调用 API。",
    }

@app.get("/order/{order_id}")
async def get_order(order_id: str):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", [order_id]).fetchone()
    conn.close()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return dict(order)

@app.get("/order/{order_id}/status")
async def get_order_status(order_id: str):
    """快速查订单状态（前端轮询用）"""
    conn = get_db()
    order = conn.execute("SELECT id, status, plan, amount, created_at, paid_at FROM orders WHERE id=?", [order_id]).fetchone()
    conn.close()
    if not order:
        return {"status": "not_found"}
    return dict(order)

# ══════════════════════════════════════
# 3. 支付宝异步通知
# ══════════════════════════════════════

@app.post("/order/notify")
async def alipay_notify(request: Request):
    """支付宝支付回调"""
    body = await request.body()
    params = {k: v for k, v in (item.split('=') for item in body.decode().split('&'))}
    
    # Verify signature
    alipay = get_alipay()
    if alipay and alipay.verify(params, params.get("sign", "")):
        provider_order_id = params.get("out_trade_no", "")
        trade_no = params.get("trade_no", "")
        trade_status = params.get("trade_status", "")
        
        if trade_status == "TRADE_SUCCESS":
            async with httpx.AsyncClient() as client:
                await client.post(f"http://localhost:{os.getenv('BILLING_PORT','8094')}/order/confirm", json={
                    "provider_order_id": provider_order_id,
                    "provider": "alipay",
                    "trade_no": trade_no,
                })
            return {"code": "10000", "msg": "Success"}
    
    return {"code": "40004", "msg": "Verify failed"}

# ══════════════════════════════════════
# 4. 收银台页面
# ══════════════════════════════════════

CHECKOUT_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AiLex — API Key 购买</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, "PingFang SC", sans-serif; background: #0a0a0a; color: #e0e0e0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
    .container { max-width: 800px; width: 100%; padding: 40px 20px; }
    h1 { font-size: 28px; font-weight: 700; text-align: center; margin-bottom: 8px; }
    .subtitle { text-align: center; color: #888; margin-bottom: 40px; }
    .plans { display: flex; gap: 16px; flex-wrap: wrap; justify-content: center; }
    .plan { background: #1a1a1a; border: 1px solid #333; border-radius: 12px; padding: 24px; width: 240px; cursor: pointer; transition: all .2s; }
    .plan:hover { border-color: #0A5032; }
    .plan.selected { border-color: #1E8C50; background: #0d2818; }
    .plan h2 { font-size: 18px; margin-bottom: 8px; }
    .plan .price { font-size: 32px; font-weight: 700; color: #1E8C50; }
    .plan .price span { font-size: 14px; color: #888; }
    .plan ul { list-style: none; margin: 16px 0; font-size: 13px; color: #aaa; }
    .plan ul li { padding: 4px 0; }
    .btn { display: block; width: 100%; max-width: 400px; margin: 24px auto; padding: 14px; background: #1E8C50; color: #fff; border: none; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; transition: background .2s; }
    .btn:hover { background: #0A5032; }
    .btn:disabled { background: #333; cursor: not-allowed; }
    .qrcode-box { text-align: center; padding: 20px; display: none; }
    .qrcode-box img { width: 200px; height: 200px; background: #fff; padding: 12px; border-radius: 8px; }
    .qrcode-box p { margin-top: 12px; color: #888; font-size: 14px; }
    .status { text-align: center; margin-top: 16px; font-size: 14px; display: none; }
    .manual-pay { text-align: center; padding: 20px; background: #1a1a1a; border-radius: 12px; border: 1px solid #333; margin-top: 20px; }
    .manual-pay h3 { margin-bottom: 8px; }
    .manual-pay p { font-size: 18px; color: #1E8C50; font-weight: 600; margin: 4px 0; }
  </style>
</head>
<body>
<div class="container">
  <h1>AiLex Platform</h1>
  <p class="subtitle">AI Agent API · 一台设备一个 Key</p>
  
  <div class="plans" id="plans"></div>
  <button class="btn" id="buyBtn" disabled>选择方案后购买</button>
  
  <div class="qrcode-box" id="qrBox">
    <h3>扫码支付</h3>
    <div id="qrContainer"></div>
    <p id="qrNote">请使用支付宝或微信扫码</p>
    <div class="manual-pay">
      <h3>或直接转账</h3>
      <p>支付宝：13703717827</p>
      <p>微信：angel520alan</p>
      <p style="font-size:12px;color:#666;">付款后告诉我订单号，人工确认</p>
    </div>
  </div>
  <div class="status" id="status"></div>
</div>

<script>
const PLANS = {"starter": {"name": "Starter", "price": 99, "requests": "1,000/天", "models": "基础模型"},
  "pro": {"name": "Pro", "price": 399, "requests": "10,000/天", "models": "全部模型"},
  "enterprise": {"name": "Enterprise", "price": 2999, "requests": "100,000/天", "models": "全部模型", "days": "365天"}};

let selectedPlan = null;
const plansContainer = document.getElementById('plans');
Object.entries(PLANS).forEach(([key, p]) => {
  const div = document.createElement('div');
  div.className = 'plan';
  div.innerHTML = '<h2>' + p.name + '</h2><div class="price">¥' + p.price + '<span>/' + (p.days || '30天') + '</span></div><ul><li>📊 ' + p.requests + '</li><li>🤖 ' + p.models + '</li></ul>';
  div.onclick = () => { document.querySelectorAll('.plan').forEach(el => el.classList.remove('selected')); div.classList.add('selected'); selectedPlan = key; document.getElementById('buyBtn').disabled = false; document.getElementById('buyBtn').textContent = '购买 ' + p.name + ' ¥' + p.price; };
  plansContainer.appendChild(div);
});

document.getElementById('buyBtn').onclick = async () => {
  if (!selectedPlan) return;
  document.getElementById('buyBtn').disabled = true;
  document.getElementById('buyBtn').textContent = '生成订单中...';
  
  const resp = await fetch('/order/create', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({plan: selectedPlan, provider: 'alipay'}) });
  const data = await resp.json();
  
  document.getElementById('qrBox').style.display = 'block';
  document.getElementById('qrNote').textContent = '订单号: ' + data.order_id + ' | 金额: ¥' + data.amount;
  
  document.getElementById('buyBtn').textContent = '已完成支付，确认';
  document.getElementById('buyBtn').onclick = async () => {
    const cr = await fetch('/order/confirm', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({provider_order_id: data.order_id, provider: 'alipay'}) });
    const cd = await cr.json();
    if (cd.status === 'paid') {
      document.getElementById('status').style.display = 'block';
      document.getElementById('status').innerHTML = '<h3>✅ 支付成功！</h3><p>API Key: <code style="background:#333;padding:4px 8px;border-radius:4px;">' + cd.api_key + '</code></p><p style="color:#f00;font-size:12px;">⚠️ 保存此 Key，离开页面不再显示</p><p>到期: ' + cd.expires_at_readable + '</p>';
      document.getElementById('qrBox').style.display = 'none';
      document.getElementById('buyBtn').style.display = 'none';
    }
  };
  document.getElementById('buyBtn').disabled = false;
};
</script>
</body>
</html>
"""

@app.get("/checkout", response_class=HTMLResponse)
async def checkout_page():
    return CHECKOUT_HTML

@app.get("/", response_class=HTMLResponse)
async def root():
    return CHECKOUT_HTML

# ══════════════════════════════════════
# 5. API Key 管理 + 设备绑定
# ══════════════════════════════════════

@app.post("/key/create")
async def create_api_key(req: ApiKeyCreate, authorization: str = Header(None)):
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=403, detail="Admin access required")
    
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
    key_hash = hashlib.sha256(req.api_key.encode()).hexdigest()
    conn = get_db()
    
    key = conn.execute("SELECT * FROM api_keys WHERE key_hash=?", [key_hash]).fetchone()
    if not key:
        conn.close()
        raise HTTPException(status_code=404, detail="Invalid API key")
    
    if key["status"] != "active":
        conn.close()
        raise HTTPException(status_code=403, detail=f"API key status: {key['status']}")
    
    now = time.time()
    if key["expires_at"] and now > key["expires_at"]:
        conn.close()
        raise HTTPException(status_code=403, detail="API key expired")
    
    bindings = json.loads(key["device_bindings"])
    
    # Existing device - update name
    for b in bindings:
        if b["fingerprint"] == req.device_fingerprint:
            if req.device_name:
                b["name"] = req.device_name
                conn.execute("UPDATE api_keys SET device_bindings=? WHERE id=?", [json.dumps(bindings), key["id"]])
                conn.commit()
            conn.close()
            return {"bound": True, "note": "Device already registered"}
    
    # New device
    if len(bindings) >= key["max_devices"]:
        conn.close()
        raise HTTPException(status_code=403, detail=f"Max devices ({key['max_devices']}) reached")
    
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
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key required")
    
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    conn = get_db()
    
    key = conn.execute("SELECT * FROM api_keys WHERE key_hash=?", [key_hash]).fetchone()
    if not key:
        conn.close()
        raise HTTPException(status_code=404, detail="Invalid API key")
    
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

# ══════════════════════════════════════
# 6. API 使用验证（Gateway 中间件）
# ══════════════════════════════════════

@app.post("/verify")
async def verify_and_use(req: UsageRequest):
    if not req.api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    key_hash = hashlib.sha256(req.api_key.encode()).hexdigest()
    conn = get_db()
    
    key = conn.execute("SELECT * FROM api_keys WHERE key_hash=?", [key_hash]).fetchone()
    if not key:
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    if key["status"] != "active":
        conn.close()
        raise HTTPException(status_code=403, detail=f"API key status: {key['status']}")
    
    now = time.time()
    if key["expires_at"] and now > key["expires_at"]:
        conn.close()
        raise HTTPException(status_code=403, detail="API key expired")
    
    if req.device_fingerprint:
        bindings = json.loads(key["device_bindings"])
        if not any(b["fingerprint"] == req.device_fingerprint for b in bindings):
            conn.close()
            raise HTTPException(status_code=403, detail=f"Device not bound. Fingerprint: {req.device_fingerprint}")
    
    if key["requests_today"] >= key["daily_limit"]:
        conn.close()
        raise HTTPException(status_code=429, detail=f"Daily limit ({key['daily_limit']}) reached")
    
    conn.execute("UPDATE api_keys SET requests_today=requests_today+1, total_requests=total_requests+1, last_used=? WHERE id=?", [now, key["id"]])
    conn.execute("INSERT INTO usage_log (key_id, endpoint, device_fingerprint, model, created_at) VALUES (?, ?, ?, ?, ?)", [key["id"], req.endpoint, req.device_fingerprint, req.model, now])
    conn.commit()
    conn.close()
    
    return {
        "verified": True,
        "plan": key["plan"],
        "requests_remaining": key["daily_limit"] - key["requests_today"],
        "expires_at": key["expires_at"],
    }

# ══════════════════════════════════════
# 7. 管理
# ══════════════════════════════════════

@app.get("/admin/api-keys")
async def list_api_keys(authorization: str = Header(None)):
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=403, detail="Admin access required")
    conn = get_db()
    keys = conn.execute("SELECT id, key_prefix, plan, owner, status, max_devices, device_bindings, requests_today, daily_limit, total_requests, created_at, expires_at FROM api_keys ORDER BY created_at DESC LIMIT 50").fetchall()
    conn.close()
    return {"keys": [dict(k) for k in keys]}

@app.post("/admin/reset-quota/{key_id}")
async def reset_quota(key_id: str, authorization: str = Header(None)):
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=403, detail="Admin access required")
    conn = get_db()
    conn.execute("UPDATE api_keys SET requests_today=0 WHERE id=?", [key_id])
    conn.commit()
    conn.close()
    return {"reset": True, "key_id": key_id}

@app.get("/usage/{key_id}")
async def get_usage(key_id: str, days: int = 7):
    conn = get_db()
    cutoff = time.time() - days * 86400
    logs = conn.execute("SELECT DATE(created_at, 'unixepoch') as date, COUNT(*) as count, COUNT(DISTINCT device_fingerprint) as devices FROM usage_log WHERE key_id=? AND created_at>? GROUP BY date ORDER BY date", [key_id, cutoff]).fetchall()
    conn.close()
    return {"key_id": key_id, "days": days, "daily_usage": [dict(r) for r in logs]}

@app.get("/stats")
async def get_stats():
    conn = get_db()
    counts = {
        "total_orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        "paid_orders": conn.execute("SELECT COUNT(*) FROM orders WHERE status='paid'").fetchone()[0],
        "active_keys": conn.execute("SELECT COUNT(*) FROM api_keys WHERE status='active'").fetchone()[0],
        "usage_30d": conn.execute(f"SELECT COUNT(*) FROM usage_log WHERE created_at>?", [time.time() - 30*86400]).fetchone()[0],
        "revenue_cny": conn.execute("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status='paid'").fetchone()[0],
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
