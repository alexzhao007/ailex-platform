#!/usr/bin/env python3
"""
AiLex Self-Security — 自我安全加固系统
自动化安全审计、入侵检测、合规检查、权限管理

核心机制：
1. 资产审计 — 自动扫描开放端口/服务/文件权限
2. 入侵检测 — 异常访问模式识别
3. 合规检查 — 12-Factor 安全因子自动验证
4. 自动修复 — 对常见安全问题执行自动修复
"""

import os
import json
import time
import sqlite3
import hashlib
import subprocess
import socket
import re
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import httpx

# ── Config ──
DB_PATH = os.getenv("SECURE_DB_PATH", "/app/data/secure.db")
MMI_API_KEY = os.getenv("MMI_API_KEY", "")
AUTO_FIX = os.getenv("AUTO_FIX", "false").lower() == "true"
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "3600"))  # 1 hour

# ── Stats ──
stats = {
    "audits_run": 0,
    "alerts_triggered": 0,
    "auto_fixes": 0,
    "vulnerabilities_found": 0,
    "vulnerabilities_fixed": 0,
    "start_time": time.time(),
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
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_type TEXT NOT NULL,
            status TEXT NOT NULL,  -- pass | warn | fail
            detail TEXT,
            severity TEXT DEFAULT 'info',
            recommendation TEXT,
            auto_fixed INTEGER DEFAULT 0,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL,
            severity TEXT NOT NULL,  -- low | medium | high | critical
            message TEXT NOT NULL,
            source_ip TEXT,
            action_taken TEXT,
            resolved INTEGER DEFAULT 0,
            created_at REAL,
            resolved_at REAL
        );
        CREATE TABLE IF NOT EXISTS security_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS fix_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fix_type TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'pending',  -- pending | success | failed
            created_at REAL,
            completed_at REAL
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ── Models ──
class Alert(BaseModel):
    alert_type: str
    severity: str = "medium"
    message: str
    source_ip: Optional[str] = None
    action_taken: Optional[str] = None

class FixAction(BaseModel):
    fix_type: str
    description: str
    command: Optional[str] = None

# ── App ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"AiLex Self-Security started — DB: {DB_PATH}")
    print(f"Auto-fix: {AUTO_FIX}, Scan interval: {SCAN_INTERVAL}s")
    yield

app = FastAPI(title="AiLex Self-Security", version="2.0.0", lifespan=lifespan)

# ══════════════════════════════════════
# 1. 资产安全审计
# ══════════════════════════════════════

def run_cmd(cmd: str, timeout: int = 10) -> str:
    """Run shell command safely"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except:
        return ""

@app.post("/audit/ports")
async def audit_open_ports():
    """扫描开放端口"""
    result = run_cmd("ss -tlnp 2>/dev/null | tail -n +2 || netstat -tlnp 2>/dev/null | tail -n +2")
    log_entry("ports", "info", f"Open ports:\n{result[:2000]}")
    
    # Parse and alert on risky ports
    risky_patterns = {"0.0.0.0:22": "SSH exposed to all interfaces", "0.0.0.0:3306": "MySQL exposed", "0.0.0.0:6379": "Redis exposed"}
    findings = []
    for port, desc in risky_patterns.items():
        if port in result:
            findings.append({"port": port, "risk": desc})
            stats["vulnerabilities_found"] += 1
            log_entry("ports", "warn", desc)
    
    return {"ports_scanned": len(result.split('\n')), "findings": findings, "raw": result[:500]}

@app.post("/audit/files")
async def audit_file_permissions():
    """扫描文件权限风险"""
    targets = [
        ("/root/.ssh", "700", "SSH keys"),
        ("/root/.env*", "600", "Environment files"),
        ("/etc/shadow", "600", "Password file"),
        ("/root/.hermes/config.yaml", "600", "Hermes config"),
    ]
    
    findings = []
    for path, expected, desc in targets:
        actual = run_cmd(f"stat -c '%a' {path} 2>/dev/null || echo 'not_found'")
        if actual != expected and actual != 'not_found':
            findings.append({"path": path, "expected": expected, "actual": actual, "desc": desc})
            log_entry("files", "warn", f"{desc}: {path} has permissions {actual}, expected {expected}")
    
    if AUTO_FIX:
        for f in findings:
            run_cmd(f"chmod {f['expected']} {f['path']}")
            stats["auto_fixes"] += 1
            f["auto_fixed"] = True
    
    return {"scanned": len(targets), "findings": findings}

@app.post("/audit/env")
async def audit_env_variables():
    """检查环境变量安全"""
    risky_vars = []
    for key, val in sorted(os.environ.items()):
        val_lower = val.lower()
        if any(k in key.lower() for k in ["key", "secret", "password", "token", "auth"]):
            if len(val) > 4:
                masked = val[:4] + "****"
                risky_vars.append({"key": key, "value_masked": masked, "length": len(val)})
    
    log_entry("env", "info", f"Found {len(risky_vars)} sensitive env vars")
    return {"total_vars": len(os.environ), "sensitive_vars": risky_vars}

@app.get("/audit/docker")
async def audit_docker():
    """Docker 安全检查"""
    checks = []
    
    # Check if running as root in container
    uid = run_cmd("id -u")
    if uid == "0":
        checks.append({"check": "Running as root", "status": "warn", "recommendation": "Use non-root user"})
    
    # Check Docker socket
    docker_sock = os.path.exists("/var/run/docker.sock")
    if docker_sock:
        checks.append({"check": "Docker socket mounted", "status": "warn", "recommendation": "Mount as read-only if possible"})
    
    return {"checks": checks}

@app.post("/audit/network")
async def audit_network():
    """网络连接审核"""
    connections = run_cmd("ss -tn 2>/dev/null | tail -n +2 || netstat -tn 2>/dev/null | tail -n +2")
    
    # Detect unusual outbound
    unusual_ports = {}
    for line in connections.split('\n'):
        if ':' in line:
            parts = line.split()
            if len(parts) >= 3:
                port = parts[2].split(':')[-1] if ':' in parts[2] else ''
                if port and port not in ['80', '443', '22', '8080', '3000', '8090', '8091', '8092', '8093']:
                    unusual_ports[port] = unusual_ports.get(port, 0) + 1
    
    return {"connections": len(connections.split('\n')), "unusual_ports": unusual_ports}

# ══════════════════════════════════════
# 2. 入侵检测
# ══════════════════════════════════════

@app.post("/security/alert")
async def add_alert(alert: Alert):
    """添加安全告警"""
    conn = get_db()
    conn.execute(
        "INSERT INTO alerts (alert_type, severity, message, source_ip, action_taken, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        [alert.alert_type, alert.severity, alert.message, alert.source_ip, alert.action_taken, time.time()]
    )
    conn.commit()
    conn.close()
    stats["alerts_triggered"] += 1
    return {"status": "alert recorded", "severity": alert.severity}

@app.post("/security/scan-auth")
async def scan_auth_logs():
    """扫描认证日志识别异常"""
    auth_log = run_cmd("tail -100 /var/log/auth.log 2>/dev/null || tail -100 /var/log/secure 2>/dev/null || echo 'no auth log'")
    
    failed_logins = len(re.findall(r'(Failed password|authentication failure|Invalid user)', auth_log, re.IGNORECASE))
    if failed_logins > 10:
        log_entry("auth", "warn", f"High failed login count: {failed_logins}")
        stats["alerts_triggered"] += 1
    
    return {"failed_logins_last_100": failed_logins, "raw_snippet": auth_log[:500]}

@app.get("/security/alerts")
async def list_alerts(severity: Optional[str] = None, resolved: bool = False, limit: int = 20):
    """列出告警"""
    conn = get_db()
    query = "SELECT * FROM alerts WHERE resolved=?"
    params = [1 if resolved else 0]
    
    if severity:
        query += " AND severity=?"
        params.append(severity)
    
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return {"alerts": [dict(r) for r in rows]}

@app.post("/security/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: int, action: str = "manual"):
    """解决告警"""
    conn = get_db()
    conn.execute(
        "UPDATE alerts SET resolved=1, resolved_at=? WHERE id=?",
        [time.time(), alert_id]
    )
    conn.commit()
    conn.close()
    return {"resolved": True, "action": action}

# ══════════════════════════════════════
# 3. 自动修复
# ══════════════════════════════════════

@app.post("/fix")
async def auto_fix(fix: FixAction):
    """执行自动修复"""
    if not AUTO_FIX:
        return {"message": "Auto-fix is disabled. Set AUTO_FIX=true to enable.", "dry_run": True}
    
    log_entry("fix", "info", f"Executing fix: {fix.fix_type} - {fix.description}")
    
    if fix.command:
        result = run_cmd(fix.command, timeout=30)
        success = "error" not in result.lower()
    else:
        result = "No command provided"
        success = False
    
    conn = get_db()
    conn.execute(
        "INSERT INTO fix_history (fix_type, description, status, created_at, completed_at) VALUES (?, ?, ?, ?, ?)",
        [fix.fix_type, fix.description, "success" if success else "failed", time.time(), time.time()]
    )
    conn.commit()
    conn.close()
    
    if success:
        stats["auto_fixes"] += 1
        stats["vulnerabilities_fixed"] += 1
    
    return {"fix_type": fix.fix_type, "success": success, "output": result[:500]}

@app.get("/fix/history")
async def fix_history(limit: int = 20):
    """修复历史"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM fix_history ORDER BY created_at DESC LIMIT ?", [limit]
    ).fetchall()
    conn.close()
    return {"fixes": [dict(r) for r in rows]}

# ══════════════════════════════════════
# 4. 全面审计
# ══════════════════════════════════════

@app.post("/audit/full")
async def full_audit(background: BackgroundTasks):
    """全量安全审计"""
    stats["audits_run"] += 1
    
    results = {
        "ports": await audit_open_ports(),
        "files": await audit_file_permissions(),
        "env": await audit_env_variables(),
        "docker": await audit_docker(),
        "network": await audit_network(),
        "auth": await scan_auth_logs(),
    }
    
    # Calculate security score
    total_checks = 0
    warnings = 0
    for k, v in results.items():
        if isinstance(v, dict) and "findings" in v:
            total_checks += v.get("scanned", 0) or len(v.get("findings", []))
            warnings += len(v.get("findings", []))
    
    security_score = max(0, 100 - (warnings * 10))
    
    # Summary
    summary = {
        "general": max(0, total_checks - warnings) if total_checks else 0,
        "warnings": warnings,
    }
    
    if AUTO_FIX:
        log_entry("audit", "info", f"Full audit complete: score={security_score}")
    
    return {
        "security_score": security_score,
        "summary": summary,
        "details": results,
        "timestamp": time.time(),
    }

@app.get("/audit/score")
async def get_security_score():
    """获取安全评分趋势"""
    conn = get_db()
    last_audit = conn.execute(
        "SELECT * FROM audit_log WHERE check_type='audit' ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    
    # Count unresolved critical alerts
    critical_open = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE resolved=0 AND severity IN ('high','critical')"
    ).fetchone()[0]
    
    conn.close()
    
    return {
        "critical_alerts_open": critical_open,
        "recent_audits": [dict(r) for r in last_audit],
        "status": "critical" if critical_open > 0 else "healthy",
    }

# ══════════════════════════════════════
# Helpers & Stats
# ══════════════════════════════════════

def log_entry(check_type: str, status: str, detail: str):
    """记录审计日志"""
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (check_type, status, detail, severity, created_at) VALUES (?, ?, ?, ?, ?)",
        [check_type, status, detail[:1000], "info" if status == "info" else "warn" if status == "warn" else "high", time.time()]
    )
    conn.commit()
    conn.close()

@app.get("/stats")
async def get_stats():
    uptime = int(time.time() - stats["start_time"])
    conn = get_db()
    counts = {
        "audit_logs": conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
        "open_alerts": conn.execute("SELECT COUNT(*) FROM alerts WHERE resolved=0").fetchone()[0],
        "fixes": conn.execute("SELECT COUNT(*) FROM fix_history").fetchone()[0],
    }
    conn.close()
    
    return {
        "uptime_hours": round(uptime / 3600, 1),
        **counts,
        "audits_run": stats["audits_run"],
        "alerts": stats["alerts_triggered"],
        "auto_fixes": stats["auto_fixes"],
        "vulnerabilities_found": stats["vulnerabilities_found"],
        "vulnerabilities_fixed": stats["vulnerabilities_fixed"],
    }

@app.get("/health")
async def health():
    return {"status": "ok", "uptime": int(time.time() - stats["start_time"])}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("SECURE_PORT", "8093"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
