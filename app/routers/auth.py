"""
Google OAuth 2.0 flow
- /auth/login?session_id=... → přesměruje na Google
- /auth/callback            → zpracuje token, uloží do DB
- /auth/status?session_id=  → zjistí stav přihlášení
- /auth/disconnect          → smaže token
"""
import json
import secrets
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GoogleRequest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

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
async def login(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Spustí OAuth flow. Claude.ai zavolá tento endpoint s session_id.
    """
    # Vygeneruj CSRF state
    state = secrets.token_urlsafe(32)

    # Ulož state → session_id mapping
    oauth_state = OAuthState(state=state, session_id=session_id)
    db.add(oauth_state)
    await db.commit()

    # Vytvoř Google OAuth URL
    flow = create_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
        prompt="consent",  # vždy zobrazí dialog, aby dostali refresh_token
    )

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
    """
    # Ověř state (CSRF)
    result = await db.execute(
        select(OAuthState).where(OAuthState.state == state)
    )
    oauth_state = result.scalar_one_or_none()

    if not oauth_state:
        raise HTTPException(status_code=400, detail="Neplatný nebo expirovaný OAuth state")

    session_id = oauth_state.session_id

    # Smaž použitý state
    await db.execute(delete(OAuthState).where(OAuthState.state == state))

    # Vyměň kód za token
    flow = create_flow()
    flow.fetch_token(code=code)
    credentials = flow.credentials

    # Zjisti email uživatele
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {credentials.token}"},
        )
        user_info = resp.json()
        email = user_info.get("email", "neznámý")

    # Ulož / aktualizuj token v DB
    result = await db.execute(
        select(OAuthToken).where(OAuthToken.session_id == session_id)
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.access_token = credentials.token
        existing.refresh_token = credentials.refresh_token or existing.refresh_token
        existing.token_expiry = credentials.expiry
        existing.scopes = json.dumps(list(credentials.scopes or []))
        existing.google_email = email
        existing.is_active = True
        existing.updated_at = datetime.utcnow()
    else:
        token_row = OAuthToken(
            session_id=session_id,
            google_email=email,
            access_token=credentials.token,
            refresh_token=credentials.refresh_token,
            token_expiry=credentials.expiry,
            scopes=json.dumps(list(credentials.scopes or [])),
        )
        db.add(token_row)

    await db.commit()
    logger.info(f"✅ Uživatel {email} se přihlásil (session: {session_id[:8]}...)")

    return HTMLResponse(content=f"""
    <html>
    <head><style>
        body {{ font-family: sans-serif; display: flex; justify-content: center;
                align-items: center; height: 100vh; margin: 0; background: #f5f5f5; }}
        .card {{ background: white; border-radius: 12px; padding: 40px;
                 text-align: center; box-shadow: 0 2px 20px rgba(0,0,0,0.1); max-width: 400px; }}
        .check {{ font-size: 48px; }}
    </style></head>
    <body>
    <div class="card">
        <div class="check">✅</div>
        <h2>Přihlášení úspěšné</h2>
        <p>Přihlášen jako: <strong>{email}</strong></p>
        <p>Můžete zavřít toto okno a vrátit se do Claude.ai.</p>
    </div>
    </body></html>
    """)


@router.get("/status")
async def status(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Claude.ai zjistí, zda je uživatel přihlášen.
    """
    result = await db.execute(
        select(OAuthToken).where(
            OAuthToken.session_id == session_id,
            OAuthToken.is_active == True,
        )
    )
    token = result.scalar_one_or_none()

    if not token:
        return {"connected": False}

    return {
        "connected": True,
        "email": token.google_email,
    }


@router.get("/disconnect")
async def disconnect(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Odpojí uživatele - smaže token z DB.
    """
    await db.execute(
        delete(OAuthToken).where(OAuthToken.session_id == session_id)
    )
    await db.commit()
    return {"disconnected": True}
