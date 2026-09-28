# TROUBLESHOOTING.md — Market Intel WhatsApp Service

## Common Issues

---

### WAHA container won't start

**Symptoms:**
- `docker compose ps` shows WAHA as `Exited`
- `docker compose logs waha` shows startup error

**Checks:**
```bash
# View full startup error
docker compose logs waha --tail=50

# Check if port 3000 is already in use
ss -tlnp | grep 3000

# Check .env variables
grep WAHA_API_KEY .env
```

**Common causes:**
1. `WAHA_API_KEY` not set or invalid format — must be `sha512:<64-char-hex>`
2. Missing session directory — run `mkdir -p ./data/sessions && chmod 700 ./data/sessions`
3. Insufficient memory — check `free -h`; WAHA needs at least 512 MB free

---

### WhatsApp session disconnects

**Symptoms:**
- Session status shows `DISCONNECTED`
- Messages fail with "WhatsApp is not connected"

**Cause:**
WhatsApp sessions can disconnect due to:
- Phone being offline for an extended period
- WhatsApp app forced logout of linked devices
- Network interruption during authentication
- WAHA container restart (session should auto-reconnect)

**Solution:**
```bash
# Check if session data still exists
ls -la ./data/sessions/

# Restart WAHA (session will reconnect if data is intact)
docker compose restart waha

# If still disconnected after 60 seconds, re-scan QR:
# 1. Open Market Intel as Super Admin
# 2. Start Session → QR appears
# 3. Scan with WhatsApp
```

---

### QR code not appearing

**Symptoms:**
- `/api/whatsapp/qr/` returns error or empty
- Dashboard shows no QR

**Checks:**
```bash
docker compose logs waha | grep -i qr
curl http://localhost:3000/api/sessions/default -H "X-Api-Key: YOUR_PLAINTEXT_KEY"
```

**Solution:**
```bash
# Force-restart the session
docker compose restart waha

# Wait 60 seconds for initialization
sleep 60

# Check session state
curl http://localhost:3000/api/sessions -H "X-Api-Key: YOUR_PLAINTEXT_KEY"
```

---

### HTTPS not working / SSL error

**Symptoms:**
- `curl: (60) SSL certificate problem`
- Browser shows "Certificate Error"

**Checks:**
```bash
# Check if certificate files exist
ls -la ./data/certs/live/whatsapp.yourdomain.com/

# Check certificate expiry
openssl x509 -in ./data/certs/live/whatsapp.yourdomain.com/fullchain.pem -noout -dates

# Check Nginx configuration
docker compose exec nginx nginx -t
```

**Solutions:**

*Certificate expired:*
```bash
docker compose --profile certbot run --rm certbot
docker compose exec nginx nginx -s reload
```

*Certificate files missing:*
```bash
# Re-run certbot
docker compose --profile certbot run --rm certbot

# Or use standalone mode (stop nginx first)
docker compose stop nginx
sudo certbot certonly --standalone -d whatsapp.yourdomain.com
# Copy certs to ./data/certs/
docker compose start nginx
```

*Nginx config error:*
```bash
docker compose exec nginx nginx -t
# Fix the reported error, then:
docker compose restart nginx
```

---

### Webhook events not being received by Django

**Symptoms:**
- WAHA sends events but `WhatsAppEvent` table stays empty
- Webhook endpoint returns non-200

**Checks:**
```bash
# Test webhook endpoint manually
curl -X POST https://api.yourdomain.com/api/whatsapp/webhook/ \
  -H "Content-Type: application/json" \
  -d '{"test": true}'
# Expected: 401 (no valid HMAC)

# Check Django webhook URL in .env
grep DJANGO_WEBHOOK_URL .env

# Check WAHA webhook config
docker compose exec waha wget -qO- http://127.0.0.1:3000/api/sessions/default
```

**Solutions:**

*Wrong webhook URL:*
- Verify `DJANGO_WEBHOOK_URL` in `.env`
- Ensure Django is accessible from the Oracle VM
- Test connectivity: `curl <DJANGO_WEBHOOK_URL>` from the VM

*HMAC mismatch:*
- Verify `WHATSAPP_WEBHOOK_SECRET` is identical in both `.env` files
- After updating, restart both WAHA and Django

---

### Safety Guard triggered automatically

**Symptoms:**
- Sends fail with "Safety stop active"
- `WhatsAppSafetyEvent` shows `SAFETY_STOP_TRIGGERED`

**Investigation:**
```bash
# Via Django admin API:
curl -H "Authorization: Bearer <JWT>" \
  https://api.yourdomain.com/api/whatsapp/safety-events/

# Check consecutive failures:
curl -H "Authorization: Bearer <JWT>" \
  https://api.yourdomain.com/api/whatsapp/status/
```

**Resolution (Super Admin required):**

1. Review the `safety_stop_reason` — understand why it triggered.
2. Check WAHA logs for the underlying WhatsApp errors:
   ```bash
   docker compose logs waha --since 2h | grep -i error
   ```
3. Verify the WhatsApp session is healthy.
4. If satisfied that the issue is resolved, reset the safety stop:
   ```bash
   curl -X POST https://api.yourdomain.com/api/whatsapp/config/safety-reset/ \
     -H "Authorization: Bearer <JWT>" \
     -H "Content-Type: application/json" \
     -d '{"reason": "Investigated: issue was X. Resolved by Y."}'
   ```

**DO NOT reset the safety stop without investigating the root cause.**

---

### High memory usage

**Symptoms:**
- `docker stats` shows WAHA using > 2 GB RAM
- VM becomes unresponsive

**Checks:**
```bash
docker stats --no-stream
free -h
docker compose logs waha --tail=20
```

**Solutions:**
```bash
# Restart WAHA (clears in-memory state)
docker compose restart waha

# If OOM is recurring, adjust limits in docker-compose.yml:
# deploy.resources.limits.memory: "2G"  → "2.5G"
```

---

### "WhatsApp service unavailable" from Django

**Symptoms:**
- Django returns 503 for all WhatsApp endpoints
- Logs show connection refused

**Checks:**
```bash
# Can Django reach the Oracle VM?
curl https://whatsapp.yourdomain.com/health

# Is WAHA running?
docker compose ps

# Check WHATSAPP_SERVICE_URL in Django .env
grep WHATSAPP_SERVICE_URL /path/to/django/.env
```

**Solutions:**
1. Restart WAHA: `docker compose restart waha`
2. Check Oracle Security List — port 443 must be open
3. Check UFW: `sudo ufw status`
4. Verify DNS: `nslookup whatsapp.yourdomain.com`

---

### Disk full

**Symptoms:**
- Docker logs stop being written
- WAHA fails to store session data

**Investigation:**
```bash
df -h
du -sh ./data/* ./backups/*
docker system df
```

**Solutions:**
```bash
# Clean old Docker artifacts
docker system prune -f

# Remove old backups (keeping last 7 days)
find ./backups -name "*.enc" -mtime +7 -delete

# Clean media files (WAHA auto-cleans based on WAHA_MEDIA_LIFETIME_SECONDS)
# If needed, manually:
find ./data/sessions/media -mtime +1 -delete

# Compact Docker volumes if needed
docker volume prune -f
```

---

## Diagnostic Commands Reference

```bash
# Service status
docker compose ps
docker compose stats --no-stream

# WAHA API (internal, no TLS)
curl http://localhost:3000/health
curl http://localhost:3000/api/sessions \
  -H "X-Api-Key: YOUR_PLAINTEXT_KEY"

# Nginx test
docker compose exec nginx nginx -t
docker compose exec nginx nginx -s reload

# TLS certificate
openssl s_client -connect whatsapp.yourdomain.com:443 -servername whatsapp.yourdomain.com \
  </dev/null 2>/dev/null | openssl x509 -noout -subject -dates

# Full health check
bash scripts/health-check.sh

# Recent WAHA errors
docker compose logs waha --since 1h | grep -iE "error|fail|warn"

# Recent Nginx errors
docker compose logs nginx --since 1h | grep -E "50[0-9]|error"
```
