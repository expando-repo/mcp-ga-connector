"""
Google OAuth 2.0 flow + Claude.ai MCP integration
"""
import json
import secrets
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GoogleRequest
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
    session_id: str = None,
    response_type: str = None,
    client_id: str = None,
    redirect_uri: str = None,
    code_challenge: str = None,
    code_challenge_method: str = None,
    state: str = None,
    scope: str = None,
    db: AsyncSession = Depends(get_db)
):
    """
    OAuth login endpoint. Podpora obou flow:
    
    1. Náš flow (s session_id):
       /auth/login?session_id=abc123
    
    2. Claude's OAuth flow (s Claude OAuth parametry):
       /auth/login?response_type=code&client_id=...&redirect_uri=...&state=...
    """
    
    # ---- Detekuj flow typ ----
    is_claude_oauth = (response_type and client_id and redirect_uri)
    
    if is_claude_oauth:
        # Claude OAuth flow — vygeneruj session_id z state
        if not state:
            raise HTTPException(status_code=400, detail="Chybí state parametr")
        
        session_id = str(uuid.uuid4())
        logger.info(f"Claude OAuth flow: vygenerován session_id={session_id[:8]}... ze state={state[:8]}...")
        
        # Ulož mapping: Claude's state → naše session_id
        oauth_state = OAuthState(
            state=state,
            session_id=session_id
        )
        db.add(oauth_state)
        await db.commit()
    
    elif not session_id:
        raise HTTPException(status_code=400, detail="Chybí session_id nebo OAuth parametry")
    
    # ---- Spusť Google OAuth flow ----
    flow = create_flow()
    
    # Pokud je Claude OAuth, pošli Claude's state do Google aby nám ho vrátil v callbacku
    if is_claude_oauth:
        auth_url, _ = flow.authorization_url(
            state=state,
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
    else:
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )

    logger.info(f"Redirecting to Google OAuth: session_id={session_id[:8]}...")
    return RedirectResponse(auth_url)


@router.get("/callback")
async def callback(
    request: Request,
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Google přesměruje sem po přihlášení.
    State je stejný jako v /login (ať je to Claude's state nebo náš).
    """
    # Ověř state (CSRF protection)
    result = await db.execute(
        select(OAuthState).where(OAuthState.state == state)
    )
    oauth_state = result.scalar_one_or_none()

    if not oauth_state:
        logger.error(f"Callback: state '{state[:20]}...' nenalezen v DB")
        raise HTTPException(status_code=400, detail="Neplatný nebo expirovaný OAuth state")

    session_id = oauth_state.session_id

    # Smaž použitý state
    await db.execute(delete(OAuthState).where(OAuthState.state == state))
    await db.commit()

    # Vyměň code za token
    flow = create_flow()
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        raise HTTPException(status_code=400, detail=f"Token exchange selhalo: {str(e)}")

    creds = flow.credentials
    
    # Dekóduj ID token (je to JWT string)
    google_email = "unknown"
    try:
        if creds.id_token:
            # ID token je JWT string, dekóduj ho
            id_token_decoded = jwt.decode(creds.id_token, options={"verify_signature": False})
            google_email = id_token_decoded.get("email", "unknown")
            logger.info(f"Email z ID token: {google_email}")
    except Exception as e:
        logger.warning(f"Nemůžu dekódovat ID token: {e}")
    
    logger.info(f"✅ Google OAuth úspěšný pro session_id={session_id[:8]}... (email: {google_email})")

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

    # Vytvoř MCP URI
    mcp_uri = f"{settings.base_url}/sse?session_id={session_id}"
    logger.info(f"MCP URI: {mcp_uri}")

    # Vrať HTML stránku, která informuje Claude o MCP URI
    # Claude.ai MCP klient sám si vezme mcp_uri z query parametru
    html_content = f"""
    <html>
    <head>
        <title>MCP GA Connector - OAuth Success</title>
    </head>
    <body>
        <h1>✅ OAuth Success!</h1>
        <p>User: {google_email}</p>
        <p>Session: {session_id}</p>
        
        <p>Pokud jsi v Claude.ai, měl by se connector automaticky připojit.</p>
        <p>Pokud ne, zkopíruj tuto MCP URI do nastavení claude.ai:</p>
        <pre>{mcp_uri}</pre>
        
        <script>
        // Pokud je to v iframeu z Claude.ai, pošli mcp_uri zpět
        if (window.opener || window.parent !== window) {{
            try {{
                window.parent.postMessage({{
                    type: 'mcp_uri',
                    mcp_uri: '{mcp_uri}'
                }}, '*');
            }} catch (e) {{
                console.log('Could not post message to parent');
            }}
        }}
        </script>
    </body>
    </html>
    """
    
    return HTMLResponse(html_content)


@router.get("/status")
async def status(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Zjistí stav přihlášení pro danou session.
    """
    result = await db.execute(
        select(OAuthToken).where(
            OAuthToken.session_id == session_id,
            OAuthToken.is_active == True,
        )
    )
    token = result.scalar_one_or_none()

    if not token:
        return {"authenticated": False, "session_id": session_id}

    return {
        "authenticated": True,
        "session_id": session_id,
        "email": token.google_email,
        "created_at": token.created_at.isoformat() if token.created_at else None,
    }


@router.get("/disconnect")
async def disconnect(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Smaže OAuth token pro danou session (logout).
    """
    await db.execute(
        delete(OAuthToken).where(OAuthToken.session_id == session_id)
    )
    await db.commit()

    return {"success": True, "message": "Odhlášeno"}
