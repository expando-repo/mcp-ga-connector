"""
OAuth 2.0 pro Claude Desktop MCP
"""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import RedirectResponse, HTMLResponse
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
    """
    OAuth 2.0 Authorization endpoint pro Claude Desktop.
    """
    
    if not response_type or not client_id or not redirect_uri or not state:
        raise HTTPException(status_code=400, detail="Chybí OAuth parametry")
    
    logger.info(f"🔐 OAuth /login: state={state[:20]}..., client_id={client_id}")
    
    # Ulož mapping: Claude's state → session_id
    session_id = str(uuid.uuid4())
    oauth_state = OAuthState(
        state=state,
        session_id=session_id
    )
    db.add(oauth_state)
    await db.commit()
    
    logger.info(f"✅ Created session {session_id[:8]}... for state {state[:8]}...")
    
    # Spusť Google OAuth flow
    flow = create_flow()
    auth_url, _ = flow.authorization_url(
        state=state,
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    logger.info(f"Redirecting to Google...")
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
    Vyměníme code za token a vrátíme HTML s auto-redirect na /sse.
    """
    
    if not code or not state:
        raise HTTPException(status_code=400, detail="Chybí code nebo state")
    
    logger.info(f"🔄 Callback: state={state[:20]}..., code={code[:20]}...")
    
    # Ověř state v DB
    result = await db.execute(
        select(OAuthState).where(OAuthState.state == state)
    )
    oauth_state = result.scalar_one_or_none()

    if not oauth_state:
        logger.error(f"❌ State '{state[:20]}...' nenalezen v DB")
        raise HTTPException(status_code=400, detail="Neplatný state")

    session_id = oauth_state.session_id
    logger.info(f"Found session: {session_id[:8]}...")

    # Smaž použitý state
    await db.execute(delete(OAuthState).where(OAuthState.state == state))
    await db.commit()

    # Vyměň code za token
    flow = create_flow()
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        logger.error(f"❌ Token exchange failed: {e}")
        raise HTTPException(status_code=400, detail=f"Token exchange selhalo: {str(e)}")

    creds = flow.credentials
    
    # Dekóduj ID token
    google_email = "unknown"
    try:
        if creds.id_token:
            id_token_decoded = jwt.decode(creds.id_token, options={"verify_signature": False})
            google_email = id_token_decoded.get("email", "unknown")
            logger.info(f"Email: {google_email}")
    except Exception as e:
        logger.warning(f"ID token decode failed: {e}")
    
    logger.info(f"✅ OAuth successful: email={google_email}")

    # Ulož token do DB
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

    # ========== CRUCIAL FOR CLAUDE DESKTOP ==========
    # Vrátíme HTTP 302 Redirect (ne HTML)
    mcp_uri = f"{settings.base_url}/sse?session_id={session_id}"
    logger.info(f"🎯 Redirecting to MCP URI: {mcp_uri}")
    
    # Vrátíme 302 redirect — Claude Desktop to automaticky sleduje
    return RedirectResponse(mcp_uri, status_code=302)


@router.get("/status")
async def status(session_id: str, db: AsyncSession = Depends(get_db)):
    """Stav přihlášení pro session."""
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
