# -*- coding: utf-8 -*-
"""
J.A.R.V.I.S. Web — FastAPI server.
Serves the HUD interface + command API. Run:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import brain

app = FastAPI(title="J.A.R.V.I.S. Web", version="1.0")
BASE = os.path.dirname(os.path.abspath(__file__))


class Command(BaseModel):
    text: str


@app.post("/api/command")
def command(cmd: Command):
    return brain.handle(cmd.text)


@app.get("/api/events")
def events():
    return {"events": brain.drain_events()}


@app.get("/api/health")
def health():
    return {"status": "online",
            "ai": brain.AI.status(),
            "last_used": brain.AI.last_used}


# Static site (must be mounted last so /api routes win)
app.mount("/", StaticFiles(directory=os.path.join(BASE, "static"), html=True),
          name="static")


@app.exception_handler(404)
async def not_found(request, exc):
    return FileResponse(os.path.join(BASE, "static", "index.html"))
