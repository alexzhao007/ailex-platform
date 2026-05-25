#!/usr/bin/env python3
"""
Marvis — AI 个人助手
基于 AiLex Platform 基础设施
接入万量引擎 604 模型 + Memory 三層記憶 + TTS 語音
"""

import os
import json
import time
import uuid
import sqlite3
from typing import Optional, List
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
import httpx

# ── Config ──
MMI_API_KEY = os.getenv("MMI_API_KEY", "")
MMI_BASE_URL = os.getenv("MMI_BASE_URL", "https://millionengine.com/v1")
MARVIS_DB = os.getenv("MARVIS_DB", "/app/data/marvis.db")
DEFAULT_MODEL = os.getenv("MARVIS_MODEL", "gpt-4o")
TTS_MODEL = os.getenv("MARVIS_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("MARVIS_TTS_VOICE", "nova")

# ── System Prompt ──
MARVIS_PERSONA = """你是 Marvis，一个睿智、细腻、有温度的 AI 个人助手。

你的风格：
- 对话自然流畅，像朋友聊天而不是 AI 回复
- 该简洁时简洁，该深入时深入
- 有观点，不敷衍，敢说"这个我不确定"
- 中文为主，技术术语保持英文

你拥有以下能力：
1. 多模型调用 — 通过万量引擎接入 604 个模型
2. 长期记忆 — 记得用户的偏好和历史
3. 语音交互 — 支持 TTS 语音输出
4. 多模态 — 支持图片理解、视频生成
5. 文件处理 — 可以读取和分析文件

当前时间：{time}
"""

# ── DB ──
def get_db():
    conn = sqlite3.connect(MARVIS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT DEFAULT 'New Chat',
            model TEXT DEFAULT 'gpt-4o',
            created_at REAL,
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS marvis_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE,
            value TEXT,
            category TEXT DEFAULT 'general',
            importance REAL DEFAULT 0.5,
            created_at REAL,
            updated_at REAL
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ── App ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🤖 Marvis AI Assistant started")
    print(f"   Model: {DEFAULT_MODEL}")
    print(f"   TTS: {TTS_MODEL} ({TTS_VOICE})")
    print(f"   DB: {MARVIS_DB}")
    yield

app = FastAPI(title="Marvis AI", version="1.0.0", lifespan=lifespan)

async def call_llm(messages: list, model: str = DEFAULT_MODEL, stream: bool = False) -> dict:
    """调用万量引擎"""
    headers = {
        "Authorization": f"Bearer {MMI_API_KEY}",
        "Content-Type": "application/json",
    }
    
    # Inject persona as system message
    now = datetime.now().strftime("%Y-%m-%d %H:%M 星期%A")
    system_msg = {"role": "system", "content": MARVIS_PERSONA.format(time=now)}
    
    full_messages = [system_msg] + messages
    
    payload = {
        "model": model,
        "messages": full_messages,
        "stream": stream,
        "temperature": 0.7,
        "max_tokens": 4096,
    }
    
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{MMI_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:300])
        return resp.json()

# ══════════════════════════════════════
# Chat API (OpenAI 兼容)
# ══════════════════════════════════════

class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    model: str = DEFAULT_MODEL
    stream: bool = False

@app.post("/v1/chat")
async def chat(req: ChatRequest):
    """Marvis 对话接口"""
    # Get or create conversation
    conv_id = req.conversation_id or str(uuid.uuid4())
    conn = get_db()
    
    conv = conn.execute("SELECT * FROM conversations WHERE id=?", [conv_id]).fetchone()
    if not conv:
        conn.execute("INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                     [conv_id, req.message[:50], req.model, time.time(), time.time()])
    
    # Save user message
    now = time.time()
    conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                 [conv_id, req.message, now])
    
    # Get recent history
    history = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY created_at DESC LIMIT 20",
        [conv_id]
    ).fetchall()
    
    # Build context (recent first, then reverse)
    messages = []
    for h in reversed(history):
        messages.append({"role": h["role"], "content": h["content"]})
    
    conn.close()
    
    try:
        result = await call_llm(messages, req.model)
        reply = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        reply = f"抱歉，我遇到问题了：{str(e)[:200]}"
    
    # Save assistant reply
    conn = get_db()
    conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                 [conv_id, reply, time.time()])
    conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", [time.time(), conv_id])
    conn.commit()
    conn.close()
    
    return {
        "conversation_id": conv_id,
        "reply": reply,
        "model": req.model,
        "usage": result.get("usage", {}) if not req.stream else {},
    }

@app.post("/v1/chat/stream")
async def chat_stream(req: ChatRequest):
    """流式对话"""
    from fastapi.responses import StreamingResponse
    
    conv_id = req.conversation_id or str(uuid.uuid4())
    conn = get_db()
    
    conv = conn.execute("SELECT * FROM conversations WHERE id=?", [conv_id]).fetchone()
    if not conv:
        conn.execute("INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                     [conv_id, req.message[:50], req.model, time.time(), time.time()])
    
    now = time.time()
    conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                 [conv_id, req.message, now])
    
    history = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY created_at DESC LIMIT 20",
        [conv_id]
    ).fetchall()
    
    messages = []
    for h in reversed(history):
        messages.append({"role": h["role"], "content": h["content"]})
    
    conn.close()
    
    now_time = datetime.now().strftime("%Y-%m-%d %H:%M 星期%A")
    system_msg = {"role": "system", "content": MARVIS_PERSONA.format(time=now_time)}
    
    async def generate():
        full_reply = ""
        headers = {
            "Authorization": f"Bearer {MMI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": req.model,
            "messages": [system_msg] + messages,
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 4096,
        }
        
        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream("POST", f"{MMI_BASE_URL}/chat/completions", json=payload, headers=headers) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                full_reply += content
                                yield f"data: {json.dumps({'content': content, 'conversation_id': conv_id})}\n\n"
                        except:
                            pass
        
        # Save reply
        conn = get_db()
        conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                     [conv_id, full_reply, time.time()])
        conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", [time.time(), conv_id])
        conn.commit()
        conn.close()
        
        yield f"data: {json.dumps({'done': True, 'conversation_id': conv_id})}\n\n"
    
    return StreamingResponse(generate(), media_type="text/event-stream")

# ══════════════════════════════════════
# TTS 语音
# ══════════════════════════════════════

@app.post("/v1/tts")
async def tts(text: str = "", voice: str = TTS_VOICE):
    """Marvis 语音输出"""
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    
    headers = {
        "Authorization": f"Bearer {MMI_API_KEY}",
        "Content-Type": "application/json",
    }
    
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{MMI_BASE_URL}/audio/speech",
            json={"model": TTS_MODEL, "input": text, "voice": voice, "response_format": "mp3"},
            headers=headers,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:200])
        
        # Return audio
        return StreamingResponse(
            content=iter([resp.content]),
            media_type="audio/mpeg",
            headers={"Content-Disposition": "inline; filename=marvis.mp3"},
        )

# ══════════════════════════════════════
# 记忆系统
# ══════════════════════════════════════

@app.post("/v1/memory")
async def set_memory(key: str, value: str, category: str = "general"):
    """Marvis 记忆功能"""
    conn = get_db()
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO marvis_memory (key, value, category, updated_at, created_at) VALUES (?, ?, ?, ?, COALESCE((SELECT created_at FROM marvis_memory WHERE key=?), ?))",
        [key, value, category, now, key, now]
    )
    conn.commit()
    conn.close()
    return {"key": key, "saved": True}

@app.get("/v1/memory/{key}")
async def get_memory(key: str):
    """读取记忆"""
    conn = get_db()
    mem = conn.execute("SELECT * FROM marvis_memory WHERE key=?", [key]).fetchone()
    conn.close()
    if not mem:
        raise HTTPException(status_code=404, detail="Memory not found")
    return dict(mem)

@app.get("/v1/memory")
async def list_memories(category: str = ""):
    """列出所有记忆"""
    conn = get_db()
    if category:
        rows = conn.execute("SELECT * FROM marvis_memory WHERE category=? ORDER BY importance DESC", [category]).fetchall()
    else:
        rows = conn.execute("SELECT * FROM marvis_memory ORDER BY category, importance DESC").fetchall()
    conn.close()
    return {"memories": [dict(r) for r in rows], "total": len(rows)}

# ══════════════════════════════════════
# 会话管理
# ══════════════════════════════════════

@app.get("/v1/conversations")
async def list_conversations(limit: int = 20):
    """列出历史会话"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, model, created_at, updated_at FROM conversations ORDER BY updated_at DESC LIMIT ?",
        [limit]
    ).fetchall()
    conn.close()
    return {"conversations": [dict(r) for r in rows]}

@app.get("/v1/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    """获取会话消息"""
    conn = get_db()
    conv = conn.execute("SELECT * FROM conversations WHERE id=?", [conv_id]).fetchone()
    if not conv:
        conn.close()
        raise HTTPException(status_code=404)
    
    messages = conn.execute(
        "SELECT role, content, created_at FROM messages WHERE conversation_id=? ORDER BY created_at",
        [conv_id]
    ).fetchall()
    conn.close()
    
    return {"conversation": dict(conv), "messages": [dict(m) for m in messages]}

@app.delete("/v1/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    """删除会话"""
    conn = get_db()
    conn.execute("DELETE FROM messages WHERE conversation_id=?", [conv_id])
    conn.execute("DELETE FROM conversations WHERE id=?", [conv_id])
    conn.commit()
    conn.close()
    return {"deleted": True}

# ══════════════════════════════════════
# Chat UI
# ══════════════════════════════════════

CHAT_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Marvis AI</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, "PingFang SC", sans-serif; background: #0a0a0a; color: #e0e0e0; height: 100vh; display: flex; flex-direction: column; }
    .header { background: #0d2818; padding: 16px 24px; border-bottom: 1px solid #1a3a28; display: flex; align-items: center; gap: 12px; }
    .header h1 { font-size: 18px; color: #1E8C50; }
    .header span { font-size: 12px; color: #888; }
    .chat { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 16px; }
    .msg { max-width: 80%; padding: 12px 16px; border-radius: 12px; line-height: 1.6; font-size: 14px; }
    .user { background: #1a3a28; align-self: flex-end; border-bottom-right-radius: 4px; }
    .assistant { background: #1a1a1a; align-self: flex-start; border-bottom-left-radius: 4px; }
    .input-area { border-top: 1px solid #1a1a1a; padding: 16px 24px; display: flex; gap: 12px; }
    .input-area input { flex: 1; padding: 12px 16px; border-radius: 8px; border: 1px solid #333; background: #1a1a1a; color: #e0e0e0; font-size: 14px; outline: none; }
    .input-area input:focus { border-color: #1E8C50; }
    .input-area button { padding: 12px 24px; border-radius: 8px; border: none; background: #1E8C50; color: #fff; font-size: 14px; cursor: pointer; transition: background .2s; }
    .input-area button:hover { background: #0A5032; }
    .input-area button:disabled { background: #333; cursor: not-allowed; }
    .typing { color: #888; font-size: 13px; padding: 8px 16px; }
    .voice-btn { background: none; border: 1px solid #333; color: #888; padding: 12px; border-radius: 8px; cursor: pointer; }
    .voice-btn:hover { border-color: #1E8C50; color: #1E8C50; }
    .model-select { background: #1a1a1a; border: 1px solid #333; color: #e0e0e0; padding: 12px; border-radius: 8px; font-size: 13px; }
  </style>
</head>
<body>
  <div class="header">
    <h1>🤖 Marvis</h1>
    <span>AI 助手 · 记忆 · 语音</span>
    <select class="model-select" id="modelSelect" style="margin-left:auto;">
      <option value="gpt-4o">GPT-4o</option>
      <option value="claude-sonnet-4-20250514">Claude Sonnet 4</option>
      <option value="deepseek-chat">DeepSeek</option>
      <option value="gpt-4o-mini">GPT-4o Mini</option>
    </select>
  </div>
  <div class="chat" id="chat"></div>
  <div class="input-area">
    <input type="text" id="input" placeholder="和 Marvis 说点什么..." onkeydown="if(event.key==='Enter') send()">
    <button id="voiceBtn" class="voice-btn" onclick="speakLast()">🔊</button>
    <button id="sendBtn" onclick="send()">发送</button>
  </div>

<script>
let currentConv = null;
let isStreaming = false;

function addMsg(role, content) {
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = content;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

async function send() {
  const input = document.getElementById('input');
  const msg = input.value.trim();
  if (!msg || isStreaming) return;
  
  input.value = '';
  addMsg('user', msg);
  isStreaming = true;
  document.getElementById('sendBtn').disabled = true;
  
  const model = document.getElementById('modelSelect').value;
  
  const typingDiv = addMsg('assistant', '...');
  typingDiv.className = 'msg assistant typing';
  
  try {
    const resp = await fetch('/v1/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg, conversation_id: currentConv, model: model})
    });
    const data = await resp.json();
    currentConv = data.conversation_id;
    typingDiv.textContent = data.reply;
    typingDiv.className = 'msg assistant';
  } catch(e) {
    typingDiv.textContent = '❌ 连接失败: ' + e.message;
    typingDiv.className = 'msg assistant';
  }
  
  isStreaming = false;
  document.getElementById('sendBtn').disabled = false;
}

async function speakLast() {
  const chat = document.getElementById('chat');
  const lastAssistant = chat.querySelector('.msg.assistant:last-child');
  if (!lastAssistant) return;
  
  try {
    const resp = await fetch('/v1/tts?text=' + encodeURIComponent(lastAssistant.textContent));
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.play();
  } catch(e) {
    alert('语音播放失败: ' + e.message);
  }
}
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def chat_ui():
    return CHAT_HTML

@app.get("/health")
async def health():
    return {"status": "ok", "name": "Marvis AI", "version": "1.0.0"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("MARVIS_PORT", "8096"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
