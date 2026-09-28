#!/usr/bin/env bash
# =============================================================================
# scripts/restore.sh
# Restore from an encrypted backup created by backup.sh.
#
# USAGE
#   bash scripts/restore.sh <path-to-backup.tar.gz.enc>
#   bash scripts/restore.sh <path-to-backup.tar.gz.enc> --passphrase-file /secure/backup.key
#
# WARNING
# -------
# Restoring session data will REPLACE the current WhatsApp session.
# Stop the WAHA container before restoring:
#   docker compose stop waha
# Then restore, then start:
#   docker compose start waha
# =============================================================================

set -euo pipefail

BACKUP_FILE="${1:-}"
PASSPHRASE_FILE=""
RESTORE_TARGET="."

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
shift || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --passphrase-file)
            PASSPHRASE_FILE="$2"
            shift 2
            ;;
        --target)
            RESTORE_TARGET="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1"
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------
[[ -n "${BACKUP_FILE}" ]] || { echo "Usage: $0 <backup.tar.gz.enc>"; exit 1; }
[[ -f "${BACKUP_FILE}" ]] || { echo "Backup file not found: ${BACKUP_FILE}"; exit 1; }
[[ -f "docker-compose.yml" ]] || { echo "Run from project root."; exit 1; }

echo "[RESTORE] Backup file: ${BACKUP_FILE}"
echo "[RESTORE] Target:      ${RESTORE_TARGET}"
echo ""
echo "WARNING: This will OVERWRITE existing session data."
echo "         Stop WAHA before proceeding: docker compose stop waha"
echo ""
read -r -p "Type 'restore' to confirm: " CONFIRM
[[ "${CONFIRM}" == "restore" ]] || { echo "Aborted."; exit 0; }

# ---------------------------------------------------------------------------
# Stop WAHA to prevent file conflicts
# ---------------------------------------------------------------------------
echo "[RESTORE] Stopping WAHA..."
docker compose stop waha || true

# ---------------------------------------------------------------------------
# Decrypt
# ---------------------------------------------------------------------------
TEMP_ARCHIVE="/tmp/restore-$(date +%s).tar.gz"
trap 'rm -f "${TEMP_ARCHIVE}"' EXIT

echo "[RESTORE] Decrypting..."

if [[ -n "${PASSPHRASE_FILE}" ]]; then
    openssl enc -aes-256-cbc \
        -pbkdf2 \
        -iter 100000 \
        -d \
        -in "${BACKUP_FILE}" \
        -out "${TEMP_ARCHIVE}" \
        -pass file:"${PASSPHRASE_FILE}"
else
    echo "[RESTORE] Enter backup decryption passphrase:"
    openssl enc -aes-256-cbc \
        -pbkdf2 \
        -iter 100000 \
        -d \
        -in "${BACKUP_FILE}" \
        -out "${TEMP_ARCHIVE}"
fi

echo "[RESTORE] Decryption successful."

# ---------------------------------------------------------------------------
# List contents before extracting
# ---------------------------------------------------------------------------
echo "[RESTORE] Archive contents:"
tar -tzf "${TEMP_ARCHIVE}" | head -20
echo ""

read -r -p "Proceed with extraction? (yes/no): " EXTRACT_CONFIRM
[[ "${EXTRACT_CONFIRM}" == "yes" ]] || { echo "Aborted."; exit 0; }

# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------
echo "[RESTORE] Extracting to ${RESTORE_TARGET}..."

tar \
    --extract \
    --gzip \
    --file="${TEMP_ARCHIVE}" \
    --directory="${RESTORE_TARGET}" \
    --overwrite

# Restore permissions on session directory.
chmod 700 "${RESTORE_TARGET}/data/sessions" 2>/dev/null || true

echo "[RESTORE] Extraction complete."

# ---------------------------------------------------------------------------
# Restart WAHA
# ---------------------------------------------------------------------------
echo "[RESTORE] Starting WAHA..."
docker compose start waha

echo ""
echo "[RESTORE] Restore complete."
echo "[RESTORE] Verify health: bash scripts/health-check.sh"
echo "[RESTORE] If WhatsApp session is not reconnected, you may need to scan a new QR code."
