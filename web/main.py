# -*- coding: utf-8 -*-
"""
J.A.R.V.I.S. Web — FastAPI server.
Serves the HUD interface + command API. Run:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

import os
import time
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import brain

app = FastAPI(title="J.A.R.V.I.S. Web", version="2.1")

# Allow the HUD hosted anywhere (Netlify, Render, cloudflared…) to call this brain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
BASE = os.path.dirname(os.path.abspath(__file__))


class Command(BaseModel):
    text: str
    session_id: str | None = None


@app.post("/api/command")
def command(cmd: Command):
    started = time.perf_counter()
    session_id = cmd.session_id or str(uuid.uuid4())
    try:
        result = brain.handle(cmd.text, session_id=session_id)
    except Exception as e:                       # never let a fault become a 500
        print(f"[command] {e}")
        result = {"replies": ["I hit a snag processing that, sir — please try once more."],
                  "actions": [], "stats": {}}
    stats = result.pop("stats", {}) or {}
    result["meta"] = {
        "session_id": session_id,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "provider": brain.AI.last_used or "local",
        "ai_status": brain.AI.status(),
        "tokens_est": brain.AI.tokens_est,
        "tokens_saved": brain.AI.tokens_saved,
        "cache": bool(stats.get("cache")),
        "cache_hits": stats.get("cache_hits", 0),
        "memories": stats.get("memories", 0),
    }
    return result


@app.get("/api/events")
def events():
    return {"events": brain.drain_events()}


@app.get("/api/health")
def health():
    return {"status": "online",
            "ai": brain.AI.status(),
            "last_used": brain.AI.last_used,
            "last_error": brain.AI.last_error,
            "keys_present": {                      # masked diagnostic: names only, never values
                "JARVIS_API_KEY": bool(os.environ.get("JARVIS_API_KEY")),
                "GROQ_API_KEY": bool(os.environ.get("GROQ_API_KEY")),
                "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")),
            },
            "provider": os.environ.get("JARVIS_LLM_PROVIDER", "gemini"),
            "user": brain.USER_NAME,
            "efficiency": {"tokens_est": brain.AI.tokens_est,
                           "tokens_saved": brain.AI.tokens_saved,
                           "cache_hits": brain.AI.cache_hits},
            "rag": {"memories": len(brain.memory_all())}}


# Static site (must be mounted last so /api routes win)
app.mount("/", StaticFiles(directory=os.path.join(BASE, "static"), html=True),
          name="static")


@app.exception_handler(404)
async def not_found(request, exc):
    return FileResponse(os.path.join(BASE, "static", "index.html"))
