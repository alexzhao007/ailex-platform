#!/usr/bin/env python3
"""
AiLex — 人工确认收款工具
用法：
  python3 confirm_order.py <order_id>
  
收到用户转账后，运行此命令确认 → 系统自动发放 API Key

需设置环境变量：
  BILLING_URL=http://localhost:8094  (或线上地址)
  ADMIN_TOKEN=<你的管理Token>
"""

import sys
import os
import requests
import json

BILLING_URL = os.getenv("BILLING_URL", "http://localhost:8094")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

def main():
    if len(sys.argv) < 2:
        print("❌ 用法: python3 confirm_order.py <订单号>")
        print("   例如: python3 confirm_order.py ord_7470b3f4d4c9bf64ffd83183")
        sys.exit(1)
    
    order_id = sys.argv[1]
    
    # Confirm payment
    resp = requests.post(
        f"{BILLING_URL}/order/confirm",
        json={
            "provider_order_id": order_id,
            "provider": "manual",
            "trade_no": f"manual_{order_id}",
        }
    )
    
    if resp.status_code == 200:
        data = resp.json()
        if data.get("status") == "paid":
            print(f"""
========================================
✅ 确认成功！API Key 已发放
========================================
订单号:   {order_id}
方案:     {data.get('plan')}
金额:     ¥{data.get('amount', '?')}
API Key:  {data.get('api_key')}
到期:     {data.get('expires_at_readable')}
========================================
请将 API Key 发给用户！
⚠️ 告诉用户需 POST /key/bind 绑定设备
========================================
""")
        else:
            print(f"⚠️ 状态异常: {data}")
    else:
        print(f"❌ 确认失败: {resp.status_code} {resp.text[:200]}")

if __name__ == "__main__":
    main()
