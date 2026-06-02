"""
MCP transport – Streamable HTTP (spec 2025-06-18).

Jediný endpoint `POST /mcp` přijímá JSON-RPC zprávy a odpovídá `application/json`.
Stateless: identitu nese `Authorization: Bearer <JWT>` (vydaný naším /token), z tokenu
se získá session_id a podle něj Google credentials. Žádné in-memory fronty → funguje
korektně i s více uvicorn workery.

Resource Server část: nevalidní/chybějící token → 401 s WWW-Authenticate (RFC 9728).
"""
import json
import logging

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db, OAuthToken
from app.tools import get_tools_definition, handle_tool_call
from app import oauth_server

logger = logging.getLogger(__name__)
router = APIRouter()

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-03-26", "2025-06-18", "2025-11-25"}

SERVER_INFO = {"name": "google-analytics", "version": "1.0.0"}


def _unauthorized() -> JSONResponse:
    """401 s odkazem na protected-resource metadata (RFC 9728)."""
    resource_meta = f"{oauth_server.issuer()}/.well-known/oauth-protected-resource"
    return JSONResponse(
        {"error": "invalid_token"},
        status_code=401,
        headers={"WWW-Authenticate": f'Bearer resource_metadata="{resource_meta}"'},
    )


def _bearer_session_id(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    payload = oauth_server.decode_token(auth[7:].strip(), expected_type="access")
    if not payload:
        return None
    return payload.get("sub")


def _rpc_result(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


@router.get("/mcp")
async def mcp_get():
    """Server nenabízí server-initiated SSE stream → 405 (dle Streamable HTTP specu)."""
    return Response(status_code=405)


@router.post("/mcp")
async def mcp_post(request: Request, db: AsyncSession = Depends(get_db)):
    # --- autentizace (Resource Server) ---
    session_id = _bearer_session_id(request)
    if not session_id:
        return _unauthorized()

    try:
        message = await request.json()
    except Exception:
        return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)

    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params", {}) or {}

    # Notifikace (bez id) – jen potvrď příjem.
    if req_id is None and method and method.startswith("notifications/"):
        return Response(status_code=202)

    # --- lifecycle ---
    if method == "initialize":
        client_version = params.get("protocolVersion")
        version = client_version if client_version in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
        return JSONResponse(_rpc_result(req_id, result))

    if method == "ping":
        return JSONResponse(_rpc_result(req_id, {}))

    if method == "tools/list":
        return JSONResponse(_rpc_result(req_id, {"tools": get_tools_definition()}))

    if method == "tools/call":
        return await _handle_tools_call(req_id, params, session_id, db)

    # Notifikace, která nezačíná notifications/ (např. po init) – potvrď.
    if req_id is None:
        return Response(status_code=202)

    return JSONResponse(_rpc_error(req_id, -32601, f"Method not found: {method}"))


async def _handle_tools_call(req_id, params: dict, session_id: str, db: AsyncSession) -> JSONResponse:
    result = await db.execute(
        select(OAuthToken).where(OAuthToken.session_id == session_id, OAuthToken.is_active == True)
    )
    token = result.scalar_one_or_none()
    if not token:
        # Token je platný JWT, ale Google creds zmizely (disconnect) → vynuť re-auth.
        return _unauthorized()

    tool_name = params.get("name")
    tool_args = params.get("arguments", {}) or {}
    logger.info(f"🔧 tools/call: {tool_name} args={tool_args}")

    try:
        data = await handle_tool_call(
            tool_name=tool_name,
            args=tool_args,
            credentials_dict=token.get_credentials_dict(),
        )
        # MCP CallToolResult: textový obsah s JSON payloadem.
        call_result = {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}],
            "isError": False,
        }
        return JSONResponse(_rpc_result(req_id, call_result))
    except Exception as e:
        logger.error(f"❌ tools/call failed: {type(e).__name__}: {e}", exc_info=True)
        call_result = {
            "content": [{"type": "text", "text": f"Chyba při volání nástroje: {e}"}],
            "isError": True,
        }
        return JSONResponse(_rpc_result(req_id, call_result))
