#!/usr/bin/env bash
# =============================================================================
# scripts/health-check.sh
# Comprehensive health check for the Market Intel WhatsApp Service.
#
# Exit codes:
#   0 = all checks pass (HEALTHY)
#   1 = one or more checks failed (DEGRADED or UNHEALTHY)
#
# Outputs a JSON summary suitable for monitoring systems.
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
pass()    { echo -e "${GREEN}[PASS]${NC}  $*"; }
fail()    { echo -e "${RED}[FAIL]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }

# ---------------------------------------------------------------------------
# Load .env (for DOMAIN)
# ---------------------------------------------------------------------------
[[ -f ".env" ]] || { fail ".env not found. Run from project root."; exit 1; }

get_env() {
    grep -E "^${1}=" .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'"
}

DOMAIN=$(get_env "DOMAIN")

OVERALL_STATUS="HEALTHY"
ERRORS=()
WARNINGS=()

# ---------------------------------------------------------------------------
# Helper: HTTP check
# ---------------------------------------------------------------------------
http_check() {
    local label="$1"
    local url="$2"
    local expected_code="${3:-200}"
    local timeout="${4:-5}"

    local actual_code
    actual_code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time "${timeout}" "${url}" 2>/dev/null || echo "000")

    if [[ "$actual_code" == "$expected_code" ]]; then
        pass "${label}: HTTP ${actual_code}"
        return 0
    else
        fail "${label}: Expected HTTP ${expected_code}, got ${actual_code} (${url})"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Check 1: Docker daemon
# ---------------------------------------------------------------------------
info "Checking Docker daemon..."
if docker info &>/dev/null; then
    pass "Docker daemon is running."
else
    fail "Docker daemon is NOT running."
    OVERALL_STATUS="UNHEALTHY"
    ERRORS+=("docker_daemon")
fi

# ---------------------------------------------------------------------------
# Check 2: WAHA container status
# ---------------------------------------------------------------------------
info "Checking WAHA container..."
WAHA_RUNNING=$(docker compose ps --format json 2>/dev/null | jq -r '.[] | select(.Service=="waha") | .State' 2>/dev/null || echo "unknown")

if [[ "$WAHA_RUNNING" == "running" ]]; then
    pass "WAHA container: running"
else
    fail "WAHA container: ${WAHA_RUNNING}"
    OVERALL_STATUS="UNHEALTHY"
    ERRORS+=("waha_container")
fi

# ---------------------------------------------------------------------------
# Check 3: WAHA health endpoint (internal)
# ---------------------------------------------------------------------------
info "Checking WAHA health endpoint (internal)..."
if http_check "WAHA /health (internal)" "http://localhost:3000/health" "200" 5; then
    # Parse health response.
    HEALTH_BODY=$(curl -s --max-time 5 "http://localhost:3000/health" 2>/dev/null || echo "{}")
    WAHA_STATE=$(echo "$HEALTH_BODY" | jq -r '.state // "unknown"' 2>/dev/null || echo "unknown")
    info "WAHA state: ${WAHA_STATE}"
else
    OVERALL_STATUS="DEGRADED"
    ERRORS+=("waha_health_endpoint")
fi

# ---------------------------------------------------------------------------
# Check 4: Nginx container
# ---------------------------------------------------------------------------
info "Checking Nginx container..."
NGINX_RUNNING=$(docker compose ps --format json 2>/dev/null | jq -r '.[] | select(.Service=="nginx") | .State' 2>/dev/null || echo "unknown")

if [[ "$NGINX_RUNNING" == "running" ]]; then
    pass "Nginx container: running"
else
    fail "Nginx container: ${NGINX_RUNNING}"
    OVERALL_STATUS="UNHEALTHY"
    ERRORS+=("nginx_container")
fi

# ---------------------------------------------------------------------------
# Check 5: Nginx internal health
# ---------------------------------------------------------------------------
info "Checking Nginx internal health..."
if http_check "Nginx /health-check" "http://localhost/health-check" "200" 5; then
    pass "Nginx serving HTTP."
else
    OVERALL_STATUS="DEGRADED"
    ERRORS+=("nginx_http")
fi

# ---------------------------------------------------------------------------
# Check 6: HTTPS (external)
# ---------------------------------------------------------------------------
if [[ -n "$DOMAIN" && "$DOMAIN" != *"yourdomain"* ]]; then
    info "Checking HTTPS endpoint: https://${DOMAIN}/health ..."
    if http_check "HTTPS /health (external)" "https://${DOMAIN}/health" "200" 10; then
        pass "HTTPS endpoint reachable."
    else
        OVERALL_STATUS="DEGRADED"
        WARNINGS+=("https_endpoint")
    fi
fi

# ---------------------------------------------------------------------------
# Check 7: TLS certificate expiry
# ---------------------------------------------------------------------------
if [[ -n "$DOMAIN" && "$DOMAIN" != *"yourdomain"* ]]; then
    info "Checking TLS certificate expiry..."
    EXPIRY_DATE=$(echo | openssl s_client -servername "${DOMAIN}" -connect "${DOMAIN}:443" 2>/dev/null | \
        openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2 || echo "")

    if [[ -n "$EXPIRY_DATE" ]]; then
        EXPIRY_EPOCH=$(date -d "$EXPIRY_DATE" +%s 2>/dev/null || echo "0")
        NOW_EPOCH=$(date +%s)
        DAYS_LEFT=$(( (EXPIRY_EPOCH - NOW_EPOCH) / 86400 ))

        if [[ $DAYS_LEFT -gt 14 ]]; then
            pass "TLS certificate expires in ${DAYS_LEFT} days (${EXPIRY_DATE})"
        elif [[ $DAYS_LEFT -gt 0 ]]; then
            warn "TLS certificate expires in ${DAYS_LEFT} days — RENEW SOON"
            WARNINGS+=("cert_expiry_soon")
        else
            fail "TLS certificate has EXPIRED"
            OVERALL_STATUS="DEGRADED"
            ERRORS+=("cert_expired")
        fi
    else
        warn "Could not check TLS certificate expiry."
        WARNINGS+=("cert_check_failed")
    fi
fi

# ---------------------------------------------------------------------------
# Check 8: Session data directory
# ---------------------------------------------------------------------------
info "Checking session data directory..."
SESSION_DIR="./data/sessions"

if [[ -d "$SESSION_DIR" ]]; then
    SESSION_PERMS=$(stat -c "%a" "$SESSION_DIR" 2>/dev/null || echo "000")
    if [[ "$SESSION_PERMS" == "700" ]]; then
        pass "Session directory permissions: ${SESSION_PERMS} (correct)"
    else
        warn "Session directory permissions: ${SESSION_PERMS} (expected 700)"
        WARNINGS+=("session_dir_perms")
    fi
else
    fail "Session directory not found: ${SESSION_DIR}"
    OVERALL_STATUS="DEGRADED"
    ERRORS+=("session_dir_missing")
fi

# ---------------------------------------------------------------------------
# Check 9: Disk space
# ---------------------------------------------------------------------------
info "Checking disk space..."
DISK_USE=$(df -h / | awk 'NR==2 {print $5}' | tr -d '%')

if [[ "$DISK_USE" -lt 80 ]]; then
    pass "Disk usage: ${DISK_USE}%"
elif [[ "$DISK_USE" -lt 90 ]]; then
    warn "Disk usage HIGH: ${DISK_USE}% — consider cleanup"
    WARNINGS+=("disk_high")
else
    fail "Disk usage CRITICAL: ${DISK_USE}%"
    OVERALL_STATUS="DEGRADED"
    ERRORS+=("disk_critical")
fi

# ---------------------------------------------------------------------------
# Check 10: Memory
# ---------------------------------------------------------------------------
info "Checking memory..."
MEM_AVAILABLE=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo "0")

if [[ "$MEM_AVAILABLE" -gt 512 ]]; then
    pass "Available memory: ${MEM_AVAILABLE} MB"
elif [[ "$MEM_AVAILABLE" -gt 256 ]]; then
    warn "Available memory LOW: ${MEM_AVAILABLE} MB"
    WARNINGS+=("memory_low")
else
    fail "Available memory CRITICAL: ${MEM_AVAILABLE} MB"
    OVERALL_STATUS="DEGRADED"
    ERRORS+=("memory_critical")
fi

# ---------------------------------------------------------------------------
# Check 11: .env in Git (CRITICAL — secrets must never be committed)
# ---------------------------------------------------------------------------
info "Checking .env is not tracked by Git..."
if git ls-files --error-unmatch .env &>/dev/null 2>&1; then
    fail "CRITICAL: .env is tracked by Git! Remove it immediately."
    OVERALL_STATUS="UNHEALTHY"
    ERRORS+=("env_in_git")
else
    pass ".env is not tracked by Git."
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "======================================"
if [[ "$OVERALL_STATUS" == "HEALTHY" ]]; then
    echo -e "${GREEN}Overall Status: HEALTHY${NC}"
elif [[ "$OVERALL_STATUS" == "DEGRADED" ]]; then
    echo -e "${YELLOW}Overall Status: DEGRADED${NC}"
else
    echo -e "${RED}Overall Status: UNHEALTHY${NC}"
fi
echo "======================================"

if [[ ${#ERRORS[@]} -gt 0 ]]; then
    echo -e "${RED}Errors:${NC}"
    for e in "${ERRORS[@]}"; do echo "  - $e"; done
fi

if [[ ${#WARNINGS[@]} -gt 0 ]]; then
    echo -e "${YELLOW}Warnings:${NC}"
    for w in "${WARNINGS[@]}"; do echo "  - $w"; done
fi

echo ""

[[ "$OVERALL_STATUS" == "HEALTHY" ]] && exit 0 || exit 1
