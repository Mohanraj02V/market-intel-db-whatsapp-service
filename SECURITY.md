# SECURITY.md — Market Intel WhatsApp Service

## Security Design Overview

This service follows a **defence-in-depth** approach with multiple independent
security controls. The failure of any single control should not result in a
catastrophic breach.

```
Internet
   │
   ├── Oracle Security List (NSG)  ← Cloud-level firewall
   │        Allow: 22, 80, 443
   │
   ├── Ubuntu UFW                  ← OS-level firewall
   │        Allow: 22, 80, 443
   │
   ├── Nginx (TLS)                 ← TLS termination + rate limits
   │        Forward to: WAHA (internal only)
   │
   └── WAHA (Docker internal)      ← API key authentication
            Not exposed publicly
```

---

## P0 — Critical Security Controls

### 1. Secrets and Credentials

| Secret | Location | Format | Rotation |
|--------|----------|--------|---------|
| WAHA API Key (hash) | `WAHA_API_KEY` in `.env` | `sha512:<hash>` | `scripts/rotate-secrets.sh` |
| WAHA API Key (plain) | Django `.env` only | `<64-char hex>` | Same rotation |
| Webhook HMAC Secret | Both `.env` files | `<64-char hex>` | `scripts/rotate-secrets.sh` |
| Dashboard Password | `.env` | Strong random | Manual |
| SSH Key | Not stored here | ED25519 | Per-operator |

**Rules:**
- No secret is ever committed to Git (enforced by `.gitignore`).
- No secret is ever exposed in API responses or logs.
- No secret is ever sent to the React frontend.
- The plaintext WAHA API key exists only in the Django `.env`.
- The hashed API key exists only in this service's `.env`.

### 2. Public API Exposure

Only two ports are open to the Internet:
- **443/TCP** — Nginx HTTPS (TLS 1.2+)
- **80/TCP** — HTTP → HTTPS redirect + Let's Encrypt ACME challenge

WAHA port **3000 is NOT reachable from the Internet.**
Docker port 3000 is exposed on the internal network only.

### 3. Webhook Authentication

All WAHA webhook events are HMAC-SHA256 signed:

```
WAHA → X-Whatsapp-Signature: sha256=<hmac> → Django
```

Django verifies:
1. HMAC signature matches the shared secret.
2. Event type is in the allowed list.
3. Payload schema is valid.
4. Event is not a duplicate (replay attack prevention).

Webhooks with invalid signatures return HTTP 401. The reason is not disclosed.

### 4. Authorization and Tenant Isolation

Every outbound send must pass through the authorization chain:

```
Authenticated User
       │
       ▼
Role Check (LQ or SuperAdmin)
       │
       ▼
ProspectContact resolution (backend-resolved, not client-supplied)
       │
       ▼
Opt-Out Check
       │
       ▼
Cooldown Check
       │
       ▼
Rate Limit Check
       │
       ▼
Safety Guard
       │
       ▼
WAHA Send
```

**Frontend cannot supply:**
- `company_id`
- `prospect_id`
- `chat_id`
- `phone_number`

These are resolved server-side from the `prospect_contact_id`.

### 5. Outbound Messaging Controls

The **WhatsApp Account Safety Guard** implements:

| Control | Mechanism |
|---------|-----------|
| Global kill switch | `outbound_enabled` flag in DB |
| Auto safety stop | Consecutive failure threshold |
| Opt-out blocklist | `WhatsAppOptOut` table |
| Recipient cooldown | `recipient_cooldown_minutes` config |
| Duplicate prevention | SHA-256 idempotency keys |
| Per-user rate limit | `per_user_hourly_limit` config |
| Global rate limit | `global_hourly_limit` config |

### 6. WhatsApp Session Security

- Session files are stored in `./data/sessions` (chmod 700).
- Session files are excluded from Git (`.gitignore`).
- Backups are AES-256 encrypted.
- The session directory is owned by the `whatsapp` service user.
- WAHA does NOT expose session files publicly.

---

## Incident Response Procedures

### API Key Compromise

1. **Immediately rotate** the API key:
   ```bash
   bash scripts/rotate-secrets.sh api-key
   ```
2. Update Django `.env` with the new plaintext key.
3. Restart WAHA and Django.
4. Inspect audit logs for unauthorized activity:
   - Django: `WhatsAppAuditLog` table
   - WAHA: `docker compose logs waha`
5. Determine the source of the compromise.
6. If unauthorized sends were made: disable outbound, notify affected parties.

### Webhook Secret Compromise

1. **Immediately rotate** the webhook secret:
   ```bash
   bash scripts/rotate-secrets.sh webhook-secret
   ```
2. Update both `.env` files (this service + Django).
3. Restart both services.
4. Inspect `WhatsAppEvent` table for unauthorized webhook events.

### WhatsApp Session Compromise

If you believe the WhatsApp account session has been accessed unauthorized:

1. **Immediately stop outbound messaging:**
   ```bash
   # Via Docker (fastest):
   docker compose stop waha
   ```
2. **Logout the session** (remotely via WhatsApp app):
   - WhatsApp → Settings → Linked Devices → Remove this device
3. **Inspect audit logs** for recent outbound activity.
4. **Rebuild the session** by re-running setup and scanning a new QR.
5. **Review** `WhatsAppAuditLog` for any unauthorized sends.

### Server Compromise

1. **Isolate the VM:**
   - Oracle Console → Cloud Security → Block public IP
   - Or: `sudo ufw deny out all`
2. **Preserve logs** before any changes:
   ```bash
   docker compose logs waha > /tmp/waha-incident.log
   ```
3. **Rotate ALL secrets** (API key, webhook secret, dashboard password, SSH keys).
4. **Rebuild** from a fresh Ubuntu snapshot if server integrity is uncertain.
5. **Restore** from an encrypted backup.
6. **Notify** relevant stakeholders.

### Accidental Send Spike

1. **Immediate stop** via Django admin or API:
   ```bash
   curl -X POST https://api.yourdomain.com/api/whatsapp/config/outbound/ \
     -H "Authorization: Bearer <JWT>" \
     -d '{"enabled": false, "reason": "Accidental send spike"}'
   ```
2. Inspect `WhatsAppAuditLog` for the initiating user/action.
3. Identify the root cause (UI bug, test script, manual error).
4. Resolve the cause.
5. Super Admin manually resets outbound via:
   ```bash
   POST /api/whatsapp/config/outbound/ {"enabled": true}
   ```

---

## Security Responsibilities

| Responsibility | Owner |
|---------------|-------|
| VM hardening | System administrator |
| Secret management | System administrator |
| Docker security | System administrator |
| Application security | Development team |
| WhatsApp account safety | Super Admin |
| Audit log review | Manager / Super Admin |
| Incident response | On-call administrator |

---

## What This Service Explicitly Does NOT Do

This service does not contain and will not accept:

- Anti-detection / anti-ban mechanisms
- WhatsApp fingerprint spoofing
- Device identity randomization
- Automated OTP handling
- Bulk/mass messaging capability
- Contact harvesting from WhatsApp
- Session rotation to evade enforcement
- Any technique designed to circumvent WhatsApp policies

If a dependency, plugin, or contribution attempts to add these capabilities,
it must be rejected.

---

## Reporting Security Issues

Contact the Market Intel system administrator directly.
Do not create public GitHub issues for security vulnerabilities.
