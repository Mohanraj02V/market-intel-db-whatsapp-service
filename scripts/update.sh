#!/usr/bin/env bash
# =============================================================================
# scripts/update.sh
# Safe update procedure for the Market Intel WhatsApp Service.
#
# Run this script to update WAHA or Nginx to a newer version.
# Always backs up before updating.
#
# USAGE
#   bash scripts/update.sh [--skip-backup]
# =============================================================================

set -euo pipefail

SKIP_BACKUP=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-backup)
            SKIP_BACKUP=true
            shift
            ;;
        *)
            echo "Unknown argument: $1"
            exit 1
            ;;
    esac
done

[[ -f "docker-compose.yml" ]] || { echo "Run from project root."; exit 1; }
[[ -f ".env" ]] || { echo ".env not found."; exit 1; }

echo "============================================================"
echo " Market Intel WhatsApp Service — Update"
echo "============================================================"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Pre-update health check
# ---------------------------------------------------------------------------
echo "[UPDATE] Running pre-update health check..."
if ! bash scripts/health-check.sh; then
    echo "[UPDATE] WARNING: Pre-update health check failed."
    echo "[UPDATE] Proceeding anyway — update may fix the issue."
fi

# ---------------------------------------------------------------------------
# Step 2: Record current versions
# ---------------------------------------------------------------------------
echo "[UPDATE] Recording current service versions..."
docker compose images 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 3: Backup
# ---------------------------------------------------------------------------
if [[ "${SKIP_BACKUP}" == "true" ]]; then
    echo "[UPDATE] SKIPPING backup (--skip-backup flag set)."
    echo "[UPDATE] WARNING: Proceeding without backup."
else
    echo "[UPDATE] Creating backup before update..."
    bash scripts/backup.sh || {
        echo "[UPDATE] Backup failed. Aborting update."
        echo "[UPDATE] Fix the backup issue first, or use --skip-backup."
        exit 1
    }
fi

# ---------------------------------------------------------------------------
# Step 4: Pull new images
# ---------------------------------------------------------------------------
echo "[UPDATE] Pulling new Docker images..."
docker compose pull

# ---------------------------------------------------------------------------
# Step 5: Apply update (WAHA first, then Nginx)
# ---------------------------------------------------------------------------
echo "[UPDATE] Updating WAHA..."
docker compose up -d --no-deps waha

echo "[UPDATE] Waiting for WAHA to become healthy..."
ATTEMPTS=0
until [[ $ATTEMPTS -ge 12 ]]; do
    WAHA_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/health 2>/dev/null || echo "000")
    if [[ "$WAHA_STATUS" == "200" ]]; then
        echo "[UPDATE] WAHA is healthy."
        break
    fi
    ATTEMPTS=$((ATTEMPTS + 1))
    echo "[UPDATE] Waiting ($ATTEMPTS/12) — WAHA status: $WAHA_STATUS"
    sleep 5
done

if [[ $ATTEMPTS -ge 12 ]]; then
    echo "[UPDATE] ERROR: WAHA did not become healthy after update."
    echo "[UPDATE] Rollback:"
    echo "  1. Find the previous image digest: docker images devlikeapro/waha"
    echo "  2. Pin it in docker-compose.yml"
    echo "  3. docker compose up -d waha"
    echo "  4. bash scripts/health-check.sh"
    exit 1
fi

echo "[UPDATE] Updating Nginx..."
docker compose up -d --no-deps nginx

# ---------------------------------------------------------------------------
# Step 6: Post-update health check
# ---------------------------------------------------------------------------
echo ""
echo "[UPDATE] Running post-update health check..."
sleep 15
bash scripts/health-check.sh && {
    echo ""
    echo "[UPDATE] Update complete. All services healthy."
} || {
    echo ""
    echo "[UPDATE] WARNING: Post-update health check failed."
    echo "[UPDATE] Investigate: docker compose logs -f"
}

echo "============================================================"
