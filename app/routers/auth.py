"""
OAuth 2.0 Authorization Server pro Claude.ai MCP
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import JSONResponse, RedirectResponse
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
    OAuth 2.0 Authorization endpoint (Claude.ai OAuth server).
    
    Claude.ai POST-uje sem s OAuth parametry, my jsme Authorization Server.
    My si pak sami spustíme Google OAuth flow pro user.
    """
    
    if not response_type or not client_id or not redirect_uri or not state:
        raise HTTPException(status_code=400, detail="Chybí OAuth parametry")
    
    # Zkontroluj že client_id je legitimní (claude.ai)
    # V produkci bys měl seznam povolených client_ids
    
    logger.info(f"OAuth /login: state={state[:20]}..., redirect_uri={redirect_uri}")
    
    # Ulož mapping: Claude's state + redirect_uri → session_id
    session_id = str(uuid.uuid4())
    oauth_state = OAuthState(
        state=state,
        session_id=session_id
    )
    db.add(oauth_state)
    await db.commit()
    
    logger.info(f"Created session {session_id[:8]}... for Claude state {state[:8]}...")
    
    # Spusť Google OAuth flow
    flow = create_flow()
    
    # Pošli Claude's state do Google aby nám ho vrátil v callbacku
    auth_url, _ = flow.authorization_url(
        state=state,
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    logger.info(f"Redirecting to Google: {auth_url[:100]}...")
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
    Vyměníme code za token a vrátíme JSON do Claude.ai callback URL.
    """
    
    if not code or not state:
        raise HTTPException(status_code=400, detail="Chybí code nebo state")
    
    # Ověř state v DB
    result = await db.execute(
        select(OAuthState).where(OAuthState.state == state)
    )
    oauth_state = result.scalar_one_or_none()

    if not oauth_state:
        logger.error(f"Callback: state '{state[:20]}...' nenalezen v DB")
        # Vrátíme error, ale ne do naší app - to nemůžeme, jsme jen callback
        raise HTTPException(status_code=400, detail="Neplatný state")

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
        raise HTTPException(status_code=400, detail=f"Token exchange selhalo")

    creds = flow.credentials
    
    # Dekóduj ID token
    google_email = "unknown"
    try:
        if creds.id_token:
            id_token_decoded = jwt.decode(creds.id_token, options={"verify_signature": False})
            google_email = id_token_decoded.get("email", "unknown")
    except Exception as e:
        logger.warning(f"ID token decode failed: {e}")
    
    logger.info(f"✅ OAuth successful: session={session_id[:8]}..., email={google_email}")

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

    # ========== KEY CHANGE ==========
    # Vrátíme HTML stránku, která si vezme state z URL a pošle JSON do Claude.ai
    # Claude.ai se připojí přes window.opener.postMessage nebo fetch
    
    html_content = f"""
    <html>
    <head>
        <title>GA Connector - OAuth Success</title>
        <meta charset="utf-8">
    </head>
    <body>
        <h1>✅ Authentication Successful!</h1>
        <p>Closing this window...</p>
        
        <script>
        // Pokud jsme otevřeni v pop-up (z Claude.ai), pošleme data zpět
        if (window.opener) {{
            window.opener.postMessage({{
                type: 'oauth_success',
                state: '{state}',
                session_id: '{session_id}',
                email: '{google_email}',
                mcp_uri: '{mcp_uri}'
            }}, '*');
            
            // Zavři toto okno
            window.close();
        }} else {{
            // Pokud to není pop-up, pošleme JSON přímou redirectem (fallback)
            // Ale to Claude.ai neočekává, takže to nebude fungovat
            document.body.innerHTML = '<pre>' + JSON.stringify({{
                state: '{state}',
                session_id: '{session_id}',
                email: '{google_email}',
                mcp_uri: '{mcp_uri}'
            }}, null, 2) + '</pre>';
        }}
        </script>
    </body>
    </html>
    """
    
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html_content)


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
