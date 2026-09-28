# API.md — Market Intel WhatsApp Service API Reference

All API endpoints are served by the **Django backend** (`https://api.yourdomain.com`).

WAHA endpoints are **not directly accessible** from the frontend or external clients.

## Authentication

All endpoints require a Django JWT token:

```
Authorization: Bearer <access_token>
```

The webhook endpoint uses HMAC authentication (no JWT).
The health endpoint has no authentication.

---

## Endpoints

### Health (no auth)

```
GET /api/whatsapp/health/
```

Returns service health without requiring authentication. Safe for monitoring systems.

**Response:**
```json
{
  "status": "HEALTHY",
  "session_status": "READY",
  "outbound_enabled": true,
  "safety_stop_active": false
}
```

**Status values:** `HEALTHY`, `DEGRADED`, `OUTBOUND_BLOCKED`, `SAFETY_STOP`, `DISCONNECTED`

---

### Session Status

```
GET /api/whatsapp/status/
```

**Permissions:** LQ, Manager, Super Admin

**Response:**
```json
{
  "session": {
    "session_name": "default",
    "status": "READY",
    "phone_number": "919876543210",
    "send_count": 42,
    "receive_count": 18,
    "failed_send_count": 0,
    "consecutive_failed_sends": 0,
    "last_successful_message_at": "2026-09-26T10:00:00Z"
  },
  "config": {
    "outbound_enabled": true,
    "safety_stop_active": false
  }
}
```

---

### QR Code

```
GET /api/whatsapp/qr/?session=default
```

**Permissions:** Super Admin only

Returns the WAHA QR code data URL for WhatsApp authentication.

---

### Session Management

```
POST /api/whatsapp/session/start/
POST /api/whatsapp/session/stop/
POST /api/whatsapp/session/logout/
```

**Permissions:** Super Admin only

**Body:**
```json
{"session_name": "default"}
```

---

### Send Message

```
POST /api/whatsapp/send/
```

**Permissions:** LQ, Super Admin

**IMPORTANT:** Recipients are resolved server-side from `prospect_contact_id`.
Do NOT supply a raw phone number or chat ID.

**Body:**
```json
{
  "prospect_contact_id": "uuid-of-prospect-contact",
  "message_body": "Your message text (max 4096 chars)",
  "session_name": "default"
}
```

**Success response (201 Created):**
```json
{
  "id": "uuid",
  "direction": "OUTBOUND",
  "status": "SENT",
  "body_preview": "Your message text...",
  "message_type": "chat",
  "sent_at": "2026-09-26T10:00:00Z"
}
```

**Error responses:**

| Code | Meaning |
|------|---------|
| 400 | Invalid input |
| 403 | Blocked by safety control (code in response) |
| 404 | ProspectContact not found |
| 503 | WAHA service unavailable |

**Safety Guard error codes:**

| Code | Meaning |
|------|---------|
| `WA_DISABLED` | WhatsApp integration is disabled |
| `WA_OUTBOUND_DISABLED` | Kill switch is off |
| `WA_SAFETY_STOP` | Auto-triggered safety stop |
| `WA_OPT_OUT` | Recipient has opted out |
| `WA_COOLDOWN` | Cooldown period not elapsed |
| `WA_RATE_LIMIT_USER` | Per-user hourly limit reached |
| `WA_RATE_LIMIT_GLOBAL` | Global hourly limit reached |
| `WA_NO_PHONE` | Key person has no phone number |
| `WA_INVALID_PHONE` | Phone number format invalid |

---

### Conversations

```
GET /api/whatsapp/conversations/
```

**Permissions:** LQ, Manager, Super Admin

Returns conversations matched to Market Intel contacts.

---

### Conversation Messages

```
GET /api/whatsapp/conversations/<uuid>/messages/
```

**Permissions:** LQ, Manager, Super Admin

---

### Configuration

```
GET  /api/whatsapp/config/
PATCH /api/whatsapp/config/
```

**Permissions:** Manager (read/write), LQ (read only), Super Admin (read/write)

**Body (PATCH):**
```json
{
  "outbound_enabled": true,
  "recipient_cooldown_minutes": 120,
  "global_hourly_limit": 50,
  "per_user_hourly_limit": 20,
  "consecutive_failure_threshold": 5
}
```

---

### Enable/Disable Outbound

```
POST /api/whatsapp/config/outbound/
```

**Permissions:** Manager, Super Admin

**Body:**
```json
{"enabled": false, "reason": "Emergency stop — investigating unusual activity"}
```

---

### Reset Safety Stop

```
POST /api/whatsapp/config/safety-reset/
```

**Permissions:** Super Admin only

**Body (reason is required):**
```json
{"reason": "Investigated 5 failures: all were transient network errors. Account is healthy."}
```

---

### Safety Events

```
GET /api/whatsapp/safety-events/
```

**Permissions:** Manager, Super Admin

Returns the 100 most recent Safety Guard events.

---

### Audit Log

```
GET /api/whatsapp/audit-log/
```

**Permissions:** Manager, Super Admin

Returns the 200 most recent audit log entries.

---

### Opt-Outs

```
GET /api/whatsapp/opt-outs/
```

**Permissions:** Manager, Super Admin

---

### Webhook (HMAC auth)

```
POST /api/whatsapp/webhook/
```

**Authentication:** HMAC-SHA256 via `X-Whatsapp-Signature` header (no JWT).

This endpoint is called by WAHA, not by the React frontend.

**Headers:**
```
X-Whatsapp-Signature: sha256=<hmac-sha256-hex>
Content-Type: application/json
```

**Response (valid event):**
```json
{"status": "received", "event_id": "uuid"}
```

**Response (invalid signature):**
```json
{"detail": "Unauthorized."}
```
HTTP 401

---

## Security Notes

1. The WAHA API key is **never** returned in any response.
2. Session authentication credentials are **never** returned.
3. Internal Oracle VM IP and port are **never** exposed.
4. Webhook secrets are **never** returned.
5. The `/api/whatsapp/send/` endpoint requires `prospect_contact_id` — not a raw phone number. Backend resolves the recipient.
