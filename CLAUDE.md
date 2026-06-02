# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Co to je

Hostovaný **MCP server (Model Context Protocol)**, který zpřístupňuje Google Analytics 4
do Claude.ai jako Custom Connector. Uživatel se připojí přes Google OAuth 2.0; server uloží jeho
tokeny a proxuje volání GA Admin/Data API jako MCP nástroje. FastAPI + async SQLAlchemy, nasazeno
na VPS za Nginxem (Hetzner). Komentáře a uživatelské texty jsou **česky** — při úpravách to dodržuj.

## Příkazy

```bash
# Lokální běh (kanonický entry point — viz varování níže)
uvicorn app.main:app --reload --port 8000

# Instalace závislostí
pip install -r requirements.txt

# Docker
docker build -t mcp-ga-connector . && docker run -p 8000:8000 --env-file .env mcp-ga-connector

# Vygenerování SECRET_KEY
python -c "import secrets; print(secrets.token_hex(32))"

# Produkční logy (systemd)
journalctl -u mcp-ga-connector -f
```

V repu **nejsou testy, linter ani build krok**. `DEPLOY.md` je kompletní produkční návod na nasazení
(Google Cloud setup, PostgreSQL, systemd, Nginx, Certbot).

## Architektura

```
claude.ai ←HTTPS/SSE→ Nginx ←→ uvicorn (app.main:app) ←→ Google Analytics API
                                      ↓
                                 PostgreSQL (OAuth tokeny, klíčem je session_id)
```

- **`app/main.py`** — skutečná FastAPI aplikace (na ni odkazuje `Dockerfile` i `deploy/mcp-ga-connector.service`).
- **`app/routers/auth.py`** — Google OAuth flow. `/authorize` (zahájí Claude) → Google →
  `/callback` (vymění code, uloží token, vrátí `mcp_uri` s vygenerovaným `session_id`).
  Dále `/status` a `/disconnect`. CSRF `state` se během round-tripu ukládá do tabulky `oauth_states`.
- **`app/routers/mcp.py`** — MCP transport. `GET /sse` otevře Server-Sent-Events stream (pošle MCP
  handshake + `tools/list`, pak vyprazdňuje per-session `asyncio.Queue` s keepalive pingem á 30 s).
  `POST /message?session_id=...` přijme JSON-RPC `tools/call`, spustí nástroj a vloží odpověď do
  fronty dané session. **Fronty zpráv jsou v paměti procesu** (`_message_queues` dict), takže
  nasazení s více workery/procesy rozbije doručování SSE zpráv — systemd unit běží s `--workers 2`,
  což je skrytá chyba.
- **`app/tools.py`** — pět GA nástrojů (`get_account_summaries`, `get_property_details`,
  `run_report`, `run_realtime_report`, `get_custom_dimensions_and_metrics`). Synchronní volání
  Google API klienta jsou obalena v `run_in_executor`. `get_tools_definition()` vrací MCP schéma,
  `handle_tool_call()` dispatchuje.
- **`app/db/database.py`** — async engine + modely `OAuthToken` / `OAuthState`. Tabulky se vytvoří
  automaticky při startu přes `Base.metadata.create_all` (žádné migrace). `get_db()` je FastAPI
  dependency pro session.
- **`app/config.py`** — pydantic-settings načítané z `.env`. Všechny z `google_client_id`,
  `google_client_secret`, `base_url`, `secret_key`, `database_url` jsou **povinné**; bez nich se
  aplikace nespustí.

Identitou pro auth je serverem vygenerovaný `session_id` (UUID) uložený jako PK tabulky
`oauth_tokens`; protéká OAuth callbackem do MCP URI a vyžadují ho oba endpointy `/sse` i `/message`.

## Důležité: drift mezi moduly / známé nekonzistence

Repo aktuálně obsahuje **dvě konkurenční kopie** wiringu aplikace. Při změnách pracuj proti `app/`
(nasazený kód) a měj na paměti, že si vzájemně odporují:

1. **`/main.py` (kořen repa) je mrtvý/legacy** — mountuje auth router pod prefix `/auth` a importuje
   `init_db`/`get_db` jinak. Nic ho nenasazuje. Živý entry point je `app.main:app`. Poslední commit
   "Remove /auth prefix" se týkal `app/main.py`, který teď mountuje auth na **root** (`/authorize`, `/callback`).

Při opravě se rozhodni pro jeden app modul, místo záplatování okolo té divergence.

OAuth cesty jsou sjednocené na root mounting (žádný `/auth` prefix): `/authorize`, `/callback`,
`/status`, `/disconnect`. `redirect_uri` míří na `{base_url}/callback` — **musí přesně odpovídat
Authorized redirect URI v Google Cloud Console** (po této změně je tam potřeba `/callback`, ne
`/auth/callback`). `/authorize` zvládá jak volání OAuth klientem (Claude Desktop s plnými parametry),
tak holé přesměrování z `/sse` jen se `session_id`.

> Poznámka: `OAuthToken.scopes` ani `token_expiry` se v `/callback` neukládají, takže
> `get_credentials_dict()` vrací prázdné `scopes` a `_build_credentials` nezná expiraci — proaktivní
> refresh se tím pádem nespustí (refresh proběhne až když Google API vrátí 401). Funkční to je,
> ale stojí za pozdější dořešení.
