"""
OAuth 2.1 Authorization Server pro Claude Custom Connector.

Dvě vrstvy:
- Claude ↔ náš server: my jsme Authorization Server. Endpointy /register (DCR),
  /authorize, /token + discovery dokumenty. Claudovi vydáváme vlastní JWT (viz oauth_server.py).
- náš server ↔ Google: my jsme klient. /authorize deleguje login na Google, /callback
  vymění Google code za GA credentials a uloží je pod session_id.

Tok:
  Claude  --/authorize-->  náš server  --redirect-->  Google login
  Google  --/callback-->   náš server  (uloží GA creds, vydá náš `code`)  --redirect-->  Claude
  Claude  --/token-->      náš server  (ověří PKCE, vydá JWT access+refresh)
  Claude  --/mcp (Bearer)-> náš server  (Resource Server, viz routers/mcp.py)
"""
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode, parse_qs

# Google při include_granted_scopes vrací i dříve udělené scope (např. userinfo.profile),
# takže vrácený scope nesouhlasí s požadovaným. Bez tohoto by oauthlib ve fetch_token()
# vyhodil "Scope has changed" a výměna tokenu selže. Musí být nastaveno před importem oauthlib.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import json

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import JSONResponse, RedirectResponse
from google_auth_oauthlib.flow import Flow
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
import jwt

from app.config import settings
from app.db.database import get_db, OAuthToken, OAuthState, OAuthClient, AuthCode
from app import oauth_server

logger = logging.getLogger(__name__)
router = APIRouter()

# Google scopes, které žádáme (GA + identita).
SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

AUTH_CODE_TTL = timedelta(minutes=5)

CLIENT_CONFIG = {
    "web": {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uris": [f"{settings.base_url}/callback"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


def create_flow() -> Flow:
    return Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=f"{settings.base_url}/callback",
    )


async def _parse_body(request: Request) -> dict:
    """Naparsuje tělo requestu (form-urlencoded i JSON) bez závislosti na python-multipart."""
    ctype = request.headers.get("content-type", "")
    raw = await request.body()
    if "application/json" in ctype:
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}
    # application/x-www-form-urlencoded (default pro OAuth /token)
    parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


# ===========================================================================
# Discovery (RFC 9728 / RFC 8414)
# ===========================================================================

@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata():
    return JSONResponse(oauth_server.protected_resource_metadata())


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata():
    return JSONResponse(oauth_server.authorization_server_metadata())


# ===========================================================================
# Dynamic Client Registration (RFC 7591)
# ===========================================================================

@router.post("/register")
async def register(request: Request, db: AsyncSession = Depends(get_db)):
    """Claude se zaregistruje a dostane client_id."""
    body = await _parse_body(request)

    redirect_uris = body.get("redirect_uris") or []
    if isinstance(redirect_uris, str):
        redirect_uris = [redirect_uris]
    client_name = body.get("client_name")

    client_id = f"mcp-{uuid.uuid4().hex}"
    client = OAuthClient(
        client_id=client_id,
        client_secret=None,  # public client (PKCE)
        redirect_uris=json.dumps(redirect_uris),
        client_name=client_name,
    )
    db.add(client)
    await db.commit()

    logger.info(f"📝 DCR: client {client_id} ({client_name}) redirect_uris={redirect_uris}")

    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": int(datetime.utcnow().timestamp()),
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_name": client_name,
        },
        status_code=201,
    )


# ===========================================================================
# Authorization endpoint (vrstva Claude ↔ náš server)
# ===========================================================================

@router.get("/authorize")
async def authorize(
    request: Request,
    response_type: str = Query(None),
    client_id: str = Query(None),
    redirect_uri: str = Query(None),
    code_challenge: str = Query(None),
    code_challenge_method: str = Query("S256"),
    state: str = Query(None),
    scope: str = Query(None),
    resource: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Claude sem přesměruje uživatele. Uložíme jeho auth-request a delegujeme login na Google.
    """
    if not redirect_uri:
        return JSONResponse({"error": "invalid_request", "error_description": "chybí redirect_uri"}, status_code=400)

    # Ověř redirect_uri proti registrovanému klientovi (pokud je registrovaný).
    if client_id:
        result = await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
        client = result.scalar_one_or_none()
        if client and redirect_uri not in client.redirect_uri_list():
            logger.error(f"❌ redirect_uri {redirect_uri} není registrovaná pro {client_id}")
            return JSONResponse({"error": "invalid_request", "error_description": "neplatné redirect_uri"}, status_code=400)

    # Náš interní state pro Google round-trip + identita session pro Google creds.
    google_state = uuid.uuid4().hex
    session_id = str(uuid.uuid4())

    oauth_state = OAuthState(
        state=google_state,
        session_id=session_id,
        client_id=client_id,
        claude_redirect_uri=redirect_uri,
        claude_state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method or "S256",
        resource=resource,
        scope=scope,
    )
    db.add(oauth_state)
    await db.commit()

    logger.info(f"🔐 /authorize: client={client_id} session={session_id[:8]}… → Google")

    flow = create_flow()
    auth_url, _ = flow.authorization_url(
        state=google_state,
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return RedirectResponse(auth_url)


# ===========================================================================
# Google callback → vydání našeho authorization code → redirect na Claude
# ===========================================================================

@router.get("/callback")
async def callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    if not code or not state:
        return JSONResponse({"error": "Chybí code nebo state"}, status_code=400)

    result = await db.execute(select(OAuthState).where(OAuthState.state == state))
    oauth_state = result.scalar_one_or_none()
    if not oauth_state:
        logger.error("❌ State nenalezen")
        return JSONResponse({"error": "Neplatný state"}, status_code=400)

    session_id = oauth_state.session_id

    # Vyměň Google code za GA tokeny.
    flow = create_flow()
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        logger.error(f"❌ Token exchange (Google) failed: {type(e).__name__}: {e}", exc_info=True)
        return JSONResponse({"error": "Token exchange failed"}, status_code=400)

    creds = flow.credentials

    # Email z id_tokenu (jen pro přehled).
    google_email = "unknown"
    try:
        if creds.id_token:
            decoded = jwt.decode(creds.id_token, options={"verify_signature": False})
            google_email = decoded.get("email", "unknown")
    except Exception:
        logger.warning("ID token decode failed")

    # Ulož Google credentials pod session_id.
    granted_scopes = list(creds.scopes) if creds.scopes else SCOPES
    token_row = OAuthToken(
        session_id=session_id,
        google_email=google_email,
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        token_expiry=creds.expiry,
        scopes=json.dumps(granted_scopes),
        is_active=True,
    )
    db.add(token_row)

    # Vydej NÁŠ authorization code pro Claude.
    our_code = secrets.token_urlsafe(32)
    auth_code = AuthCode(
        code=our_code,
        session_id=session_id,
        client_id=oauth_state.client_id,
        redirect_uri=oauth_state.claude_redirect_uri,
        code_challenge=oauth_state.code_challenge,
        code_challenge_method=oauth_state.code_challenge_method,
        resource=oauth_state.resource,
        scope=oauth_state.scope,
        expires_at=datetime.utcnow() + AUTH_CODE_TTL,
    )
    db.add(auth_code)

    # Smaž state až po úspěšném uložení (bezpečné opakování při selhání).
    await db.execute(delete(OAuthState).where(OAuthState.state == state))
    await db.commit()

    logger.info(f"✅ Google OK ({google_email}); vydán code pro Claude, session {session_id[:8]}…")

    # Přesměruj zpět na Claude redirect_uri s naším code + původním state.
    params = {"code": our_code}
    if oauth_state.claude_state:
        params["state"] = oauth_state.claude_state
    sep = "&" if "?" in (oauth_state.claude_redirect_uri or "") else "?"
    return RedirectResponse(f"{oauth_state.claude_redirect_uri}{sep}{urlencode(params)}")


# ===========================================================================
# Token endpoint (vrstva Claude ↔ náš server)
# ===========================================================================

@router.post("/token")
async def token(request: Request, db: AsyncSession = Depends(get_db)):
    body = await _parse_body(request)
    grant_type = body.get("grant_type")

    if grant_type == "authorization_code":
        return await _grant_authorization_code(body, db)
    if grant_type == "refresh_token":
        return await _grant_refresh_token(body, db)

    return JSONResponse(
        {"error": "unsupported_grant_type", "error_description": f"grant_type={grant_type}"},
        status_code=400,
    )


async def _grant_authorization_code(body: dict, db: AsyncSession) -> JSONResponse:
    code = body.get("code")
    redirect_uri = body.get("redirect_uri")
    code_verifier = body.get("code_verifier")

    if not code:
        return JSONResponse({"error": "invalid_request", "error_description": "chybí code"}, status_code=400)

    result = await db.execute(select(AuthCode).where(AuthCode.code == code))
    ac = result.scalar_one_or_none()
    if not ac:
        return JSONResponse({"error": "invalid_grant", "error_description": "neznámý code"}, status_code=400)

    # Single-use: smaž hned (ať se code nedá použít dvakrát).
    await db.execute(delete(AuthCode).where(AuthCode.code == code))
    await db.commit()

    if ac.expires_at < datetime.utcnow():
        return JSONResponse({"error": "invalid_grant", "error_description": "code expiroval"}, status_code=400)

    if ac.redirect_uri and redirect_uri and ac.redirect_uri != redirect_uri:
        return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri nesouhlasí"}, status_code=400)

    # PKCE (RFC 7636) – povinné v OAuth 2.1.
    if ac.code_challenge:
        if not code_verifier or not oauth_server.verify_pkce(
            code_verifier, ac.code_challenge, ac.code_challenge_method or "S256"
        ):
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE ověření selhalo"}, status_code=400)

    scope = ac.scope or "mcp"
    access_token, expires_in = oauth_server.mint_access_token(ac.session_id, scope)
    refresh_token = oauth_server.mint_refresh_token(ac.session_id, scope)

    logger.info(f"🎟️  Vydán access token pro session {ac.session_id[:8]}…")

    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "refresh_token": refresh_token,
            "scope": scope,
        }
    )


async def _grant_refresh_token(body: dict, db: AsyncSession) -> JSONResponse:
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        return JSONResponse({"error": "invalid_request", "error_description": "chybí refresh_token"}, status_code=400)

    payload = oauth_server.decode_token(refresh_token, expected_type="refresh")
    if not payload:
        return JSONResponse({"error": "invalid_grant", "error_description": "neplatný refresh_token"}, status_code=400)

    session_id = payload["sub"]

    # Ověř, že session pořád existuje a je aktivní.
    result = await db.execute(
        select(OAuthToken).where(OAuthToken.session_id == session_id, OAuthToken.is_active == True)
    )
    if not result.scalar_one_or_none():
        return JSONResponse({"error": "invalid_grant", "error_description": "session neexistuje"}, status_code=400)

    scope = payload.get("scope", "mcp")
    access_token, expires_in = oauth_server.mint_access_token(session_id, scope)
    new_refresh = oauth_server.mint_refresh_token(session_id, scope)  # rotace

    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "refresh_token": new_refresh,
            "scope": scope,
        }
    )


# ===========================================================================
# Pomocné endpointy (diagnostika)
# ===========================================================================

@router.get("/status")
async def status(session_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(OAuthToken).where(OAuthToken.session_id == session_id, OAuthToken.is_active == True)
    )
    token = result.scalar_one_or_none()
    if not token:
        return {"authenticated": False}
    return {"authenticated": True, "email": token.google_email}


@router.get("/disconnect")
async def disconnect(session_id: str, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(OAuthToken).where(OAuthToken.session_id == session_id))
    await db.commit()
    return {"success": True}
