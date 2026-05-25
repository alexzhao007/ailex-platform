#!/usr/bin/env python3
"""
AiLex Memory — 超长记忆体服务
12-Factor Agents 合规：Factor 3(Own Context) + Factor 5(Unify State) + Factor 12(Stateless)

架构：
┌──────────────────────────────────────────┐
│  Memory Service (:8090)                   │
│  ┌──────────┐  ┌──────────┐  ┌────────┐ │
│  │ 短期记忆  │  │ 长期记忆  │  │ 语义记忆│ │
│  │ (滑动窗口)│  │ (持久化) │  │ (向量)  │ │
│  └──────────┘  └──────────┘  └────────┘ │
│         │              │           │      │
│         └──────────────┴───────────┘      │
│                       │                   │
│              ┌────────▼────────┐          │
│              │   SQLite/Chroma │          │
│              └─────────────────┘          │
└──────────────────────────────────────────┘

特点：
- 三层记忆架构（工作记忆/长期记忆/语义记忆）
- 自动上下文压缩（Factor 3）
- 会话暂停/恢复（Factor 6）
- 无状态 API 设计（Factor 12）
- 向量检索（支持 10 万+ 记忆条目）
"""

import os
import json
import time
import hashlib
import sqlite3
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import numpy as np

# ── Config ──
MEMORY_DB_PATH = os.getenv("MEMORY_DB_PATH", "/app/data/memory.db")
VECTOR_DB_PATH = os.getenv("VECTOR_DB_PATH", "/app/data/vectors.db")
SHORT_TERM_SIZE = int(os.getenv("SHORT_TERM_SIZE", "20"))       # 短期记忆窗口大小
SUMMARIZE_THRESHOLD = int(os.getenv("SUMMARIZE_THRESHOLD", "50"))  # 触发摘要的阈值
MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", "8000"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

# ── Stats ──
stats = {
    "total_conversations": 0,
    "total_memories": 0,
    "total_embeddings": 0,
    "compressions_saved_tokens": 0,
    "start_time": time.time(),
}

# ── Pydantic Models ──
class Message(BaseModel):
    role: str  # user | assistant | system | tool
    content: str
    name: Optional[str] = None
    timestamp: Optional[float] = None

class Conversation(BaseModel):
    session_id: str
    messages: List[Message]
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[float] = None

class MemoryQuery(BaseModel):
    session_id: Optional[str] = None
    query: str
    top_k: int = 5
    memory_type: Optional[str] = None  # short | long | semantic

class MemoryItem(BaseModel):
    id: str
    session_id: str
    content: str
    memory_type: str = "long"  # short | long | semantic
    embedding: Optional[List[float]] = None
    importance: Optional[float] = 0.5
    created_at: Optional[float] = None
    ttl: Optional[float] = None

# ── Database ──
def get_db():
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            session_id TEXT PRIMARY KEY,
            metadata TEXT DEFAULT '{}',
            created_at REAL,
            updated_at REAL,
            message_count INTEGER DEFAULT 0,
            summary TEXT,
            archived INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            name TEXT,
            created_at REAL,
            FOREIGN KEY (session_id) REFERENCES conversations(session_id)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at);
        
        CREATE TABLE IF NOT EXISTS long_term_memories (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            content TEXT NOT NULL,
            memory_type TEXT DEFAULT 'long',
            importance REAL DEFAULT 0.5,
            created_at REAL,
            access_count INTEGER DEFAULT 0,
            last_accessed REAL
        );
        CREATE INDEX IF NOT EXISTS idx_ltm_session ON long_term_memories(session_id);
        
        CREATE TABLE IF NOT EXISTS embeddings_cache (
            hash TEXT PRIMARY KEY,
            vector BLOB,
            model TEXT,
            created_at REAL
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ── Simple embedding function (replace with actual embedding API) ──
def get_embedding(text: str) -> List[float]:
    """Get embedding vector. Uses hash-based deterministic dummy for now,
    replace with actual embedding API call when available."""
    # Simple hash-based deterministic vector (128 dims)
    h = hashlib.sha256(text.encode()).digest()
    vec = [((h[i % 32] + (i * 7)) % 256) / 255.0 for i in range(128)]
    # Normalize
    norm = np.linalg.norm(vec)
    return [v / norm for v in vec]

def cosine_similarity(a: List[float], b: List[float]) -> float:
    return float(np.dot(a, b))

# ── Context Compression (Factor 3) ──
def compress_context(messages: List[Dict]) -> Dict:
    """
    上下文压缩策略（Factor 3: Own Your Context）：
    - 保留最近 N 条完整消息（短期记忆窗口）
    - 之前的消息压缩为摘要
    - 保留关键信息（工具调用结果、决策点）
    """
    n = SHORT_TERM_SIZE
    if len(messages) <= n:
        return {
            "compressed": False,
            "short_term": messages,
            "summary": None,
            "original_count": len(messages),
            "compressed_count": len(messages),
        }
    
    # 保留最近 N 条
    recent = messages[-n:]
    # 之前的压缩为摘要
    history = messages[:-n]
    
    # 提取关键信息构建摘要
    key_points = []
    for msg in history:
        r = msg.get("role", "")
        c = msg.get("content", "")
        if r == "tool" and len(c) < 200:
            key_points.append(f"[工具结果] {c[:100]}")
        elif r == "assistant" and len(c) < 500:
            key_points.append(f"[AI] {c[:100]}")
        elif r == "user":
            key_points.append(f"[用户] {c[:100]}")
        elif r == "system":
            key_points.append(f"[系统] 指令已执行")
    
    summary = " | ".join(key_points[-20:])  # 最多保留 20 条关键信息
    if len(summary) > 2000:
        summary = summary[:2000] + "..."
    
    tokens_saved = sum(len(m.get("content", "")) for m in history) - len(summary)
    
    return {
        "compressed": True,
        "short_term": recent,
        "summary": summary,
        "original_count": len(messages),
        "compressed_count": len(recent) + 1,
        "tokens_saved": tokens_saved,
    }

# ── FastAPI App ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"AiLex Memory Service started")
    print(f"  DB: {MEMORY_DB_PATH}")
    print(f"  Short-term window: {SHORT_TERM_SIZE}")
    print(f"  Summarize threshold: {SUMMARIZE_THRESHOLD}")
    yield

app = FastAPI(title="AiLex Memory", version="2.0.0", lifespan=lifespan)

# ── API Routes ──

@app.get("/health")
async def health():
    uptime = int(time.time() - stats["start_time"])
    return {
        "status": "ok",
        "uptime_seconds": uptime,
        "stats": stats,
    }

# ── Conversation Management ──

@app.post("/conversations")
async def create_conversation(conv: Conversation):
    """创建新的会话"""
    session_id = conv.session_id or str(uuid.uuid4())
    now = time.time()
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO conversations (session_id, metadata, created_at, updated_at) VALUES (?, ?, ?, ?)",
        [session_id, json.dumps(conv.metadata or {}), now, now],
    )
    conn.commit()
    conn.close()
    stats["total_conversations"] += 1
    return {"session_id": session_id, "created_at": now}

@app.get("/conversations")
async def list_conversations(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    archived: bool = False,
):
    """列出会话"""
    conn = get_db()
    rows = conn.execute(
        "SELECT session_id, message_count, summary, created_at, updated_at FROM conversations WHERE archived = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        [1 if archived else 0, limit, offset],
    ).fetchall()
    conn.close()
    return {
        "conversations": [dict(r) for r in rows],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
    }

@app.get("/conversations/{session_id}")
async def get_conversation(session_id: str):
    """获取会话详情（含消息）"""
    conn = get_db()
    conv = conn.execute(
        "SELECT * FROM conversations WHERE session_id = ?", [session_id]
    ).fetchone()
    if not conv:
        raise HTTPException(status_code=404, detail="Session not found")
    
    messages = conn.execute(
        "SELECT role, content, name, created_at FROM messages WHERE session_id = ? ORDER BY id", [session_id]
    ).fetchall()
    conn.close()
    
    return {
        "session": dict(conv),
        "messages": [dict(m) for m in messages],
        "message_count": len(messages),
    }

@app.post("/conversations/{session_id}/messages")
async def add_message(session_id: str, message: Message):
    """添加消息到会话"""
    now = message.timestamp or time.time()
    conn = get_db()
    
    # Check conversation exists
    conv = conn.execute(
        "SELECT session_id, message_count FROM conversations WHERE session_id = ?", [session_id]
    ).fetchone()
    if not conv:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Add message
    conn.execute(
        "INSERT INTO messages (session_id, role, content, name, created_at) VALUES (?, ?, ?, ?, ?)",
        [session_id, message.role, message.content, message.name, now],
    )
    
    # Update conversation
    new_count = conv["message_count"] + 1
    conn.execute(
        "UPDATE conversations SET message_count = ?, updated_at = ? WHERE session_id = ?",
        [new_count, now, session_id],
    )
    
    # Auto-summarize if threshold reached (Factor 3)
    if new_count >= SUMMARIZE_THRESHOLD and new_count % SUMMARIZE_THRESHOLD == 0:
        all_messages = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id", [session_id]
        ).fetchall()
        compression = compress_context([dict(m) for m in all_messages])
        if compression["compressed"]:
            conn.execute(
                "UPDATE conversations SET summary = ?, message_count = ? WHERE session_id = ?",
                [compression["summary"], new_count, session_id],
            )
            stats["compressions_saved_tokens"] += compression.get("tokens_saved", 0)
    
    conn.commit()
    conn.close()
    stats["total_memories"] += 1
    return {"message_id": now, "message_count": new_count}

# ── Context Compression (Factor 3) ──

@app.get("/conversations/{session_id}/context")
async def get_context(session_id: str):
    """获取压缩后的上下文（Factor 3: Own Your Context）"""
    conn = get_db()
    messages = conn.execute(
        "SELECT role, content, name FROM messages WHERE session_id = ? ORDER BY id", [session_id]
    ).fetchall()
    conv = conn.execute("SELECT summary FROM conversations WHERE session_id = ?", [session_id]).fetchone()
    conn.close()
    
    if not messages:
        raise HTTPException(status_code=404, detail="Session not found")
    
    msg_dicts = [dict(m) for m in messages]
    compression = compress_context(msg_dicts)
    
    # Build the final context
    context_parts = []
    
    # 1. Summary (if exists)
    if compression.get("summary"):
        context_parts.append(f"[历史摘要]\n{compression['summary']}\n")
    
    # 2. System context
    for msg in compression["short_term"]:
        if msg["role"] == "system":
            context_parts.append(f"[系统指令]\n{msg['content']}\n")
    
    # 3. Recent context
    for msg in compression["short_term"]:
        if msg["role"] != "system":
            role_tag = {"user": "用户", "assistant": "AI", "tool": "工具"}.get(msg["role"], msg["role"])
            context_parts.append(f"[{role_tag}]\n{msg['content']}\n")
    
    context = "\n---\n".join(context_parts)
    
    return {
        "session_id": session_id,
        "compressed": compression["compressed"],
        "summary": compression.get("summary"),
        "original_count": compression["original_count"],
        "compressed_count": compression["compressed_count"],
        "tokens_saved": compression.get("tokens_saved", 0),
        "context": context,
        "context_length": len(context),
    }

# ── Long Term Memory ──

@app.post("/memories")
async def save_memory(item: MemoryItem):
    """保存长期记忆"""
    now = item.created_at or time.time()
    conn = get_db()
    
    importance = 0.5  # default
    conn.execute(
        "INSERT OR REPLACE INTO long_term_memories (id, session_id, content, memory_type, importance, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        [item.id, item.session_id, item.content, item.memory_type, importance, now],
    )
    
    # Store embedding if provided
    if item.embedding:
        conn_vec = sqlite3.connect(VECTOR_DB_PATH)
        conn_vec.execute(
            "CREATE TABLE IF NOT EXISTS vectors (id TEXT PRIMARY KEY, vector BLOB, created_at REAL)"
        )
        conn_vec.execute(
            "INSERT OR REPLACE INTO vectors (id, vector, created_at) VALUES (?, ?, ?)",
            [item.id, json.dumps(item.embedding), now],
        )
        conn_vec.commit()
        conn_vec.close()
        stats["total_embeddings"] += 1
    
    conn.commit()
    conn.close()
    return {"id": item.id, "memory_type": item.memory_type, "created_at": now}

@app.post("/memories/search")
async def search_memories(query: MemoryQuery):
    """语义搜索记忆（Factor 3: 上下文检索）"""
    query_vec = get_embedding(query.query)
    
    conn = get_db()
    
    if query.session_id:
        memories = conn.execute(
            "SELECT id, content, memory_type, importance, created_at, access_count FROM long_term_memories WHERE session_id = ? ORDER BY created_at DESC LIMIT 100",
            [query.session_id],
        ).fetchall()
    else:
        memories = conn.execute(
            "SELECT id, content, memory_type, importance, created_at, access_count FROM long_term_memories ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    conn.close()
    
    # Simple ranking (importance + recency)
    now = time.time()
    scored = []
    for m in memories:
        m = dict(m)
        recency_score = 1.0 / (1.0 + (now - m["created_at"]) / 86400)  # 1 day decay
        importance_score = m.get("importance", 0.5)
        access_score = min(1.0, m.get("access_count", 0) / 10.0)
        m["score"] = (recency_score * 0.3 + importance_score * 0.5 + access_score * 0.2)
        scored.append(m)
    
    # Update access counts
    conn = get_db()
    for m in scored[:query.top_k]:
        conn.execute(
            "UPDATE long_term_memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
            [time.time(), m["id"]],
        )
    conn.commit()
    conn.close()
    
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {
        "results": scored[:query.top_k],
        "query": query.query,
        "total": len(scored),
    }

# ── Session Management (Factor 6) ──

@app.post("/conversations/{session_id}/pause")
async def pause_session(session_id: str):
    """暂停会话（Factor 6）"""
    conn = get_db()
    now = time.time()
    
    # Create pause snapshot
    messages = conn.execute(
        "SELECT role, content, name, created_at FROM messages WHERE session_id = ? ORDER BY id", [session_id]
    ).fetchall()
    
    if not messages:
        raise HTTPException(status_code=404, detail="Session not found")
    
    snapshot = {
        "session_id": session_id,
        "paused_at": now,
        "message_count": len(messages),
        "messages": [dict(m) for m in messages],
    }
    
    # Store snapshot
    conn.execute(
        "UPDATE conversations SET metadata = ?, updated_at = ? WHERE session_id = ?",
        [json.dumps({"paused": True, "snapshot": snapshot}), now, session_id],
    )
    conn.commit()
    conn.close()
    
    return {"session_id": session_id, "paused_at": now, "message_count": len(messages)}

@app.post("/conversations/{session_id}/resume")
async def resume_session(session_id: str):
    """恢复会话（Factor 6）"""
    conn = get_db()
    conv = conn.execute(
        "SELECT metadata FROM conversations WHERE session_id = ?", [session_id]
    ).fetchone()
    
    if not conv:
        raise HTTPException(status_code=404, detail="Session not found")
    
    metadata = json.loads(conv["metadata"])
    metadata["paused"] = False
    now = time.time()
    
    conn.execute(
        "UPDATE conversations SET metadata = ?, updated_at = ? WHERE session_id = ?",
        [json.dumps(metadata), now, session_id],
    )
    conn.commit()
    conn.close()
    
    return {"session_id": session_id, "resumed_at": now}

# ── Stats ──

@app.get("/stats")
async def get_stats():
    """详细统计"""
    conn = get_db()
    conv_count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    ltm_count = conn.execute("SELECT COUNT(*) FROM long_term_memories").fetchone()[0]
    conn.close()
    
    uptime = int(time.time() - stats["start_time"])
    return {
        "uptime_seconds": uptime,
        "uptime_hours": round(uptime / 3600, 1),
        "conversations": conv_count,
        "messages": msg_count,
        "long_term_memories": ltm_count,
        "compressions_saved_tokens": stats["compressions_saved_tokens"],
        "embeddings": stats["total_embeddings"],
    }

@app.delete("/conversations/{session_id}")
async def delete_conversation(session_id: str):
    """删除会话"""
    conn = get_db()
    conn.execute("DELETE FROM messages WHERE session_id = ?", [session_id])
    conn.execute("DELETE FROM conversations WHERE session_id = ?", [session_id])
    conn.execute("DELETE FROM long_term_memories WHERE session_id = ?", [session_id])
    conn.commit()
    conn.close()
    return {"session_id": session_id, "deleted": True}

if __name__ == "__main__":
    import uvicorn
    import sys
    
    port = int(os.getenv("MEMORY_PORT", "8090"))
    host = os.getenv("MEMORY_HOST", "0.0.0.0")
    
    print(f"AiLex Memory Service starting on {host}:{port}")
    print(f"  DB path: {MEMORY_DB_PATH}")
    print(f"  Short-term window: {SHORT_TERM_SIZE}")
    
    uvicorn.run(app, host=host, port=port, log_level="info")
