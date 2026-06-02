"""
PostgreSQL databáze - ukládání OAuth tokenů per uživatel a OAuth 2.1 stavu.

Dvě vrstvy OAuth:
- Claude ↔ náš server: jsme Authorization Server (tabulky oauth_clients, auth_codes,
  oauth_states drží Claude auth-request během Google round-tripu).
- náš server ↔ Google: jsme klient (tabulka oauth_tokens drží Google credentials,
  klíčem je session_id).
"""
import json
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, Boolean
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class OAuthToken(Base):
    """
    Uložené Google OAuth tokeny pro každou session.
    session_id = náhodné ID přiřazené serverem při OAuth flow (UUID).
    """
    __tablename__ = "oauth_tokens"

    session_id = Column(String(64), primary_key=True, index=True)
    google_email = Column(String(255), nullable=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    token_expiry = Column(DateTime, nullable=True)
    scopes = Column(Text, nullable=True)  # JSON list
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_active = Column(Boolean, default=True)

    def get_credentials_dict(self) -> dict:
        return {
            "token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_uri": "https://oauth2.googleapis.com/token",
            "token_expiry": self.token_expiry,
            "scopes": json.loads(self.scopes) if self.scopes else [],
        }


class OAuthState(Base):
    """
    Dočasný stav během Google OAuth round-tripu (CSRF ochrana).

    Drží zároveň původní Claude auth-request, aby ho `/callback` mohl po návratu
    z Googlu dokončit (vydat náš authorization code a přesměrovat na Claude).
    """
    __tablename__ = "oauth_states"

    state = Column(String(128), primary_key=True)  # state, který posíláme Googlu
    session_id = Column(String(64), nullable=False)

    # Původní OAuth request od Claude (vrstva Claude ↔ náš server)
    client_id = Column(String(128), nullable=True)
    claude_redirect_uri = Column(Text, nullable=True)
    claude_state = Column(Text, nullable=True)
    code_challenge = Column(Text, nullable=True)
    code_challenge_method = Column(String(16), nullable=True)
    resource = Column(Text, nullable=True)
    scope = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)


class OAuthClient(Base):
    """
    Klient zaregistrovaný přes Dynamic Client Registration (RFC 7591).
    Typicky Claude.ai – uloží si sem svoje redirect_uris.
    """
    __tablename__ = "oauth_clients"

    client_id = Column(String(128), primary_key=True)
    client_secret = Column(String(255), nullable=True)  # public client → None
    redirect_uris = Column(Text, nullable=False)  # JSON list
    client_name = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def redirect_uri_list(self) -> list[str]:
        return json.loads(self.redirect_uris) if self.redirect_uris else []


class AuthCode(Base):
    """
    Krátkodobý authorization code, který vydáváme my Claudovi (vrstva Claude ↔ náš server).
    Claude ho na `/token` vymění za náš JWT access token. PKCE se ověřuje proti code_challenge.
    """
    __tablename__ = "auth_codes"

    code = Column(String(128), primary_key=True)
    session_id = Column(String(64), nullable=False)  # mapuje na OAuthToken (Google creds)
    client_id = Column(String(128), nullable=True)
    redirect_uri = Column(Text, nullable=True)
    code_challenge = Column(Text, nullable=True)
    code_challenge_method = Column(String(16), nullable=True)
    resource = Column(Text, nullable=True)
    scope = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
