#!/usr/bin/env bash
# =============================================================================
# scripts/backup.sh
# Encrypted backup of WAHA session data and configuration.
#
# What is backed up:
#   - ./data/sessions/   (WhatsApp session files — encrypted)
#   - ./nginx/           (Nginx config)
#   - .env               (encrypted)
#   - docker-compose.yml
#
# What is NOT backed up:
#   - TLS certificates (managed by Let's Encrypt, easily re-issued)
#   - Temporary media files
#   - Log files
#   - node_modules or build artifacts
#
# Encryption: AES-256 symmetric via OpenSSL.
# The passphrase is prompted interactively and NEVER stored in this script.
#
# USAGE
#   bash scripts/backup.sh
#   bash scripts/backup.sh --passphrase-file /secure/backup.key
#
# RESTORE
#   bash scripts/restore.sh backup-YYYYMMDD-HHMMSS.tar.gz.enc
#
# RETENTION
#   By default the script keeps the 7 most recent backups.
#   Set BACKUP_RETENTION_DAYS to change.
# =============================================================================

set -euo pipefail

BACKUP_DIR="./backups"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_NAME="backup-${TIMESTAMP}.tar.gz"
ENCRYPTED_NAME="${BACKUP_NAME}.enc"
PASSPHRASE_FILE=""

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --passphrase-file)
            PASSPHRASE_FILE="$2"
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
[[ -f "docker-compose.yml" ]] || { echo "Run from project root."; exit 1; }
mkdir -p "${BACKUP_DIR}"

echo "[BACKUP] Starting backup: ${ENCRYPTED_NAME}"
echo "[BACKUP] Retention: ${RETENTION_DAYS} days"

# ---------------------------------------------------------------------------
# Create archive of sensitive data
# ---------------------------------------------------------------------------
echo "[BACKUP] Creating archive..."

# Temporary unencrypted archive — deleted immediately after encryption.
TEMP_ARCHIVE="/tmp/${BACKUP_NAME}"
trap 'rm -f "${TEMP_ARCHIVE}"' EXIT

tar \
    --create \
    --gzip \
    --file="${TEMP_ARCHIVE}" \
    --exclude='./data/sessions/media' \
    --exclude='./data/webroot' \
    ./data/sessions \
    ./nginx \
    .env \
    docker-compose.yml \
    .env.example \
    2>/dev/null || true

echo "[BACKUP] Archive created."

# ---------------------------------------------------------------------------
# Encrypt the archive
# ---------------------------------------------------------------------------
echo "[BACKUP] Encrypting..."

if [[ -n "${PASSPHRASE_FILE}" ]]; then
    openssl enc -aes-256-cbc \
        -pbkdf2 \
        -iter 100000 \
        -salt \
        -in "${TEMP_ARCHIVE}" \
        -out "${BACKUP_DIR}/${ENCRYPTED_NAME}" \
        -pass file:"${PASSPHRASE_FILE}"
else
    echo "[BACKUP] Enter backup encryption passphrase (will be prompted twice):"
    openssl enc -aes-256-cbc \
        -pbkdf2 \
        -iter 100000 \
        -salt \
        -in "${TEMP_ARCHIVE}" \
        -out "${BACKUP_DIR}/${ENCRYPTED_NAME}"
fi

# Set restrictive permissions on the encrypted backup.
chmod 600 "${BACKUP_DIR}/${ENCRYPTED_NAME}"

echo "[BACKUP] Encrypted backup saved: ${BACKUP_DIR}/${ENCRYPTED_NAME}"
echo "[BACKUP] Size: $(du -sh "${BACKUP_DIR}/${ENCRYPTED_NAME}" | cut -f1)"

# ---------------------------------------------------------------------------
# Cleanup old backups
# ---------------------------------------------------------------------------
echo "[BACKUP] Cleaning up backups older than ${RETENTION_DAYS} days..."

find "${BACKUP_DIR}" \
    -name "backup-*.tar.gz.enc" \
    -mtime "+${RETENTION_DAYS}" \
    -delete \
    -print | while read -r f; do
        echo "[BACKUP] Deleted old backup: $f"
    done

echo "[BACKUP] Backup complete."

# ---------------------------------------------------------------------------
# Verify backup integrity
# ---------------------------------------------------------------------------
echo "[BACKUP] Verifying backup integrity..."

VERIFY_TEMP="/tmp/backup-verify-${TIMESTAMP}"
mkdir -p "${VERIFY_TEMP}"
trap 'rm -rf "${VERIFY_TEMP}" "${TEMP_ARCHIVE}"' EXIT

if [[ -n "${PASSPHRASE_FILE}" ]]; then
    openssl enc -aes-256-cbc \
        -pbkdf2 \
        -iter 100000 \
        -d \
        -in "${BACKUP_DIR}/${ENCRYPTED_NAME}" \
        -out "${VERIFY_TEMP}/test.tar.gz" \
        -pass file:"${PASSPHRASE_FILE}" 2>/dev/null && \
    tar -tzf "${VERIFY_TEMP}/test.tar.gz" > /dev/null 2>&1 && \
    echo "[BACKUP] Integrity verification: PASSED" || \
    echo "[BACKUP] WARN: Integrity verification failed — verify passphrase."
else
    echo "[BACKUP] Skipping integrity verification (no passphrase file provided)."
    echo "[BACKUP] To verify manually:"
    echo "         openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 -in ${BACKUP_DIR}/${ENCRYPTED_NAME} | tar -tzf -"
fi

echo ""
echo "[BACKUP] RESTORE COMMAND:"
echo "  bash scripts/restore.sh ${BACKUP_DIR}/${ENCRYPTED_NAME}"
