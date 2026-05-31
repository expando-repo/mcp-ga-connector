"""
MCP SSE endpoint - hlavní vstupní bod pro Claude.ai
Implementuje Model Context Protocol přes Server-Sent Events (SSE)

Protokol:
1. Claude se připojí na GET /sse?session_id=...
2. Server streamuje SSE zprávy
3. Claude posílá POST /message?session_id=... s JSON-RPC požadavky
"""
import json
import logging
import asyncio
from typing import AsyncGenerator

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db, OAuthToken
from app.tools import handle_tool_call, get_tools_definition
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

# In-memory fronta zpráv per session
# V produkci lze nahradit Redis pub/sub
_message_queues: dict[str, asyncio.Queue] = {}


def get_or_create_queue(session_id: str) -> asyncio.Queue:
    if session_id not in _message_queues:
        _message_queues[session_id] = asyncio.Queue()
    return _message_queues[session_id]


async def sse_generator(session_id: str, queue: asyncio.Queue) -> AsyncGenerator[str, None]:
    """Generuje SSE stream pro danou session."""

    # Poslat inicializační zprávu (MCP handshake)
    init_msg = {
        "jsonrpc": "2.0",
        "method": "connection/established",
        "params": {"sessionId": session_id},
    }
    yield f"data: {json.dumps(init_msg)}\n\n"

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
async def sse_endpoint(request: Request, session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Hlavní SSE endpoint. Claude.ai se sem připojí jako MCP klient.
    URL: https://vasa-domena.cz/sse?session_id=<id>
    """
    # Ověř, že session má platný token
    result = await db.execute(
        select(OAuthToken).where(
            OAuthToken.session_id == session_id,
            OAuthToken.is_active == True,
        )
    )
    token = result.scalar_one_or_none()

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Nepřihlášen. Nejdřív proveď Google OAuth přihlášení.",
        )

    queue = get_or_create_queue(session_id)

    return StreamingResponse(
        sse_generator(session_id, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Nginx: vypni buffering
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
    Claude.ai sem posílá JSON-RPC požadavky (tool calls, ping, atd.)
    """
    body = await request.json()
    method = body.get("method", "")
    msg_id = body.get("id")

    # Načti credentials pro tuto session
    result = await db.execute(
        select(OAuthToken).where(
            OAuthToken.session_id == session_id,
            OAuthToken.is_active == True,
        )
    )
    token_row = result.scalar_one_or_none()

    if not token_row:
        return _error_response(msg_id, -32001, "Nepřihlášen")

    queue = get_or_create_queue(session_id)

    # ---- JSON-RPC dispatch ----

    if method == "initialize":
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": settings.mcp_server_name,
                    "version": "1.0.0",
                },
            },
        }
        await queue.put(response)
        return {"ok": True}

    elif method == "tools/list":
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": get_tools_definition()},
        }
        await queue.put(response)
        return {"ok": True}

    elif method == "tools/call":
        tool_name = body.get("params", {}).get("name")
        tool_args = body.get("params", {}).get("arguments", {})

        try:
            credentials_dict = token_row.get_credentials_dict()
            result_content = await handle_tool_call(tool_name, tool_args, credentials_dict)
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result_content, ensure_ascii=False)}],
                    "isError": False,
                },
            }
        except Exception as e:
            logger.error(f"Tool call error: {e}")
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": f"Chyba: {str(e)}"}],
                    "isError": True,
                },
            }

        await queue.put(response)
        return {"ok": True}

    elif method == "ping":
        await queue.put({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        return {"ok": True}

    else:
        err = _error_response(msg_id, -32601, f"Neznámá metoda: {method}")
        await queue.put(err)
        return {"ok": True}


def _error_response(msg_id, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }
