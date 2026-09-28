# OPERATIONS.md — Market Intel WhatsApp Service

## Daily Operations

### Health Check

Run at any time to verify service health:

```bash
bash scripts/health-check.sh
```

Or check the API endpoint:

```bash
curl https://whatsapp.yourdomain.com/health
```

Expected healthy response:
```json
{
  "status": "HEALTHY",
  "session_status": "READY",
  "outbound_enabled": true,
  "safety_stop_active": false
}
```

### Health States

| State | Meaning | Action |
|-------|---------|--------|
| `HEALTHY` | All systems operational | None |
| `DEGRADED` | Some checks failing | Investigate logs |
| `OUTBOUND_BLOCKED` | Kill switch active | Check if intentional |
| `SAFETY_STOP` | Auto-triggered stop | Super Admin review required |
| `DISCONNECTED` | WAHA session lost | Re-scan QR |
| `AUTH_REQUIRED` | WhatsApp QR needed | Scan QR |

---

## WhatsApp Session Management

### Check session status

```bash
docker compose exec waha wget -qO- http://127.0.0.1:3000/api/sessions/default
```

### Re-scan QR code (if disconnected)

1. Open Market Intel as Super Admin.
2. Go to WhatsApp management.
3. Click **Start Session** (if stopped).
4. QR code appears — scan with WhatsApp → Linked Devices.

### Force-restart WAHA (without losing session)

```bash
docker compose restart waha
```

### Completely reset the WhatsApp session

⚠️ **WARNING:** This logs out the WhatsApp account.

```bash
# 1. Backup first
bash scripts/backup.sh

# 2. Stop services
docker compose stop

# 3. Delete session data
rm -rf ./data/sessions/*

# 4. Restart
docker compose start

# 5. Scan new QR code
```

---

## Safety Guard Operations

### Check if safety stop is active

```bash
# Via API (Manager/Admin JWT required)
curl -H "Authorization: Bearer <JWT>" \
  https://api.yourdomain.com/api/whatsapp/status/
```

### Enable/disable outbound messaging

```bash
# Disable (Manager or Super Admin)
curl -X POST https://api.yourdomain.com/api/whatsapp/config/outbound/ \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false, "reason": "Maintenance"}'

# Enable
curl -X POST https://api.yourdomain.com/api/whatsapp/config/outbound/ \
  -d '{"enabled": true, "reason": "Maintenance complete"}'
```

### Reset Safety Guard stop (Super Admin only)

```bash
curl -X POST https://api.yourdomain.com/api/whatsapp/config/safety-reset/ \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Investigated 5 send failures: all were transient network errors. WhatsApp account is healthy."}'
```

---

## Backup Operations

### Run a backup

```bash
bash scripts/backup.sh
```

Backups are stored in `./backups/` as AES-256 encrypted archives.

### Automated backup (recommended)

Add to crontab:

```bash
sudo crontab -e
```

```cron
0 3 * * * cd /opt/market-intel-whatsapp-service && \
  bash scripts/backup.sh --passphrase-file /root/.backup-passphrase >> /var/log/whatsapp-backup.log 2>&1
```

### Restore from backup

```bash
bash scripts/restore.sh backups/backup-YYYYMMDD-HHMMSS.tar.gz.enc
```

---

## Log Management

### View live logs

```bash
# All services
docker compose logs -f

# WAHA only
docker compose logs -f waha

# Nginx only
docker compose logs -f nginx
```

### Check recent errors

```bash
docker compose logs waha --since 1h | grep -i error
docker compose logs nginx --since 1h | grep -E "50[0-9]"
```

### Log retention

Docker JSON file logs are capped per `docker-compose.yml`:
- WAHA: 50 MB × 5 files = 250 MB max
- Nginx: 20 MB × 5 files = 100 MB max

---

## Monitoring Alerts

Configure these alerts in your monitoring system:

| Alert | Condition | Severity |
|-------|-----------|---------|
| WAHA container stopped | Container state != running | CRITICAL |
| WhatsApp disconnected | Session status = DISCONNECTED | HIGH |
| Safety stop triggered | `safety_stop_active = true` | HIGH |
| Consecutive failures | `consecutive_failed_sends >= 3` | MEDIUM |
| TLS cert expiry | `days_remaining < 14` | HIGH |
| Disk usage | `> 85%` | MEDIUM |
| RAM available | `< 512 MB` | MEDIUM |
| Webhook errors | `processing_status = FAILED` count | MEDIUM |

---

## Resource Monitoring

### Check resource usage

```bash
# Container resource stats
docker stats --no-stream

# System overview
free -h
df -h
top -bn1 | head -20
```

### Expected resource usage

| Resource | WAHA (NOWEB) | Nginx | Total |
|---------|-------------|-------|-------|
| RAM | 200-400 MB | 30-50 MB | ~500 MB |
| CPU (idle) | 0-2% | 0-1% | ~3% |
| CPU (active) | 5-15% | 1-5% | ~20% |
| Disk | 100-500 MB sessions | < 50 MB logs | ~1 GB |

---

## Secret Rotation

### Rotate API key (recommended every 90 days)

```bash
bash scripts/rotate-secrets.sh api-key
```

1. Script generates new plaintext key and hash.
2. Update this service `.env` with new hash.
3. Update Django `.env` with new plaintext key.
4. Restart both services.

### Rotate webhook secret

```bash
bash scripts/rotate-secrets.sh webhook-secret
```

1. Script generates new secret.
2. Update BOTH `.env` files (this service + Django).
3. Restart both services.

### Rotate SSH keys

```bash
# 1. Generate new key pair (on your local machine)
ssh-keygen -t ed25519 -f ~/.ssh/whatsapp-service-new

# 2. Add new public key to Oracle VM
cat ~/.ssh/whatsapp-service-new.pub | ssh ubuntu@<VM-IP> 'cat >> ~/.ssh/authorized_keys'

# 3. Verify new key works
ssh -i ~/.ssh/whatsapp-service-new ubuntu@<VM-IP> 'echo ok'

# 4. Remove old public key from authorized_keys on the VM
# 5. Update Oracle Console SSH keys if stored there
```

---

## Update Procedure

### Safe update workflow

```bash
# 1. Backup
bash scripts/backup.sh

# 2. Note current version
CURRENT_VERSION=$(docker compose images --format json | jq -r '.[] | select(.Service=="waha") | .Tag')
echo "Current WAHA version: $CURRENT_VERSION"

# 3. Pull new image
docker compose pull

# 4. New version
NEW_VERSION=$(docker compose images --format json | jq -r '.[] | select(.Service=="waha") | .Tag')
echo "New WAHA version: $NEW_VERSION"

# 5. Update
docker compose up -d waha

# 6. Health check
sleep 30
bash scripts/health-check.sh

# 7. Verify WhatsApp session is still connected
curl http://localhost:3000/health
```

### Rollback

```bash
# 1. Edit docker-compose.yml — pin image to known-good version
nano docker-compose.yml
# Change: image: devlikeapro/waha:noweb-arm
# To:     image: devlikeapro/waha:noweb-arm@sha256:<digest>

# 2. Restart with pinned version
docker compose up -d waha

# 3. Verify
bash scripts/health-check.sh
```

---

## Troubleshooting Quick Reference

| Symptom | First Check | Command |
|---------|-------------|---------|
| WAHA not responding | Container running? | `docker compose ps` |
| Session disconnected | WAHA logs | `docker compose logs waha --since 30m` |
| Webhook not received | Nginx logs | `docker compose logs nginx --since 30m` |
| Sends blocked | Safety stop active? | `curl .../api/whatsapp/status/` |
| HTTPS not working | Cert expired? | `bash scripts/health-check.sh` |
| High memory | Docker stats | `docker stats --no-stream` |

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for detailed solutions.
