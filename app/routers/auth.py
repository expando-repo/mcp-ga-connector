"""
OAuth 2.0 pro Claude Desktop MCP
Vrací JSON response s mcp_uri
"""
import logging
import uuid
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from google_auth_oauthlib.flow import Flow
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
import jwt

from app.config import settings
from app.db.database import get_db, OAuthToken, OAuthState

logger = logging.getLogger(__name__)
router = APIRouter()

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

CLIENT_CONFIG = {
    "web": {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uris": [f"{settings.base_url}/auth/callback"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


def create_flow() -> Flow:
    return Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=f"{settings.base_url}/auth/callback",
    )


@router.get("/login")
async def login(
    request: Request,
    response_type: str = Query(None),
    client_id: str = Query(None),
    redirect_uri: str = Query(None),
    code_challenge: str = Query(None),
    code_challenge_method: str = Query(None),
    state: str = Query(None),
    scope: str = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """OAuth 2.0 Authorization endpoint."""
    
    if not response_type or not client_id or not redirect_uri or not state:
        raise HTTPException(status_code=400, detail="Chybí OAuth parametry")
    
    logger.info(f"🔐 OAuth /login: state={state[:20]}...")
    
    session_id = str(uuid.uuid4())
    oauth_state = OAuthState(state=state, session_id=session_id)
    db.add(oauth_state)
    await db.commit()
    
    logger.info(f"✅ Session {session_id[:8]}...")
    
    flow = create_flow()
    auth_url, _ = flow.authorization_url(
        state=state,
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    logger.info(f"→ Google OAuth redirect")
    from fastapi.responses import RedirectResponse
    return RedirectResponse(auth_url)


@router.get("/callback")
async def callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Google OAuth callback.
    VRACÍ JSON RESPONSE s mcp_uri pro Claude Desktop!
    """
    
    if not code or not state:
        return JSONResponse({"error": "Chybí code nebo state"}, status_code=400)
    
    logger.info(f"🔄 Callback: state={state[:20]}...")
    
    result = await db.execute(
        select(OAuthState).where(OAuthState.state == state)
    )
    oauth_state = result.scalar_one_or_none()

    if not oauth_state:
        logger.error(f"❌ State nenalezen")
        return JSONResponse({"error": "Neplatný state"}, status_code=400)

    session_id = oauth_state.session_id
    
    await db.execute(delete(OAuthState).where(OAuthState.state == state))
    await db.commit()

    # Vyměň code za token
    flow = create_flow()
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        logger.error(f"❌ Token exchange failed")
        return JSONResponse({"error": "Token exchange failed"}, status_code=400)

    creds = flow.credentials
    
    google_email = "unknown"
    try:
        if creds.id_token:
            id_token_decoded = jwt.decode(creds.id_token, options={"verify_signature": False})
            google_email = id_token_decoded.get("email", "unknown")
    except Exception as e:
        logger.warning(f"ID token decode failed")
    
    logger.info(f"✅ OAuth OK: {google_email}")

    # Ulož token
    token_row = OAuthToken(
        session_id=session_id,
        google_email=google_email,
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        token_expiry=creds.expiry,
        is_active=True,
    )
    db.add(token_row)
    await db.commit()

    # ========== KLÍČOVÉ: VRÁTÍME JSON S mcp_uri ==========
    mcp_uri = f"{settings.base_url}/sse?session_id={session_id}"
    logger.info(f"🎯 MCP URI: {mcp_uri}")
    
    # Vrátíme JSON response s mcp_uri
    # Claude Desktop si vezme tuto URL a sám se tam připojí
    return JSONResponse({
        "success": True,
        "mcp_uri": mcp_uri,
        "session_id": session_id,
        "email": google_email,
        "status": "authenticated"
    })


@router.get("/status")
async def status(session_id: str, db: AsyncSession = Depends(get_db)):
    """Stav přihlášení."""
    result = await db.execute(
        select(OAuthToken).where(
            OAuthToken.session_id == session_id,
            OAuthToken.is_active == True,
        )
    )
    token = result.scalar_one_or_none()

    if not token:
        return {"authenticated": False}

    return {
        "authenticated": True,
        "email": token.google_email,
    }


@router.get("/disconnect")
async def disconnect(session_id: str, db: AsyncSession = Depends(get_db)):
    """Odhlášení."""
    await db.execute(
        delete(OAuthToken).where(OAuthToken.session_id == session_id)
    )
    await db.commit()
    return {"success": True}
