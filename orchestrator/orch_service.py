#!/usr/bin/env python3
"""
AiLex Orchestrator — 多Agent编排引擎 (Factor 10)
支持 DAG 定义、顺序/并行执行、条件分支、结果聚合

Factor 10 核心原则：小聚焦 Agent
  10 步以内，一个大 Agent 不如三个小 Agent
  每个 Agent 只做一件事，做精

本引擎设计：
  1. DAG 定义语言 (YAML) — 描述步骤间的依赖和条件
  2. DAG 执行引擎 — 解析 DAG + 按拓扑序执行 + 处理条件分支
  3. 每个节点指向一个小 Agent（3-10 步），职责单一
"""

import os
import json
import time
import uuid
import sqlite3
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import httpx
import yaml

# ── Config ──
DB_PATH = os.getenv("ORCH_DB_PATH", "/app/data/orch.db")
MMI_API_KEY = os.getenv("MMI_API_KEY", "")
MMI_BASE_URL = os.getenv("MMI_BASE_URL", "https://millionengine.com/v1")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8080")

# ── DB ──
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workflows (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            dag_yaml TEXT NOT NULL,
            created_at REAL,
            updated_at REAL,
            version INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS executions (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending',  -- pending | running | paused | completed | failed
            input_data TEXT,
            output_data TEXT,
            current_node TEXT,
            error TEXT,
            created_at REAL,
            completed_at REAL
        );
        CREATE TABLE IF NOT EXISTS execution_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            agent_name TEXT,
            status TEXT DEFAULT 'pending',  -- pending | running | completed | skipped | failed
            input_data TEXT,
            output_data TEXT,
            error TEXT,
            started_at REAL,
            completed_at REAL
        );
        CREATE TABLE IF NOT EXISTS agent_definitions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            system_prompt TEXT NOT NULL,
            max_steps INTEGER DEFAULT 5,
            tools TEXT DEFAULT '[]',  -- JSON array of tool names
            created_at REAL,
            updated_at REAL
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ── Models ──
class DAGNode(BaseModel):
    id: str
    agent: str
    input_template: Optional[str] = None  # template with {refs} to previous outputs
    condition: Optional[str] = None       # skip if condition not met
    retry_on_fail: int = 0
    depends_on: List[str] = []

class DAGDefinition(BaseModel):
    nodes: List[DAGNode]

class WorkflowCreate(BaseModel):
    name: str
    description: Optional[str] = None
    dag: DAGDefinition

class AgentDefinition(BaseModel):
    name: str
    description: Optional[str] = None
    system_prompt: str
    max_steps: int = 5
    tools: List[str] = []

class ExecutionStart(BaseModel):
    workflow_id: str
    input_data: Optional[dict] = None

# ── App ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"AiLex Orchestrator started — DB: {DB_PATH}")
    yield

app = FastAPI(title="AiLex Orchestrator", version="1.0.0", lifespan=lifespan)

async def call_agent(agent_name: str, prompt: str, system_prompt: str = "") -> dict:
    """调用一个小 Agent"""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    
    headers = {
        "Authorization": f"Bearer {MMI_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{MMI_BASE_URL}/chat/completions",
            json={"model": "gpt-4o", "messages": messages, "temperature": 0.3, "max_tokens": 2000},
            headers=headers,
        )
        if resp.status_code != 200:
            return {"error": resp.text[:200], "status": "failed"}
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {"content": content, "model": data.get("model", "gpt-4o"), "status": "completed"}

# ══════════════════════════════════════
# 1. Agent 定义管理
# ══════════════════════════════════════

@app.post("/agents")
async def create_agent(agent: AgentDefinition):
    """注册一个小 Agent"""
    agent_id = f"agent_{uuid.uuid4().hex[:8]}"
    now = time.time()
    conn = get_db()
    conn.execute(
        "INSERT INTO agent_definitions (id, name, description, system_prompt, max_steps, tools, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [agent_id, agent.name, agent.description, agent.system_prompt, agent.max_steps, json.dumps(agent.tools), now, now]
    )
    conn.commit()
    conn.close()
    return {"agent_id": agent_id, "name": agent.name}

@app.get("/agents")
async def list_agents():
    conn = get_db()
    rows = conn.execute("SELECT * FROM agent_definitions ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"agents": [dict(r) for r in rows]}

# ══════════════════════════════════════
# 2. 工作流定义
# ══════════════════════════════════════

@app.post("/workflows")
async def create_workflow(wf: WorkflowCreate):
    """创建 DAG 工作流"""
    wf_id = f"wf_{uuid.uuid4().hex[:8]}"
    now = time.time()
    dag_yaml = yaml.dump(wf.dag.model_dump(), allow_unicode=True)
    
    conn = get_db()
    conn.execute(
        "INSERT INTO workflows (id, name, description, dag_yaml, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        [wf_id, wf.name, wf.description, dag_yaml, now, now]
    )
    conn.commit()
    conn.close()
    
    return {
        "workflow_id": wf_id,
        "name": wf.name,
        "nodes": len(wf.dag.nodes),
        "dag_parsed": wf.dag.model_dump(),
    }

@app.get("/workflows")
async def list_workflows():
    conn = get_db()
    rows = conn.execute("SELECT id, name, description, created_at, version FROM workflows ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"workflows": [dict(r) for r in rows]}

@app.get("/workflows/{wf_id}")
async def get_workflow(wf_id: str):
    conn = get_db()
    wf = conn.execute("SELECT * FROM workflows WHERE id=?", [wf_id]).fetchone()
    conn.close()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    result = dict(wf)
    result["dag"] = yaml.safe_load(wf["dag_yaml"])
    return result

# ══════════════════════════════════════
# 3. DAG 执行引擎 (Factor 10 核心)
# ══════════════════════════════════════

def topological_sort(nodes: List[dict]) -> List[str]:
    """拓扑排序 — 确定执行顺序"""
    # Build adjacency
    in_degree = {}
    adj = {}
    for n in nodes:
        in_degree[n["id"]] = 0
        adj[n["id"]] = []
    
    for n in nodes:
        for dep in n.get("depends_on", []):
            if dep not in adj:
                continue
            adj[dep].append(n["id"])
            in_degree[n["id"]] = in_degree.get(n["id"], 0) + 1
    
    # Kahn's algorithm
    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    order = []
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for dep in adj.get(nid, []):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)
    
    return order

@app.post("/workflows/{wf_id}/execute")
async def execute_workflow(wf_id: str, background: BackgroundTasks, input_data: Optional[dict] = None):
    """执行工作流"""
    conn = get_db()
    wf = conn.execute("SELECT * FROM workflows WHERE id=?", [wf_id]).fetchone()
    if not wf:
        conn.close()
        raise HTTPException(status_code=404)
    
    execution_id = f"exec_{uuid.uuid4().hex[:12]}"
    now = time.time()
    
    dag = yaml.safe_load(wf["dag_yaml"])
    nodes = dag.get("nodes", [])
    exec_order = topological_sort(nodes)
    
    conn.execute(
        "INSERT INTO executions (id, workflow_id, status, input_data, current_node, created_at) VALUES (?, ?, 'running', ?, ?, ?)",
        [execution_id, wf_id, json.dumps(input_data or {}), exec_order[0] if exec_order else "", now]
    )
    
    # Create node records
    for node in nodes:
        conn.execute(
            "INSERT INTO execution_nodes (execution_id, node_id, agent_name, status) VALUES (?, ?, ?, 'pending')",
            [execution_id, node["id"], node.get("agent", "")]
        )
    
    conn.commit()
    conn.close()
    
    # Execute in background
    background.add_task(run_workflow, execution_id, wf_id, nodes, exec_order, input_data or {})
    
    return {
        "execution_id": execution_id,
        "workflow_id": wf_id,
        "status": "running",
        "execution_order": exec_order,
        "total_nodes": len(nodes),
    }

async def run_workflow(execution_id: str, wf_id: str, nodes: List[dict], order: List[str], input_data: dict):
    """后台执行工作流（DAG 引擎）"""
    outputs = {}  # node_id -> output
    
    for node_id in order:
        node = next(n for n in nodes if n["id"] == node_id)
        
        # Check condition
        if node.get("condition"):
            condition_template = node["condition"]
            # Simple eval of condition
            try:
                condition_context = {**input_data, **{k: outputs.get(k, {}) for k in node.get("depends_on", [])}}
                result = await call_agent("condition-checker", 
                    f"Evaluate condition: {condition_template}\nContext: {json.dumps(condition_context, ensure_ascii=False)}\nAnswer only 'true' or 'false'.")
                if "false" in result.get("content", "").lower():
                    conn = get_db()
                    conn.execute("UPDATE execution_nodes SET status='skipped' WHERE execution_id=? AND node_id=?", [execution_id, node_id])
                    conn.commit()
                    conn.close()
                    outputs[node_id] = {"skipped": True, "reason": condition_template}
                    continue
            except:
                pass
        
        # Build input
        dep_outputs = {dep: outputs.get(dep, {}) for dep in node.get("depends_on", [])}
        agent_input = node.get("input_template", "Process: " + node.get("agent", ""))
        # Template substitution
        context = {**input_data, **dep_outputs}
        for key, val in context.items():
            placeholder = f"{{{key}}}"
            if isinstance(val, dict):
                val = json.dumps(val, ensure_ascii=False)
            agent_input = agent_input.replace(placeholder, str(val)[:2000])
        
        # Update status
        conn = get_db()
        now = time.time()
        conn.execute(
            "UPDATE execution_nodes SET status='running', input_data=?, started_at=? WHERE execution_id=? AND node_id=?",
            [json.dumps({"prompt": agent_input}), now, execution_id, node_id]
        )
        conn.execute("UPDATE executions SET current_node=? WHERE id=?", [node_id, execution_id])
        conn.commit()
        conn.close()
        
        # Call agent with retry
        max_retries = node.get("retry_on_fail", 0) + 1
        agent_result = None
        for attempt in range(max_retries):
            agent_result = await call_agent(node.get("agent", "generic"), agent_input)
            if agent_result.get("status") == "completed":
                break
        
        # Record result
        conn = get_db()
        now = time.time()
        if agent_result and agent_result.get("status") == "completed":
            outputs[node_id] = agent_result.get("content", "")
            conn.execute(
                "UPDATE execution_nodes SET status='completed', output_data=?, completed_at=? WHERE execution_id=? AND node_id=?",
                [json.dumps(agent_result), now, execution_id, node_id]
            )
        else:
            outputs[node_id] = {"error": agent_result.get("error", "unknown") if agent_result else "no response"}
            conn.execute(
                "UPDATE execution_nodes SET status='failed', error=?, completed_at=? WHERE execution_id=? AND node_id=?",
                [outputs[node_id]["error"], now, execution_id, node_id]
            )
            conn.execute("UPDATE executions SET status='failed', error=?, completed_at=? WHERE id=?",
                [outputs[node_id]["error"], now, execution_id])
            conn.commit()
            conn.close()
            return
        
        conn.commit()
        conn.close()
    
    # All done
    conn = get_db()
    now = time.time()
    conn.execute(
        "UPDATE executions SET status='completed', output_data=?, completed_at=? WHERE id=?",
        [json.dumps(outputs), now, execution_id]
    )
    conn.commit()
    conn.close()

@app.get("/executions/{exec_id}")
async def get_execution(exec_id: str):
    """获取执行状态"""
    conn = get_db()
    exec_row = conn.execute("SELECT * FROM executions WHERE id=?", [exec_id]).fetchone()
    if not exec_row:
        conn.close()
        raise HTTPException(status_code=404)
    
    nodes = conn.execute(
        "SELECT * FROM execution_nodes WHERE execution_id=? ORDER BY id", [exec_id]
    ).fetchall()
    conn.close()
    
    return {
        "execution": dict(exec_row),
        "nodes": [dict(n) for n in nodes],
    }

@app.post("/workflows/{wf_id}/dag")
async def update_dag(wf_id: str, dag: DAGDefinition):
    """更新 DAG 定义"""
    conn = get_db()
    dag_yaml = yaml.dump(dag.model_dump(), allow_unicode=True)
    conn.execute(
        "UPDATE workflows SET dag_yaml=?, updated_at=?, version=version+1 WHERE id=?",
        [dag_yaml, time.time(), wf_id]
    )
    conn.commit()
    conn.close()
    return {"updated": True, "nodes": len(dag.nodes)}

@app.get("/stats")
async def get_stats():
    conn = get_db()
    counts = {
        "workflows": conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0],
        "agents": conn.execute("SELECT COUNT(*) FROM agent_definitions").fetchone()[0],
        "executions": conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0],
        "completed": conn.execute("SELECT COUNT(*) FROM executions WHERE status='completed'").fetchone()[0],
        "failed": conn.execute("SELECT COUNT(*) FROM executions WHERE status='failed'").fetchone()[0],
    }
    conn.close()
    return counts

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("ORCH_PORT", "8095"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
