#!/usr/bin/env bash
# =============================================================================
# scripts/setup.sh
# Oracle Always Free ARM64 Ubuntu — initial server setup
#
# What this script does:
#   1.  Validates environment (Ubuntu, ARM64, root/sudo)
#   2.  Applies system updates and configures unattended security updates
#   3.  Installs Docker + Docker Compose plugin
#   4.  Configures UFW firewall (22, 80, 443 only)
#   5.  Hardens SSH (key-only, no root login, no password auth)
#   6.  Installs and configures fail2ban
#   7.  Creates a least-privilege 'whatsapp' service user
#   8.  Creates required directories with restricted permissions
#   9.  Validates .env file (all required variables present)
#  10.  Issues Let's Encrypt TLS certificate via Certbot
#  11.  Starts Docker services
#  12.  Verifies health
#
# USAGE
# -----
#   sudo bash scripts/setup.sh
#
# Run this script ONCE on a fresh Oracle Ubuntu instance.
# For subsequent updates use scripts/update.sh.
#
# SECURITY WARNINGS
# -----------------
# • DO NOT run this script with secrets inline or as environment variables
#   visible in ps or /proc. Source .env before running if needed.
# • SSH hardening will LOCK password-based SSH. Ensure you have a working
#   SSH key pair configured BEFORE running this script.
# • After UFW is configured only ports 22, 80, 443 are open.
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

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }

# ---------------------------------------------------------------------------
# STEP 0: Validate environment
# ---------------------------------------------------------------------------
info "Validating environment..."

[[ "$EUID" -eq 0 ]] || die "This script must be run as root (sudo)."

# Check OS
if ! grep -qi ubuntu /etc/os-release 2>/dev/null; then
    die "This script targets Ubuntu. Detected: $(cat /etc/os-release 2>/dev/null | grep PRETTY_NAME || echo unknown)"
fi

# Check architecture
ARCH=$(uname -m)
if [[ "$ARCH" != "aarch64" ]]; then
    warn "Expected aarch64 (ARM64) but detected: $ARCH"
    warn "This service is designed for Oracle Free Tier ARM64."
    warn "Proceeding, but WAHA noweb-arm image may not work on $ARCH."
fi

# Verify we are in the project root
[[ -f "docker-compose.yml" ]] || die "Run this script from the market-intel-whatsapp-service project root."
[[ -f ".env" ]] || die ".env file not found. Copy .env.example to .env and fill in all values."

success "Environment validated."

# ---------------------------------------------------------------------------
# Load .env for validation (without executing it).
# ---------------------------------------------------------------------------
# We use grep/awk to read values without executing arbitrary code.
get_env() {
    local key="$1"
    grep -E "^${key}=" .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'"
}

# ---------------------------------------------------------------------------
# STEP 1: Validate required .env variables
# ---------------------------------------------------------------------------
info "Validating .env variables..."

REQUIRED_VARS=(
    DOMAIN
    CERTBOT_EMAIL
    WAHA_API_KEY
    WAHA_DASHBOARD_PASSWORD
    WHATSAPP_WEBHOOK_SECRET
    DJANGO_WEBHOOK_URL
)

MISSING=()
for VAR in "${REQUIRED_VARS[@]}"; do
    VAL=$(get_env "$VAR")
    if [[ -z "$VAL" || "$VAL" == *"REPLACE_WITH"* ]]; then
        MISSING+=("$VAR")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    error "The following required variables are missing or still have placeholder values in .env:"
    for V in "${MISSING[@]}"; do
        error "  - $V"
    done
    die "Please fill in all values in .env before running setup."
fi

DOMAIN=$(get_env "DOMAIN")
CERTBOT_EMAIL=$(get_env "CERTBOT_EMAIL")

success ".env validation passed."

# ---------------------------------------------------------------------------
# STEP 2: System updates
# ---------------------------------------------------------------------------
info "Updating system packages..."

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    curl \
    wget \
    gnupg \
    lsb-release \
    ca-certificates \
    apt-transport-https \
    software-properties-common \
    ufw \
    fail2ban \
    unattended-upgrades \
    apt-listchanges \
    logrotate \
    jq \
    openssl

success "System packages updated."

# ---------------------------------------------------------------------------
# STEP 3: Unattended security updates
# ---------------------------------------------------------------------------
info "Configuring unattended security updates..."

cat > /etc/apt/apt.conf.d/50unattended-upgrades << 'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::Package-Blacklist {};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::MinimalSteps "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Mail "root";
EOF

cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::AutocleanInterval "7";
APT::Periodic::Unattended-Upgrade "1";
EOF

systemctl enable unattended-upgrades
systemctl start unattended-upgrades

success "Unattended security updates configured."

# ---------------------------------------------------------------------------
# STEP 4: Install Docker
# ---------------------------------------------------------------------------
info "Installing Docker..."

if command -v docker &>/dev/null; then
    success "Docker already installed: $(docker --version)"
else
    # Official Docker installation for Ubuntu ARM64
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/ubuntu \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      tee /etc/apt/sources.list.d/docker.list > /dev/null

    apt-get update -qq
    apt-get install -y -qq \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin

    success "Docker installed: $(docker --version)"
fi

# Configure Docker daemon for security.
mkdir -p /etc/docker
cat > /etc/docker/daemon.json << 'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  },
  "no-new-privileges": true,
  "icc": false,
  "live-restore": true,
  "userland-proxy": false
}
EOF

systemctl enable docker
systemctl restart docker

success "Docker configured and started."

# ---------------------------------------------------------------------------
# STEP 5: Create whatsapp service user
# ---------------------------------------------------------------------------
info "Creating 'whatsapp' service user..."

if id "whatsapp" &>/dev/null; then
    success "User 'whatsapp' already exists."
else
    useradd \
        --system \
        --no-create-home \
        --shell /usr/sbin/nologin \
        --comment "Market Intel WhatsApp Service" \
        whatsapp

    success "User 'whatsapp' created."
fi

# Add whatsapp user to docker group so it can run docker commands.
usermod -aG docker whatsapp

success "User 'whatsapp' configured."

# ---------------------------------------------------------------------------
# STEP 6: Create directories with restricted permissions
# ---------------------------------------------------------------------------
info "Creating service directories..."

# Project root should be owned by whatsapp user.
PROJECT_DIR="$(pwd)"

mkdir -p \
    "${PROJECT_DIR}/data/sessions" \
    "${PROJECT_DIR}/data/certs" \
    "${PROJECT_DIR}/data/webroot" \
    "${PROJECT_DIR}/backups" \
    "${PROJECT_DIR}/logs"

# Session data MUST be restricted — WhatsApp auth files are sensitive.
chmod 700 "${PROJECT_DIR}/data/sessions"
chmod 750 "${PROJECT_DIR}/data/certs"
chmod 755 "${PROJECT_DIR}/data/webroot"
chmod 750 "${PROJECT_DIR}/backups"
chmod 750 "${PROJECT_DIR}/logs"

# Set ownership
chown -R whatsapp:whatsapp "${PROJECT_DIR}/data"
chown -R whatsapp:whatsapp "${PROJECT_DIR}/backups"
chown -R whatsapp:whatsapp "${PROJECT_DIR}/logs"

# The .env file should not be world-readable.
chmod 640 "${PROJECT_DIR}/.env"
chown root:whatsapp "${PROJECT_DIR}/.env"

success "Directories created with restricted permissions."

# ---------------------------------------------------------------------------
# STEP 7: UFW Firewall
# ---------------------------------------------------------------------------
info "Configuring UFW firewall..."

# Reset to defaults.
ufw --force reset

# Default policies.
ufw default deny incoming
ufw default allow outgoing

# Allow SSH (restrict source if you have a fixed admin IP).
# Example: ufw allow from 203.0.113.0/24 to any port 22 proto tcp
ufw allow 22/tcp comment "SSH"

# Allow HTTP only for ACME challenge and redirect.
ufw allow 80/tcp comment "HTTP (ACME + redirect)"

# Allow HTTPS.
ufw allow 443/tcp comment "HTTPS"

# Explicitly deny Docker management ports.
ufw deny 2375/tcp comment "Block Docker daemon HTTP"
ufw deny 2376/tcp comment "Block Docker daemon HTTPS"

# Explicitly deny WAHA internal port (Docker already isolates this,
# but belt-and-suspenders is appropriate).
ufw deny 3000/tcp comment "Block WAHA direct access"

# Enable UFW.
ufw --force enable

success "UFW firewall configured."
ufw status verbose

# ---------------------------------------------------------------------------
# STEP 8: SSH hardening
# ---------------------------------------------------------------------------
info "Hardening SSH configuration..."

SSHD_CONFIG="/etc/ssh/sshd_config"

# Backup original config.
cp "${SSHD_CONFIG}" "${SSHD_CONFIG}.backup.$(date +%Y%m%d%H%M%S)"

# Apply hardened settings.
# WARNING: This disables password authentication.
# Ensure your SSH key is authorized before running.
cat > /etc/ssh/sshd_config.d/99-hardening.conf << 'EOF'
# Market Intel WhatsApp Service — SSH Hardening
# Applied by setup.sh

# Disable root login.
PermitRootLogin no

# Key-only authentication.
PasswordAuthentication no
PubkeyAuthentication yes
AuthenticationMethods publickey

# Disable unused auth methods.
ChallengeResponseAuthentication no
KerberosAuthentication no
GSSAPIAuthentication no
UsePAM no

# Limit login attempts.
MaxAuthTries 3
MaxSessions 5

# Set login grace period.
LoginGraceTime 30

# Disable X11 forwarding.
X11Forwarding no

# Disable TCP forwarding (unless needed for VPN/tunnel).
# AllowTcpForwarding no

# Show last login.
PrintLastLog yes

# Disconnect idle sessions after 10 minutes.
ClientAliveInterval 300
ClientAliveCountMax 2

# Use modern key exchange algorithms only.
KexAlgorithms curve25519-sha256,curve25519-sha256@libssh.org,diffie-hellman-group16-sha512,diffie-hellman-group18-sha512

# Strong ciphers only.
Ciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-gcm@openssh.com,aes256-ctr,aes192-ctr,aes128-ctr

# Strong MACs only.
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com,umac-128-etm@openssh.com
EOF

# Test configuration before restarting.
sshd -t && systemctl reload sshd

success "SSH hardened."

# ---------------------------------------------------------------------------
# STEP 9: fail2ban
# ---------------------------------------------------------------------------
info "Configuring fail2ban..."

cat > /etc/fail2ban/jail.d/sshd-custom.conf << 'EOF'
[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
bantime = 3600
findtime = 600
backend = systemd
EOF

systemctl enable fail2ban
systemctl restart fail2ban

success "fail2ban configured."

# ---------------------------------------------------------------------------
# STEP 10: Issue Let's Encrypt TLS certificate
# ---------------------------------------------------------------------------
info "Issuing Let's Encrypt certificate for ${DOMAIN}..."

# Start nginx temporarily on port 80 for ACME webroot challenge.
# We need a temporary nginx just for the challenge.

# Pull Certbot image.
docker compose run --rm --profile certbot certbot || {
    warn "Certbot run failed. Attempting manual Certbot..."

    # Try standalone mode as fallback.
    apt-get install -y -qq certbot

    certbot certonly \
        --standalone \
        --preferred-challenges http \
        --email "${CERTBOT_EMAIL}" \
        --agree-tos \
        --no-eff-email \
        -d "${DOMAIN}" \
        --non-interactive

    # Copy to the volume path.
    mkdir -p "./data/certs"
    rsync -a /etc/letsencrypt/ ./data/certs/

    success "Certificate obtained via standalone Certbot."
}

success "TLS certificate issued."

# ---------------------------------------------------------------------------
# STEP 11: Nginx config — substitute DOMAIN
# ---------------------------------------------------------------------------
info "Updating Nginx configuration with domain: ${DOMAIN}..."

# nginx/whatsapp.conf uses ${NGINX_SERVER_NAME} as a placeholder.
# Replace it with the actual domain.
sed -i "s/\${NGINX_SERVER_NAME}/${DOMAIN}/g" nginx/whatsapp.conf

success "Nginx config updated."

# ---------------------------------------------------------------------------
# STEP 12: Start Docker services
# ---------------------------------------------------------------------------
info "Pulling Docker images..."
docker compose pull

info "Starting Docker services..."
docker compose up -d

success "Docker services started."

# ---------------------------------------------------------------------------
# STEP 13: Verify health
# ---------------------------------------------------------------------------
info "Waiting for services to become healthy (up to 90 seconds)..."

ATTEMPTS=0
MAX_ATTEMPTS=18

until [[ $ATTEMPTS -ge $MAX_ATTEMPTS ]]; do
    WAHA_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:3000/health" 2>/dev/null || echo "000")

    if [[ "$WAHA_HEALTH" == "200" ]]; then
        success "WAHA is healthy."
        break
    fi

    ATTEMPTS=$((ATTEMPTS + 1))
    info "Waiting... (${ATTEMPTS}/${MAX_ATTEMPTS}) WAHA status: ${WAHA_HEALTH}"
    sleep 5
done

if [[ $ATTEMPTS -ge $MAX_ATTEMPTS ]]; then
    warn "WAHA did not become healthy within 90 seconds."
    warn "Check logs: docker compose logs waha"
fi

# Check Nginx.
NGINX_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost/health-check" 2>/dev/null || echo "000")
if [[ "$NGINX_HEALTH" == "200" ]]; then
    success "Nginx is healthy."
else
    warn "Nginx health check returned: ${NGINX_HEALTH}"
fi

# ---------------------------------------------------------------------------
# STEP 14: Configure certificate renewal cron
# ---------------------------------------------------------------------------
info "Configuring certificate renewal cron..."

RENEW_SCRIPT="/etc/cron.d/letsencrypt-renew-whatsapp"
cat > "${RENEW_SCRIPT}" << 'EOF'
# Renew Let's Encrypt certificate twice daily.
# certbot will only actually renew within 30 days of expiry.
0 2,14 * * * root cd /opt/market-intel-whatsapp-service && docker compose --profile certbot run --rm certbot renew --quiet && docker compose exec nginx nginx -s reload >> /var/log/letsencrypt-renew.log 2>&1
EOF

chmod 644 "${RENEW_SCRIPT}"

success "Certificate renewal cron configured."

# ---------------------------------------------------------------------------
# Setup complete
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo -e "${GREEN}Setup complete!${NC}"
echo ""
echo "Service URL:    https://${DOMAIN}/api/"
echo "WAHA Dashboard: https://${DOMAIN}/dashboard/"
echo ""
echo "Next steps:"
echo "  1. Verify: curl https://${DOMAIN}/health"
echo "  2. Configure Django WHATSAPP_SERVICE_URL to point to https://${DOMAIN}"
echo "  3. Start a WhatsApp session and scan the QR code via the Market Intel admin"
echo ""
echo "Logs:           docker compose logs -f"
echo "Health check:   bash scripts/health-check.sh"
echo "============================================================"
