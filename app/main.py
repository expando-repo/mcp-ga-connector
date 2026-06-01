"""
FastAPI app pro MCP Google Analytics Connector
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.database import Base, engine
from app.routers import auth, mcp

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle: startup & shutdown."""
    logger.info("🚀 Startup...")
    
    # Inicializuj DB
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    logger.info("✅ DB inicializována")
    
    yield
    
    logger.info("🛑 Shutdown...")


# Vytvoř FastAPI app
app = FastAPI(
    title="MCP GA Connector",
    description="Google Analytics Connector pro Claude.ai",
    lifespan=lifespan,
)

# ========== CORS nastavení ==========
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://claude.ai",
        "https://api.anthropic.com",
        "http://localhost:3000",  # development
        "http://localhost:8000",  # development
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registruj routers
# auth router je WITHOUT prefix — /authorize, /callback budou na root
app.include_router(auth.router, tags=["auth"])
app.include_router(mcp.router, tags=["mcp"])


@app.get("/")
async def root():
    """Health check."""
    return {"status": "ok", "service": "MCP GA Connector"}
