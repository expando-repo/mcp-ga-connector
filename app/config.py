"""
Konfigurace aplikace - načítá z prostředí / .env souboru
"""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Google OAuth
    google_client_id: str
    google_client_secret: str

    # Server
    base_url: str  # např. https://mcp.vasedomena.cz
    secret_key: str  # náhodný tajný klíč pro šifrování session

    # PostgreSQL
    database_url: str  # postgresql+asyncpg://user:pass@host/dbname

    # MCP
    mcp_server_name: str = "google-analytics"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # Ignoruj další env proměnné


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
