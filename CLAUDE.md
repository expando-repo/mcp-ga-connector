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
claude.ai ←HTTPS→ Nginx ←→ uvicorn (app.main:app) ←→ Google Analytics API
   (OAuth 2.1,                    ↓
    Bearer JWT)            PostgreSQL (Google tokeny + OAuth 2.1 stav)
```

**Dvě vrstvy OAuth.** Server je vůči Claudovi plnohodnotný **OAuth 2.1 Authorization Server**
(Claude je klient, dostává náš vlastní JWT), a zároveň je **klientem vůči Googlu** (deleguje login,
ukládá GA credentials). To jsou dva nezávislé tokeny: náš JWT v sobě nese `session_id` (claim `sub`),
podle kterého Resource Server dohledá Google credentials v `oauth_tokens`.

Tok: `Claude →/authorize→ náš server →redirect→ Google →/callback→ náš server (uloží GA creds,
vydá náš code) →redirect→ Claude →/token→ náš JWT →/mcp (Bearer)→ nástroje`.

- **`app/main.py`** — FastAPI aplikace (na ni odkazuje `Dockerfile` i `deploy/mcp-ga-connector.service`).
- **`app/oauth_server.py`** — OAuth 2.1 helpery: mint/verify JWT access+refresh tokenů (HS256 přes
  `SECRET_KEY`), PKCE S256 ověření, discovery dokumenty (RFC 8414 / RFC 9728). Kanonická resource
  URI je `{base_url}/mcp`, issuer je `{base_url}`.
- **`app/routers/auth.py`** — Authorization Server. Discovery (`/.well-known/oauth-protected-resource`,
  `/.well-known/oauth-authorization-server`), Dynamic Client Registration `POST /register` (RFC 7591),
  `GET /authorize` (uloží Claude auth-request do `oauth_states` a deleguje na Google),
  `GET /callback` (vymění Google code, uloží GA creds pod `session_id`, vydá náš `auth_code`,
  přesměruje zpět na Claudovo `redirect_uri`), `POST /token` (granty `authorization_code` s PKCE
  a `refresh_token`, vydává JWT). Tělo se parsuje ručně (`_parse_body`) — bez `python-multipart`.
- **`app/routers/mcp.py`** — MCP transport **Streamable HTTP** (spec 2025-06-18). Jediný endpoint
  `POST /mcp` přijímá JSON-RPC (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`,
  `ping`) a odpovídá `application/json`. **Stateless** — identitu nese `Authorization: Bearer <JWT>`,
  žádné in-memory fronty, takže `--workers 2` je v pořádku. Chybějící/nevalidní token → `401` s
  `WWW-Authenticate: Bearer resource_metadata=...`. `GET /mcp` → `405` (server nenabízí push stream).
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

Identitou pro auth je serverem vygenerovaný `session_id` (UUID), klíč tabulky `oauth_tokens`
(Google creds). Vůči Claudovi se ven nese **náš JWT** s tímto `session_id` v claimu `sub`.

## DB tabulky

- **`oauth_tokens`** — Google credentials per `session_id` (access/refresh token, expiry, scopes).
- **`oauth_states`** — dočasný stav během Google round-tripu; drží i původní Claude auth-request
  (`client_id`, `claude_redirect_uri`, `claude_state`, `code_challenge`, `resource`, `scope`).
- **`oauth_clients`** — klienti zaregistrovaní přes DCR (jejich `redirect_uris`).
- **`auth_codes`** — krátkodobé (5 min) authorization codes, které vydáváme Claudovi; single-use,
  ověřují se proti PKCE `code_challenge`.

Tabulky se vytvoří automaticky při startu (`Base.metadata.create_all`, žádné migrace).

## Důležité: konfigurace a kompatibilita

- **Google redirect_uri** je `{base_url}/callback` — musí přesně odpovídat Authorized redirect URI
  v Google Cloud Console.
- **MCP URL pro Claude konektor** je `{base_url}/mcp` (ne kořen, ne `/sse`).
- `OAUTHLIB_RELAX_TOKEN_SCOPE=1` se nastavuje v `auth.py` před importem oauthlib — Google přes
  `include_granted_scopes` vrací širší scope, než žádáme (např. `userinfo.profile`), což by jinak
  shodilo `fetch_token()` s „Scope has changed".
- `OAuthToken.scopes` i `token_expiry` se v `/callback` ukládají a `_build_credentials` z nich
  staví `Credentials` s `expiry`, takže proaktivní refresh Google tokenu funguje.
