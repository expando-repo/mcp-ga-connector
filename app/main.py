"""
MCP Google Analytics Connector
Hostovaný MCP server s Google OAuth 2.0 pro Claude.ai
"""
import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from app.db.database import init_db, get_db
from app.routers import auth, mcp
from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("✅ DB inicializována")
    yield
    logger.info("Server se vypíná")


app = FastAPI(
    title="MCP Google Analytics Connector",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://claude.ai", "https://api.anthropic.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(mcp.router, tags=["mcp"])


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html><body>
    <h2>MCP Google Analytics Connector</h2>
    <p>Server běží. Pro připojení přes Claude.ai:</p>
    <ol>
        <li>Navštiv: <a href="/init">/init</a> - vygeneruje session_id a MCP URL</li>
        <li>Nebo použij: <code>/sse?session_id=UNIQUE_ID</code> s vlastním session_id</li>
    </ol>
    </body></html>
    """


@app.get("/init")
async def init_session():
    """
    Vygeneruje nový session_id a vrátí připravenou URL pro Claude.ai
    Klient zavolá tento endpoint a dostane JSON s připravenou MCP URL.
    """
    session_id = str(uuid.uuid4())
    mcp_url = f"{settings.base_url}/sse?session_id={session_id}"
    
    return JSONResponse({
        "session_id": session_id,
        "mcp_url": mcp_url,
        "instructions": f"Zkopíruj tuto URL do Claude.ai MCP connectoru: {mcp_url}"
    })


@app.get("/health")
async def health():
    return {"status": "ok"}
