"""
OAuth 2.1 Authorization Server helpery (vrstva Claude ↔ náš server).

Náš server je vůči Claudovi Authorization Server i Resource Server. Vydáváme vlastní
JWT access/refresh tokeny (podepsané SECRET_KEY, HS256), nezávislé na Google tokenech.
JWT v sobě nese `session_id` (claim `sub`), podle kterého si Resource Server najde
uložené Google credentials v tabulce oauth_tokens.

Build na MCP authorization spec 2025-06-18: RFC 8414 (AS metadata), RFC 9728
(protected resource metadata), RFC 7636 (PKCE S256), RFC 7591 (DCR), RFC 8707 (resource).
"""
import base64
import hashlib
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings

ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=30)

# Scopes, které tento Resource Server zná (čistě MCP rovina; Google scopes jsou jinde).
SUPPORTED_SCOPES = ["mcp"]


def issuer() -> str:
    """OAuth issuer == base_url (bez trailing slashe)."""
    return settings.base_url.rstrip("/")


def canonical_resource() -> str:
    """
    Kanonická URI tohoto MCP serveru (RFC 8707 audience).
    MCP endpoint běží na /mcp, takže resource je {base_url}/mcp.
    """
    return f"{issuer()}/mcp"


# ---------------------------------------------------------------------------
# PKCE (RFC 7636)
# ---------------------------------------------------------------------------

def verify_pkce(code_verifier: str, code_challenge: str, method: str = "S256") -> bool:
    """Ověří code_verifier proti uloženému code_challenge."""
    if not code_challenge:
        # Nebyla zahájena s PKCE – nic neověřujeme (OAuth 2.1 ale PKCE vyžaduje).
        return False
    if method == "plain":
        return code_verifier == code_challenge
    # S256: base64url(sha256(verifier)) bez paddingu
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return expected == code_challenge


# ---------------------------------------------------------------------------
# JWT tokeny
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def mint_access_token(session_id: str, scope: str = "mcp") -> tuple[str, int]:
    """Vytvoří access JWT. Vrací (token, expires_in_sekund)."""
    now = _now()
    exp = now + ACCESS_TOKEN_TTL
    payload = {
        "iss": issuer(),
        "sub": session_id,
        "aud": canonical_resource(),
        "scope": scope,
        "token_type": "access",
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    return token, int(ACCESS_TOKEN_TTL.total_seconds())


def mint_refresh_token(session_id: str, scope: str = "mcp") -> str:
    now = _now()
    exp = now + REFRESH_TOKEN_TTL
    payload = {
        "iss": issuer(),
        "sub": session_id,
        "aud": canonical_resource(),
        "scope": scope,
        "token_type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str, expected_type: str = "access") -> dict | None:
    """
    Ověří podpis, expiraci a audience. Vrátí payload nebo None při nevalidním tokenu.
    """
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[ALGORITHM],
            audience=canonical_resource(),
            issuer=issuer(),
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
    except jwt.InvalidTokenError:
        return None
    if payload.get("token_type") != expected_type:
        return None
    return payload


# ---------------------------------------------------------------------------
# Discovery dokumenty
# ---------------------------------------------------------------------------

def protected_resource_metadata() -> dict:
    """RFC 9728 – Protected Resource Metadata."""
    return {
        "resource": canonical_resource(),
        "authorization_servers": [issuer()],
        "scopes_supported": SUPPORTED_SCOPES,
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata() -> dict:
    """RFC 8414 – Authorization Server Metadata."""
    base = issuer()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": SUPPORTED_SCOPES,
    }
