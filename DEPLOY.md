# MCP Google Analytics Connector — Kompletní návod na nasazení

## Přehled architektury

```
Klient v Claude.ai
    ↓  přidá Custom Connector URL
    ↓  klikne Connect → Google OAuth
    ↓  po přihlášení má přístup k GA datům
    
claude.ai ←→ HTTPS/SSE ←→ Nginx ←→ FastAPI (uvicorn) ←→ Google Analytics API
                                          ↓
                                    PostgreSQL (tokeny)
```

---

## ČÁST 1: Google Cloud Console

### 1.1 Vytvoř projekt a povol API

1. Jdi na [console.cloud.google.com](https://console.cloud.google.com)
2. Vytvoř nový projekt, např. `mcp-ga-connector`
3. V menu: **APIs & Services → Library** a povol:
   - `Google Analytics Admin API`
   - `Google Analytics Data API`

### 1.2 Nastav OAuth consent screen

1. **APIs & Services → OAuth consent screen**
2. User Type: **External**
3. Vyplň:
   - App name: `MCP GA Connector` (nebo název tvého produktu)
   - User support email: tvůj email
   - Developer contact: tvůj email
4. Scopes: přidej `analytics.readonly`, `email`, `openid`
5. Test users: přidej emaily klientů (dokud není app verifikována)

> ⚠️ **Produkce:** Pro neomezený přístup bez "test users" musíš projít Google verifikací.
> Pro interní použití stačí test users.

### 1.3 Vytvoř OAuth 2.0 klienta

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Application type: **Web application**
3. Name: `MCP GA Connector`
4. Authorized redirect URIs:
   ```
   https://mcp.vasedomena.cz/callback
   ```
5. Ulož `Client ID` a `Client Secret` — budeš je potřebovat

---

## ČÁST 2: Hetzner VPS — základní setup

```bash
# Připoj se na server
ssh root@IP_SERVERU

# Aktualizace systému
apt update && apt upgrade -y

# Instalace závislostí
apt install -y python3.11 python3.11-venv python3-pip nginx certbot python3-certbot-nginx git postgresql

# Vytvoř systémového uživatele pro službu
useradd -m -s /bin/bash mcpuser
```

---

## ČÁST 3: PostgreSQL

```bash
# Přihlás se jako postgres
sudo -u postgres psql

-- Vytvoř databázi a uživatele
CREATE USER mcpuser WITH PASSWORD 'SILNE_HESLO';
CREATE DATABASE mcpdb OWNER mcpuser;
GRANT ALL PRIVILEGES ON DATABASE mcpdb TO mcpuser;
\q
```

---

## ČÁST 4: Nasazení aplikace

```bash
# Klonuj repozitář
mkdir -p /opt/mcp-ga-connector
cd /opt/mcp-ga-connector
git clone https://github.com/expando-repo/mcp-ga-connector.git .

# Vytvoř virtuální prostředí
python3.11 -m venv venv
source venv/bin/activate

# Nainstaluj závislosti
pip install --upgrade pip
pip install -r requirements.txt

# Nastav oprávnění
chown -R mcpuser:mcpuser /opt/mcp-ga-connector
```

### 4.1 Vytvoř .env soubor

```bash
cp .env.example .env
nano .env
```

Vyplň:
```env
GOOGLE_CLIENT_ID=tvůj-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=tvůj-client-secret

BASE_URL=https://mcp.vasedomena.cz

# Vygeneruj: python3 -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=vygenerovaný_klíč

DATABASE_URL=postgresql+asyncpg://mcpuser:SILNE_HESLO@localhost:5432/mcpdb
```

```bash
# Ochraň .env soubor
chmod 600 .env
chown mcpuser:mcpuser .env
```

---

## ČÁST 5: Systemd služba

```bash
# Zkopíruj service soubor
cp deploy/mcp-ga-connector.service /etc/systemd/system/

# Aktivuj a spusť
systemctl daemon-reload
systemctl enable mcp-ga-connector
systemctl start mcp-ga-connector

# Zkontroluj status
systemctl status mcp-ga-connector
journalctl -u mcp-ga-connector -f
```

---

## ČÁST 6: Nginx + SSL

### 6.1 Nginx konfigurace

```bash
# Nastav doménu v config souboru
sed -i 's/mcp.vasedomena.cz/TVOJA_DOMENA/g' deploy/nginx.conf

# Zkopíruj konfig
cp deploy/nginx.conf /etc/nginx/sites-available/mcp-ga-connector
ln -s /etc/nginx/sites-available/mcp-ga-connector /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

### 6.2 SSL certifikát (Let's Encrypt — zdarma)

```bash
certbot --nginx -d mcp.vasedomena.cz
# Následuj instrukce, zadej email, souhlasí s podmínkami
```

Certbot automaticky upraví nginx config pro HTTPS. ✅

---

## ČÁST 7: Připojení v Claude.ai

### Pro klienty — postup připojení (3 kroky):

1. V Claude.ai jdi do **Settings → Connectors → Add custom connector**
2. Zadej URL:
   ```
   https://mcp.vasedomena.cz/sse?session_id=UNIQUE_SESSION_ID
   ```
   > `session_id` je libovolný unikátní řetězec — každý klient si vygeneruje vlastní,
   > nebo mu ho přidělíš ty.

3. Klikni na **Connect** — otevře se Google OAuth přihlášení
4. Klient se přihlásí svým Google účtem a povolí přístup k Analytics
5. Hotovo ✅

### Ověření funkčnosti

Po připojení může klient Claude.ai napsat:
```
Jaké GA properties mám k dispozici?
```
```
Ukaž mi návštěvnost za posledních 30 dní pro property 123456789
```
```
Kolik uživatelů je právě na mém webu?
```

---

## ČÁST 8: Správa a údržba

### Logy
```bash
journalctl -u mcp-ga-connector -f          # live logy
journalctl -u mcp-ga-connector --since "1h ago"  # poslední hodina
```

### Aktualizace kódu
```bash
cd /opt/mcp-ga-connector
git pull
source venv/bin/activate && pip install -r requirements.txt
systemctl restart mcp-ga-connector
```

### Databáze — přehled přihlášených klientů
```sql
SELECT session_id, google_email, created_at, updated_at
FROM oauth_tokens
WHERE is_active = true
ORDER BY created_at DESC;
```

### Odpojení klienta
```bash
curl "https://mcp.vasedomena.cz/disconnect?session_id=SESSION_ID"
```

---

## Troubleshooting

| Problém | Řešení |
|---------|--------|
| `401 Nepřihlášen` | Klient musí nejdřív projít OAuth na `/authorize?session_id=...` |
| SSE se odpojuje | Zkontroluj nginx `proxy_read_timeout` — musí být 3600s |
| `Token expired` | Refresh token funguje automaticky, pokud je uložen |
| GA API 403 | Zkontroluj, zda jsou povoleny oba GA API v Google Cloud |

---

## Bezpečnostní doporučení

- `.env` soubor nikdy necommituj do gitu — je v `.gitignore`
- Pravidelně rotuj `SECRET_KEY`
- Nastav firewall: `ufw allow 22,80,443/tcp && ufw enable`
- Zvaž přidání rate limitingu v Nginx pro `/auth` endpoint
