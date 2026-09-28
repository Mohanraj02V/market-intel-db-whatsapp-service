#!/usr/bin/env bash
# =============================================================================
# scripts/rotate-secrets.sh
# Rotate WAHA API key and/or webhook HMAC secret.
#
# This script guides you through rotating secrets without losing the WhatsApp
# session (no logout required for API key rotation).
#
# USAGE
#   bash scripts/rotate-secrets.sh [api-key|webhook-secret|all]
# =============================================================================

set -euo pipefail

TARGET="${1:-all}"

echo "============================================================"
echo " Market Intel WhatsApp Service — Secret Rotation"
echo "============================================================"
echo ""

[[ -f ".env" ]] || { echo ".env not found. Run from project root."; exit 1; }
[[ -f "docker-compose.yml" ]] || { echo "Run from project root."; exit 1; }

# ---------------------------------------------------------------------------
# Generate a new API key pair
# ---------------------------------------------------------------------------
rotate_api_key() {
    echo "--- Rotating WAHA API Key ---"
    echo ""

    NEW_PLAIN=$(python3 -c "import secrets; print(secrets.token_hex(64))")
    NEW_HASH=$(echo -n "${NEW_PLAIN}" | sha512sum | awk '{print "sha512:" $1}')

    echo "New API key generated."
    echo ""
    echo "IMPORTANT: Save the PLAINTEXT key in Django .env as WHATSAPP_SERVICE_API_KEY:"
    echo ""
    echo "  WHATSAPP_SERVICE_API_KEY=${NEW_PLAIN}"
    echo ""
    echo "The HASH goes into this service .env as WAHA_API_KEY:"
    echo "  WAHA_API_KEY=${NEW_HASH}"
    echo ""

    read -r -p "Update .env with new WAHA_API_KEY? (yes/no): " CONFIRM
    if [[ "${CONFIRM}" == "yes" ]]; then
        # Replace only the WAHA_API_KEY line.
        sed -i "s|^WAHA_API_KEY=.*|WAHA_API_KEY=${NEW_HASH}|" .env
        echo "[OK] .env updated with new WAHA_API_KEY."
        echo "[!!] Remember to update WHATSAPP_SERVICE_API_KEY in Django .env!"
    else
        echo "Skipped .env update. Apply manually."
    fi

    echo ""
    echo "After updating Django .env:"
    echo "  1. Restart Django: systemctl restart gunicorn  (or your process manager)"
    echo "  2. Restart WAHA:   docker compose restart waha"
    echo "  3. Verify:         bash scripts/health-check.sh"
    echo ""
}

# ---------------------------------------------------------------------------
# Generate a new webhook secret
# ---------------------------------------------------------------------------
rotate_webhook_secret() {
    echo "--- Rotating Webhook HMAC Secret ---"
    echo ""

    NEW_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(64))")

    echo "New webhook secret generated."
    echo ""
    echo "This value must be set IDENTICALLY in BOTH:"
    echo "  1. This service's .env as WHATSAPP_WEBHOOK_SECRET"
    echo "  2. Django .env as WHATSAPP_WEBHOOK_SECRET"
    echo ""
    echo "  WHATSAPP_WEBHOOK_SECRET=${NEW_SECRET}"
    echo ""

    read -r -p "Update this service's .env? (yes/no): " CONFIRM
    if [[ "${CONFIRM}" == "yes" ]]; then
        sed -i "s|^WHATSAPP_WEBHOOK_SECRET=.*|WHATSAPP_WEBHOOK_SECRET=${NEW_SECRET}|" .env
        echo "[OK] .env updated with new WHATSAPP_WEBHOOK_SECRET."
        echo "[!!] Remember to update WHATSAPP_WEBHOOK_SECRET in Django .env!"
    else
        echo "Skipped .env update. Apply manually."
    fi

    echo ""
    echo "After updating both .env files:"
    echo "  1. Restart WAHA:   docker compose restart waha"
    echo "  2. Restart Django: systemctl restart gunicorn"
    echo "  3. Test webhook:   bash scripts/health-check.sh"
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
case "${TARGET}" in
    api-key)
        rotate_api_key
        ;;
    webhook-secret)
        rotate_webhook_secret
        ;;
    all)
        rotate_api_key
        rotate_webhook_secret
        ;;
    *)
        echo "Usage: $0 [api-key|webhook-secret|all]"
        exit 1
        ;;
esac

echo "============================================================"
echo "Secret rotation guidance complete."
echo "============================================================"
