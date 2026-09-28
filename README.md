# Market Intel WhatsApp Service

A secure, isolated WhatsApp messaging service for the Market Intel application.

**Architecture:** Oracle Always Free ARM64 + Docker + WAHA (NOWEB engine) + Nginx TLS

```
                         Market Intel Users
                               │
                               ▼
                        React / Vite
                            Vercel
                               │
                               │ HTTPS + JWT
                               ▼
                      Django REST API
                          AWS EC2
                               │
                               │ HTTPS + API Key
                               │ (server-side only)
                               ▼
                 ┌─────────────────────────────┐
                 │  Oracle Always Free ARM64    │
                 │                             │
                 │  Ubuntu 22.04               │
                 │    ├── UFW Firewall          │
                 │    ├── Docker               │
                 │    │    ├── Nginx :443       │
                 │    │    └── WAHA NOWEB ARM   │
                 │    │         └── :3000 (internal only)
                 │    └── fail2ban             │
                 │                             │
                 └──────────────┬──────────────┘
                                │
                           WhatsApp
                                │
                           End User
```

## Quick Start

### Prerequisites
- Oracle Cloud Always Free account
- ARM64 Ubuntu 22.04 VM (2 OCPU, 12 GB RAM)
- Domain name pointing to the VM's public IP
- Existing Market Intel Django backend on AWS EC2

### 1. Clone and configure

```bash
git clone <this-repo> /opt/market-intel-whatsapp-service
cd /opt/market-intel-whatsapp-service

cp .env.example .env
# Fill in ALL values in .env — see DEPLOYMENT.md for guidance
nano .env
```

### 2. Generate secrets

```bash
# API key (plaintext for Django, hash goes into .env)
python3 -c "import secrets; print(secrets.token_hex(64))"

# Webhook secret (same value in both services)
python3 -c "import secrets; print(secrets.token_hex(64))"

# Hash the API key for WAHA:
echo -n "YOUR_PLAINTEXT_KEY" | sha512sum | awk '{print "sha512:" $1}'
```

### 3. Run setup

```bash
sudo bash scripts/setup.sh
```

### 4. Configure Django

Add to Django `backend/.env`:

```env
WHATSAPP_ENABLED=true
WHATSAPP_SERVICE_URL=https://whatsapp.yourdomain.com
WHATSAPP_SERVICE_API_KEY=<plaintext-api-key>
WHATSAPP_WEBHOOK_SECRET=<same-secret-as-WHATSAPP_WEBHOOK_SECRET>
WHATSAPP_SERVICE_TIMEOUT=30
```

Add to Django `config/settings.py`:

```python
INSTALLED_APPS = [
    # ... existing apps ...
    'whatsapp',
]
```

Add to Django `config/urls.py`:

```python
path('api/whatsapp/', include('whatsapp.urls')),
```

### 5. Scan QR code

1. Log in to Market Intel as Super Admin
2. Navigate to the WhatsApp admin panel
3. Click "Start Session"
4. Scan the QR code with WhatsApp → Linked Devices → Link a Device
5. Session is now active

## Security Highlights

| Control | Implementation |
|---------|---------------|
| WAHA API auth | SHA-512 hashed API key |
| Webhook auth | HMAC-SHA256 signature |
| Transport | HTTPS only (TLS 1.2+) |
| WAHA exposure | Internal Docker network only |
| Session files | chmod 700, git-ignored |
| Outbound kill switch | `outbound_enabled` flag |
| Auto-safety stop | Consecutive failure threshold |
| Opt-out protection | Keyword detection + blocklist |
| Duplicate prevention | SHA-256 idempotency keys |
| Rate limiting | Per-user + global hourly limits |
| Audit trail | Immutable `WhatsAppAuditLog` |

## What This Service Does NOT Do

- ❌ Bulk messaging
- ❌ Contact scraping
- ❌ Anti-detection mechanisms
- ❌ Fingerprint spoofing
- ❌ Automated account creation
- ❌ Session rotation to evade restrictions
- ❌ Retry loops to push through WhatsApp restrictions

## Documentation

| Document | Contents |
|----------|----------|
| [DEPLOYMENT.md](DEPLOYMENT.md) | Oracle VM setup, Docker, DNS, HTTPS |
| [SECURITY.md](SECURITY.md) | Security design, controls, incident response |
| [OPERATIONS.md](OPERATIONS.md) | Day-to-day operations, monitoring, backup |
| [API.md](API.md) | API reference for all endpoints |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Common issues and solutions |

## Health Check

```bash
bash scripts/health-check.sh
```

## Emergency Stop

```bash
# Disable all outbound WhatsApp messaging immediately:
# Via Django API (Manager/Admin):
curl -X POST https://api.yourdomain.com/api/whatsapp/config/outbound/ \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false, "reason": "Emergency stop"}'

# Or via Docker (immediate, no API required):
docker compose stop waha
```

## License

Internal use only — Market Intel proprietary.
