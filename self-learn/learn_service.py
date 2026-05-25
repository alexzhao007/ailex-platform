#!/usr/bin/env python3
"""
AiLex Self-Learning — 自我学习系统
自动从对话中提取经验、构建知识库、优化 Prompt

核心机制：
1. 经验提取 — 从成功/失败案例中提取可复用模式
2. 技能进化 — 自动优化技能模板和 prompt
3. 知识建模 — 从对话中构建结构化知识
4. 遗忘机制 — 按重要性淘汰过时知识
"""

import os
import json
import time
import sqlite3
import hashlib
import re
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import httpx

# ── Config ──
DB_PATH = os.getenv("LEARN_DB_PATH", "/app/data/learn.db")
MMI_API_KEY = os.getenv("MMI_API_KEY", "")
MMI_BASE_URL = os.getenv("MMI_BASE_URL", "https://millionengine.com/v1")
LEARN_MODEL = os.getenv("LEARN_MODEL", "gpt-4o")
IMPORTANCE_THRESHOLD = float(os.getenv("IMPORTANCE_THRESHOLD", "0.3"))  # 低于此值考虑遗忘

# ── Stats ──
stats = {
    "experiences_extracted": 0,
    "prompts_optimized": 0,
    "knowledge_nodes": 0,
    "forgotten_items": 0,
    "total_learn_cycles": 0,
    "start_time": time.time(),
}

# ── DB Setup ──
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS experiences (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,  -- success | failure | insight
            summary TEXT NOT NULL,
            context TEXT,
            lesson TEXT NOT NULL,
            tags TEXT DEFAULT '[]',
            importance REAL DEFAULT 0.5,
            source TEXT,  -- session_id or manual
            created_at REAL,
            last_used REAL,
            use_count INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_exp_type ON experiences(type, importance);
        CREATE INDEX IF NOT EXISTS idx_exp_tags ON experiences(tags);
        
        CREATE TABLE IF NOT EXISTS prompt_versions (
            id TEXT PRIMARY KEY,
            skill_name TEXT NOT NULL,
            version INTEGER DEFAULT 1,
            prompt_text TEXT NOT NULL,
            performance_score REAL DEFAULT 0.5,
            test_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            created_at REAL,
            parent_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_prompt_skill ON prompt_versions(skill_name, version);
        
        CREATE TABLE IF NOT EXISTS knowledge_graph (
            id TEXT PRIMARY KEY,
            concept TEXT NOT NULL,
            relation TEXT,  -- is_a | part_of | related_to | leads_to
            target_concept TEXT,
            evidence TEXT,
            confidence REAL DEFAULT 0.5,
            created_at REAL,
            updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_kg_concept ON knowledge_graph(concept);
        
        CREATE TABLE IF NOT EXISTS learning_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ── Models ──
class ExperienceInput(BaseModel):
    type: str  # success | failure | insight
    summary: str
    context: Optional[str] = None
    lesson: str
    tags: Optional[List[str]] = None
    source: Optional[str] = None

class AnalyzeRequest(BaseModel):
    text: str
    context_type: str = "conversation"  # conversation | code | decision

class PromptTest(BaseModel):
    skill_name: str
    prompt_text: str
    results: List[dict]  # [{"success": true/false, "notes": ""}]

# ══════════════════════════════════════
# App
# ══════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"AiLex Self-Learning started — DB: {DB_PATH}")
    yield

app = FastAPI(title="AiLex Self-Learning", version="2.0.0", lifespan=lifespan)

async def call_llm(prompt: str, model: str = LEARN_MODEL) -> str:
    """调用万量引擎 LLM"""
    headers = {"Authorization": f"Bearer {MMI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{MMI_BASE_URL}/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3},
            headers=headers,
        )
        if resp.status_code != 200:
            return ""
        return resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")

# ── 1. 经验提取 ──
@app.post("/learn/extract")
async def extract_experience(req: AnalyzeRequest, background: BackgroundTasks):
    """从交互中自动提取经验"""
    prompt = f"""
    Analyze the following interaction and extract learning experiences.
    For each distinct lesson, output a JSON object with:
    - type: "success" | "failure" | "insight"
    - summary: one-line summary
    - lesson: specific actionable lesson
    - tags: array of relevant tags
    - importance: 0.0 to 1.0
    
    Interaction:
    ---
    {req.text[:4000]}
    ---
    
    Output JSON array:
    """
    
    result = await call_llm(prompt)
    
    try:
        # Try to parse JSON from response
        json_match = re.search(r'\[.*\]', result, re.DOTALL)
        if json_match:
            experiences = json.loads(json_match.group())
        else:
            experiences = [{"type": "insight", "summary": result[:200], "lesson": result[:500], "tags": ["auto-extracted"], "importance": 0.5}]
    except:
        experiences = [{"type": "insight", "summary": result[:200], "lesson": result[:500], "tags": ["auto-extracted"], "importance": 0.5}]
    
    # Store
    conn = get_db()
    saved = 0
    for exp in experiences:
        exp_id = hashlib.md5(f"{exp.get('summary','')}{time.time()}".encode()).hexdigest()[:16]
        conn.execute(
            "INSERT OR IGNORE INTO experiences (id, type, summary, lesson, tags, importance, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [exp_id, exp.get("type", "insight"), exp.get("summary", ""), exp.get("lesson", ""),
             json.dumps(exp.get("tags", [])), exp.get("importance", 0.5), "auto-extract", time.time()]
        )
        saved += 1
    
    conn.commit()
    conn.close()
    stats["experiences_extracted"] += saved
    stats["total_learn_cycles"] += 1
    
    return {"extracted": saved, "experiences": experiences[:5]}

@app.post("/learn/add")
async def add_experience(exp: ExperienceInput):
    """手动添加经验"""
    exp_id = hashlib.md5(f"{exp.summary}{time.time()}".encode()).hexdigest()[:16]
    conn = get_db()
    conn.execute(
        "INSERT INTO experiences (id, type, summary, context, lesson, tags, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [exp_id, exp.type, exp.summary, exp.context, exp.lesson,
         json.dumps(exp.tags or []), exp.source, time.time()]
    )
    conn.commit()
    conn.close()
    stats["experiences_extracted"] += 1
    return {"id": exp_id, "status": "saved"}

@app.get("/learn/experiences")
async def list_experiences(
    type: Optional[str] = None,
    tag: Optional[str] = None,
    min_importance: float = 0.3,
    limit: int = 20
):
    """列出经验"""
    conn = get_db()
    query = "SELECT * FROM experiences WHERE archived=0 AND importance>=?"
    params = [min_importance]
    
    if type:
        query += " AND type=?"
        params.append(type)
    if tag:
        query += " AND tags LIKE ?"
        params.append(f'%{tag}%')
    
    query += " ORDER BY importance DESC, created_at DESC LIMIT ?"
    params.append(limit)
    
    rows = conn.execute(query, params).fetchall()
    conn.close()
    
    return {"experiences": [dict(r) for r in rows], "total": len(rows)}

# ── 2. Prompt 自动优化 ──
@app.post("/learn/optimize-prompt")
async def optimize_prompt(skill: str, current_prompt: str, background: BackgroundTasks):
    """自动优化 prompt"""
    # Get past performance
    conn = get_db()
    past = conn.execute(
        "SELECT prompt_text, success_count, test_count FROM prompt_versions WHERE skill_name=? ORDER BY version DESC LIMIT 5",
        [skill]
    ).fetchall()
    conn.close()
    
    perf_context = ""
    for p in past:
        rate = f"{p['success_count']}/{p['test_count']}" if p['test_count'] > 0 else "untested"
        perf_context += f"- Version: success={rate}\n"
    
    prompt = f"""
    As an AI prompt engineer, optimize the following prompt for better results.
    
    Skill: {skill}
    
    Current prompt:
    ---
    {current_prompt[:3000]}
    ---
    
    Past performance:
    {perf_context}
    
    Provide:
    1. Optimized prompt (max 500 words)
    2. What was improved (3-5 bullet points)
    
    Output as JSON:
    {{"optimized_prompt": "...", "improvements": ["...", "..."]}}
    """
    
    result = await call_llm(prompt)
    
    try:
        json_match = re.search(r'\{.*\}', result, re.DOTALL)
        optimized = json.loads(json_match.group()) if json_match else {"optimized_prompt": result, "improvements": ["AI-optimized"]}
    except:
        optimized = {"optimized_prompt": result, "improvements": ["AI-optimized"]}
    
    # Save new version
    conn = get_db()
    last_version = conn.execute(
        "SELECT MAX(version) FROM prompt_versions WHERE skill_name=?", [skill]
    ).fetchone()[0] or 0
    
    version_id = hashlib.md5(f"{skill}{time.time()}".encode()).hexdigest()[:16]
    conn.execute(
        "INSERT INTO prompt_versions (id, skill_name, version, prompt_text, created_at) VALUES (?, ?, ?, ?, ?)",
        [version_id, skill, last_version + 1, optimized.get("optimized_prompt", ""), time.time()]
    )
    conn.commit()
    conn.close()
    
    stats["prompts_optimized"] += 1
    return {"version": last_version + 1, "optimized": optimized}

@app.post("/learn/test-prompt")
async def test_prompt(test: PromptTest):
    """记录 prompt 测试结果"""
    conn = get_db()
    successes = sum(1 for r in test.results if r.get("success"))
    total = len(test.results)
    score = successes / total if total > 0 else 0
    
    conn.execute(
        "UPDATE prompt_versions SET performance_score=?, test_count=test_count+?, success_count=success_count+? WHERE skill_name=? AND prompt_text=?",
        [score, total, successes, test.skill_name, test.prompt_text]
    )
    conn.commit()
    conn.close()
    return {"success_rate": score, "tested": total, "passed": successes}

# ── 3. 知识建模 ──
@app.post("/learn/knowledge/add")
async def add_knowledge(concept: str, relation: str, target: str, evidence: str = ""):
    """添加知识节点"""
    kid = hashlib.md5(f"{concept}{relation}{target}".encode()).hexdigest()[:16]
    now = time.time()
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO knowledge_graph (id, concept, relation, target_concept, evidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [kid, concept, relation, target, evidence, now, now]
    )
    conn.commit()
    conn.close()
    stats["knowledge_nodes"] += 1
    return {"id": kid}

@app.get("/learn/knowledge/search")
async def search_knowledge(query: str, limit: int = 10):
    """搜索知识"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM knowledge_graph WHERE concept LIKE ? OR target_concept LIKE ? ORDER BY confidence DESC LIMIT ?",
        [f'%{query}%', f'%{query}%', limit]
    ).fetchall()
    conn.close()
    return {"results": [dict(r) for r in rows]}

@app.get("/learn/knowledge/graph")
async def get_knowledge_graph(concept: str, depth: int = 2):
    """获取知识图谱（关联查询）"""
    conn = get_db()
    nodes = set()
    edges = []
    
    current = {concept}
    for _ in range(depth):
        if not current:
            break
        placeholders = ",".join(["?" for _ in current])
        rows = conn.execute(
            f"SELECT * FROM knowledge_graph WHERE concept IN ({placeholders}) OR target_concept IN ({placeholders})",
            list(current) + list(current)
        ).fetchall()
        for r in rows:
            nodes.add(r["concept"])
            nodes.add(r["target_concept"])
            edges.append({"source": r["concept"], "target": r["target_concept"], "relation": r["relation"]})
        current = nodes - {concept}
    
    conn.close()
    return {"nodes": list(nodes), "edges": edges}

# ── 4. 遗忘机制 ──
@app.post("/learn/forget")
async def forget(min_importance: float = IMPORTANCE_THRESHOLD):
    """自动遗忘低价值知识"""
    conn = get_db()
    
    # Archive old, low-importance, rarely-used experiences
    cutoff = time.time() - 30 * 86400  # 30 days
    forgotten = conn.execute(
        "UPDATE experiences SET archived=1 WHERE importance<? AND last_used IS NOT NULL AND last_used<? AND use_count<3",
        [min_importance, cutoff]
    ).rowcount
    
    conn.commit()
    conn.close()
    stats["forgotten_items"] += forgotten
    stats["total_learn_cycles"] += 1
    
    return {"forgotten": forgotten, "remaining_threshold": min_importance}

# ── Stats ──
@app.get("/stats")
async def get_stats():
    uptime = int(time.time() - stats["start_time"])
    conn = get_db()
    counts = {
        "experiences": conn.execute("SELECT COUNT(*) FROM experiences WHERE archived=0").fetchone()[0],
        "prompt_versions": conn.execute("SELECT COUNT(*) FROM prompt_versions").fetchone()[0],
        "knowledge_nodes": conn.execute("SELECT COUNT(*) FROM knowledge_graph").fetchone()[0],
    }
    conn.close()
    
    return {
        "uptime_hours": round(uptime / 3600, 1),
        **counts,
        "extracted": stats["experiences_extracted"],
        "optimized": stats["prompts_optimized"],
        "forgotten": stats["forgotten_items"],
        "learn_cycles": stats["total_learn_cycles"],
    }

@app.get("/health")
async def health():
    return {"status": "ok", "uptime": int(time.time() - stats["start_time"])}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("LEARN_PORT", "8092"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
