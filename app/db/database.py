"""
PostgreSQL databáze - ukládání OAuth tokenů per uživatel
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
    Uložené OAuth tokeny pro každého uživatele.
    session_id = náhodné ID přiřazené klientovi při OAuth flow
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
            "scopes": json.loads(self.scopes) if self.scopes else [],
        }


class OAuthState(Base):
    """
    Dočasný stav během OAuth flow (CSRF ochrana)
    """
    __tablename__ = "oauth_states"

    state = Column(String(128), primary_key=True)
    session_id = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
