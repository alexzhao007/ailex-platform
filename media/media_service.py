#!/usr/bin/env python3
"""
AiLex Media — 视频/音频生成服务
基于万量引擎 API，100+ 视频/音频模型全覆盖

支持模型家族：
视频生成：Kling 2.0 / Veo 3.1 / Sora 2 / Vidu Q3 / Wan 2.6-2.7 / MiniMax M2
音频/TTS：OpenAI TTS / MiniMax Speech / Qwen3 TTS
"""

import os
import json
import time
import hashlib
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, File, UploadFile, Form
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
import httpx

# ── Config ──
MMI_API_KEY = os.getenv("MMI_API_KEY", "")
MMI_BASE_URL = os.getenv("MMI_BASE_URL", "https://millionengine.com/v1")
MEDIA_STORAGE = os.getenv("MEDIA_STORAGE", "/app/data/media")
os.makedirs(MEDIA_STORAGE, exist_ok=True)

# ── Stats ──
stats = {
    "videos_generated": 0,
    "audio_generated": 0,
    "total_requests": 0,
    "errors": 0,
    "start_time": time.time(),
}

# ── Pydantic Models ──
class VideoRequest(BaseModel):
    prompt: str
    model: str = "kling-video"  # default
    duration: Optional[int] = 5  # seconds
    resolution: Optional[str] = "1080p"
    negative_prompt: Optional[str] = None
    style: Optional[str] = None  # realistic | anime | 3d | etc
    aspect_ratio: Optional[str] = "16:9"
    seed: Optional[int] = None

class AudioRequest(BaseModel):
    text: str
    model: str = "tts-1-hd"
    voice: Optional[str] = None
    speed: Optional[float] = 1.0
    format: Optional[str] = "mp3"

class VoiceCloneRequest(BaseModel):
    name: str
    audio_url: Optional[str] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"AiLex Media Service started")
    print(f"  Video models: Kling 2.0 / Veo 3.1 / Sora 2 / Vidu Q3 / Wan 2.7 / MiniMax M2")
    print(f"  Audio models: OpenAI TTS / MiniMax Speech / Qwen3 TTS")
    print(f"  Storage: {MEDIA_STORAGE}")
    yield

app = FastAPI(title="AiLex Media", version="2.0.0", lifespan=lifespan)

# ── Async HTTP Client ──
async def call_millionengine(model: str, payload: dict, endpoint: str = "chat/completions") -> dict:
    """Generic call to millionengine API"""
    headers = {
        "Authorization": f"Bearer {MMI_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(
            f"{MMI_BASE_URL}/{endpoint}",
            json={**payload, "model": model},
            headers=headers,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
        return resp.json()

# ── Routes ──

@app.get("/health")
async def health():
    uptime = int(time.time() - stats["start_time"])
    return {"status": "ok", "uptime": uptime, "stats": stats}

@app.get("/models")
async def list_media_models():
    """列出可用的视频/音频模型"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{MMI_BASE_URL}/models",
            headers={"Authorization": f"Bearer {MMI_API_KEY}"},
        )
        if resp.status_code != 200:
            return {"error": "cannot fetch models"}
        all_models = resp.json().get("data", [])
    
    # Filter media models
    media_keywords = ["video", "audio", "tts", "speech", "kling", "sora", "veo", 
                      "vidu", "minimax", "wan", "grok-video", "mj_video", "sora"]
    video_models = []
    audio_models = []
    for m in all_models:
        mid = m["id"].lower()
        if any(k in mid for k in ["tts", "speech", "audio"]):
            audio_models.append(m["id"])
        elif any(k in mid for k in ["video", "kling", "sora", "veo", "vidu", "wan", "grok-video", "mj_video"]):
            video_models.append(m["id"])
    
    return {
        "video_models": sorted(video_models),
        "audio_models": sorted(audio_models),
        "video_count": len(video_models),
        "audio_count": len(audio_models),
    }

# ══════════════════════════════════════
# 视频生成
# ══════════════════════════════════════

@app.post("/video/generate")
async def generate_video(req: VideoRequest):
    """生成视频"""
    stats["total_requests"] += 1
    model = req.model
    
    # Model auto-selection
    if model == "auto":
        if "realistic" in (req.style or ""):
            model = "kling-video"
        elif "anime" in (req.style or ""):
            model = "veo3.1"
        else:
            model = "viduq2"  # balanced default
    
    payload = {
        "prompt": req.prompt,
        **({"negative_prompt": req.negative_prompt} if req.negative_prompt else {}),
        **({"duration": req.duration} if req.duration else {}),
        **({"style": req.style} if req.style else {}),
        **({"aspect_ratio": req.aspect_ratio} if req.aspect_ratio else {}),
        **({"seed": req.seed} if req.seed else {}),
        "n": 1,
    }
    
    try:
        result = await call_millionengine(model, {"messages": [{
            "role": "user", "content": f"Generate video: {json.dumps(payload)}"
        }]})
        stats["videos_generated"] += 1
        return {
            "status": "success",
            "model": model,
            "prompt": req.prompt,
            "result": result,
            "generated_at": time.time(),
        }
    except Exception as e:
        stats["errors"] += 1
        raise HTTPException(status_code=500, detail=str(e)[:300])

@app.get("/video/models")
async def list_video_models():
    """推荐视频模型列表"""
    return {
        "recommended": [
            {"id": "kling-video", "name": "Kling 2.0", "quality": "★★★★★", "speed": "★★★", "cost": "中等"},
            {"id": "veo3.1", "name": "Google Veo 3.1", "quality": "★★★★★", "speed": "★★★", "cost": "高"},
            {"id": "sora-2", "name": "OpenAI Sora 2", "quality": "★★★★★", "speed": "★★", "cost": "高"},
            {"id": "viduq3", "name": "Vidu Q3", "quality": "★★★★☆", "speed": "★★★★", "cost": "低"},
            {"id": "wan2.6-i2v", "name": "Wan 2.6", "quality": "★★★★☆", "speed": "★★★★", "cost": "低"},
            {"id": "grok-video-3", "name": "Grok Video 3", "quality": "★★★★☆", "speed": "★★★★★", "cost": "中"},
            {"id": "MiniMax-M2.7", "name": "MiniMax M2.7", "quality": "★★★★☆", "speed": "★★★★", "cost": "中"},
        ],
        "total_available": 100,
    }

# ══════════════════════════════════════
# 音频/TTS 生成
# ══════════════════════════════════════

@app.post("/audio/tts")
async def text_to_speech(req: AudioRequest):
    """文字转语音"""
    stats["total_requests"] += 1
    
    voice_models = {
        "tts-1-hd": {"model": "tts-1-hd", "voices": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]},
        "minimax": {"model": "MiniMax/speech-02-hd", "voices": ["male-1", "female-1", "kid-1"]},
        "qwen3": {"model": "qwen3-tts-flash", "voices": ["default"]},
        "gpt-4o": {"model": "gpt-4o-mini-tts", "voices": ["alloy", "nova", "shimmer", "ash", "sage"]},
    }
    
    model_config = voice_models.get(req.model, voice_models["tts-1-hd"])
    model = model_config["model"]
    voice = req.voice or model_config["voices"][0]
    
    payload = {
        "model": model,
        "input": req.text,
        "voice": voice,
        "speed": req.speed,
        "response_format": req.format,
    }
    
    try:
        headers = {
            "Authorization": f"Bearer {MMI_API_KEY}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{MMI_BASE_URL}/audio/speech",
                json=payload,
                headers=headers,
            )
            if resp.status_code != 200:
                # Fallback: use chat completion to get audio
                fallback = await call_millionengine(model, {"messages": [
                    {"role": "user", "content": f"Read the following text aloud with voice '{voice}': {req.text}"}
                ]})
                stats["audio_generated"] += 1
                return {"status": "success", "model": model, "voice": voice, "result": fallback}
            
            # Store audio file
            file_id = hashlib.md5(f"{req.text}{time.time()}".encode()).hexdigest()[:12]
            file_path = os.path.join(MEDIA_STORAGE, f"{file_id}.{req.format}")
            with open(file_path, "wb") as f:
                f.write(resp.content)
            
            stats["audio_generated"] += 1
            return {
                "status": "success",
                "model": model,
                "voice": voice,
                "duration_seconds": len(req.text) / 15,  # rough estimate
                "file_id": file_id,
                "file_path": file_path,
                "generated_at": time.time(),
            }
    except Exception as e:
        stats["errors"] += 1
        raise HTTPException(status_code=500, detail=str(e)[:300])

@app.get("/audio/voices")
async def list_voices():
    """列出可用音色"""
    return {
        "providers": {
            "openai_tts": {
                "model": "tts-1-hd",
                "voices": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"],
                "description": "OpenAI 标准 TTS",
            },
            "gpt4o_tts": {
                "model": "gpt-4o-mini-tts",
                "voices": ["alloy", "nova", "shimmer", "ash", "sage", "coral"],
                "description": "GPT-4o 新一代 TTS，更自然",
            },
            "minimax_speech": {
                "model": "MiniMax/speech-02-hd",
                "voices": ["male-1", "female-1", "kid-1"],
                "description": "MiniMax 高保真语音",
            },
            "qwen_tts": {
                "model": "qwen3-tts-flash",
                "voices": ["default"],
                "description": "通义千问 TTS，快速",
            },
        }
    }

@app.post("/audio/voice-clone")
async def clone_voice(req: VoiceCloneRequest):
    """声音克隆（MiniMax Voice Clone）"""
    if not req.audio_url:
        return JSONResponse(status_code=400, content={"error": "audio_url required"})
    
    try:
        result = await call_millionengine("MiniMax-Voice-Clone", {
            "messages": [{"role": "user", "content": f"Clone voice from: {req.audio_url}, name: {req.name}"}]
        })
        return {"status": "success", "name": req.name, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:300])

# ══════════════════════════════════════
# 文件服务
# ══════════════════════════════════════

@app.get("/file/{file_id}")
async def get_file(file_id: str):
    """获取生成的媒体文件"""
    for ext in ["mp3", "mp4", "wav", "webm", "png", "jpg"]:
        path = os.path.join(MEDIA_STORAGE, f"{file_id}.{ext}")
        if os.path.exists(path):
            media_type = {
                "mp3": "audio/mpeg", "mp4": "video/mp4", "wav": "audio/wav",
                "webm": "video/webm", "png": "image/png", "jpg": "image/jpeg"
            }.get(ext, "application/octet-stream")
            return FileResponse(path, media_type=media_type)
    raise HTTPException(status_code=404, detail="File not found")

# ══════════════════════════════════════
# 剪辑辅助
# ══════════════════════════════════════

@app.post("/edit/script")
async def generate_script(topic: str = Form(...), duration: int = Form(60), style: str = Form("tutorial")):
    """AI 生成视频脚本"""
    prompt = f"Write a {duration}-second video script about '{topic}' in {style} style. Include: hook, main content, call-to-action."
    result = await call_millionengine("gpt-4o", {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
    })
    script_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    return {
        "topic": topic,
        "duration_seconds": duration,
        "style": style,
        "script": script_text,
        "estimated_words": len(script_text),
    }

@app.post("/edit/subtitle")
async def generate_subtitles(text: str = Form(...)):
    """从文案生成字幕分段"""
    prompt = f"Split the following text into subtitle segments (max 20 chars per line, max 2 lines per subtitle). Output as JSON array with 'text' and 'duration_seconds' fields:\n\n{text}"
    result = await call_millionengine("gpt-4o", {
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    })
    return result

@app.get("/stats")
async def get_stats():
    uptime = int(time.time() - stats["start_time"])
    return {
        "uptime_hours": round(uptime / 3600, 1),
        "videos_generated": stats["videos_generated"],
        "audio_generated": stats["audio_generated"],
        "total_requests": stats["total_requests"],
        "errors": stats["errors"],
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("MEDIA_PORT", "8091"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
