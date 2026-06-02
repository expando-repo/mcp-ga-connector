"""
MCP (Model Context Protocol) routes pro SSE stream a message handling.
"""
import asyncio
import json
import logging
import uuid
from typing import AsyncGenerator, Optional, Dict, Any
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.db.database import get_db, OAuthToken
from app.tools import get_tools_definition, handle_tool_call

logger = logging.getLogger(__name__)
router = APIRouter()

# In-memory message queues per session
_message_queues: Dict[str, asyncio.Queue] = {}


async def get_or_create_queue(session_id: str) -> asyncio.Queue:
    """Vrátí existující queue nebo vytvoří novu."""
    if session_id not in _message_queues:
        _message_queues[session_id] = asyncio.Queue()
    return _message_queues[session_id]


async def sse_generator(session_id: str, queue: asyncio.Queue, email: str) -> AsyncGenerator[str, None]:
    """Generuje SSE stream pro danou session."""

    # 1. Poslat inicializační zprávu (MCP handshake)
    init_msg = {
        "jsonrpc": "2.0",
        "method": "connection/established",
        "params": {"sessionId": session_id},
    }
    yield f"data: {json.dumps(init_msg)}\n\n"
    logger.info(f"✅ MCP connection established pro session {session_id[:8]}...")

    # 2. Poslat seznam nástrojů
    tools_list = get_tools_definition()
    tools_msg = {
        "jsonrpc": "2.0",
        "method": "tools/list",
        "params": {
            "tools": tools_list,
        },
    }
    yield f"data: {json.dumps(tools_msg)}\n\n"
    logger.info(f"✅ Tools list sent: {len(tools_list)} nástrojů")

    try:
        while True:
            try:
                # Čekej na zprávu z fronty (timeout 30s pro keepalive)
                message = await asyncio.wait_for(queue.get(), timeout=30)
                if message is None:  # sentinel pro ukončení
                    break
                yield f"data: {json.dumps(message)}\n\n"
            except asyncio.TimeoutError:
                # Keepalive ping
                yield ": keepalive\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        _message_queues.pop(session_id, None)
        logger.info(f"SSE stream uzavřen: {session_id[:8]}...")


@router.get("/sse")
async def sse_endpoint(
    request: Request,
    session_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Hlavní SSE endpoint. Claude.ai se sem připojí jako MCP klient.
    
    Bez parametrů: https://mcp-ga-connector.locoglobal.ai/sse
    S session_id: https://mcp-ga-connector.locoglobal.ai/sse?session_id=...
    
    Pokud session_id chybí → vygeneruj nový
    Pokud uživatel není přihlášený → přesměruj na OAuth login
    """
    
    # Vygeneruj session_id pokud chybí
    if not session_id:
        session_id = str(uuid.uuid4())
        logger.info(f"✅ Auto-generovaný nový session_id: {session_id[:8]}...")
    
    # Ověř, že session má platný OAuth token
    result = await db.execute(
        select(OAuthToken).where(
            OAuthToken.session_id == session_id,
            OAuthToken.is_active == True,
        )
    )
    token = result.scalar_one_or_none()

    # Pokud není přihlášený → přesměruj na OAuth login
    if not token:
        logger.info(f"Session {session_id[:8]}... není přihlášená, přesměrování na OAuth login")
        from fastapi.responses import RedirectResponse
        redirect_url = f"{settings.base_url}/authorize?session_id={session_id}"
        return RedirectResponse(redirect_url)

    logger.info(f"✅ SSE stream otevřen pro session {session_id[:8]}... (email: {token.google_email})")

    # Vytvoř queue pro tuto session
    queue = await get_or_create_queue(session_id)

    return StreamingResponse(
        sse_generator(session_id, queue, token.google_email),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/message")
async def message_endpoint(
    request: Request,
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Přijímá JSON-RPC zprávu od Claude.ai a zpracovává ji.
    Claude.ai POST-uje sem s tool call requestem.
    """
    try:
        body = await request.json()
    except Exception as e:
        return {"error": f"Neplatný JSON: {str(e)}"}

    # Ověř session
    result = await db.execute(
        select(OAuthToken).where(
            OAuthToken.session_id == session_id,
            OAuthToken.is_active == True,
        )
    )
    token = result.scalar_one_or_none()

    if not token:
        return {"error": "Session není přihlášená"}

    logger.info(f"📨 Message z Claude: {body.get('method', 'unknown')}")

    # Zpracuj zprávu
    if body.get("method") == "tools/call":
        tool_name = body.get("params", {}).get("name")
        tool_args = body.get("params", {}).get("arguments", {})

        logger.info(f"🔧 Tool call: {tool_name} se argumenty: {tool_args}")

        # Zavolej handler
        try:
            result = await handle_tool_call(
                tool_name=tool_name,
                args=tool_args,
                credentials_dict=token.get_credentials_dict(),
            )

            response = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": result,
            }
        except Exception as e:
            logger.error(f"❌ Tool call failed: {e}")
            response = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {
                    "code": -32603,
                    "message": str(e),
                },
            }

        # Pošli response zpět do SSE stream
        queue = await get_or_create_queue(session_id)
        await queue.put(response)

        return response

    return {"error": f"Neznámá metoda: {body.get('method')}"}
