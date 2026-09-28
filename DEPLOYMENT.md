# DEPLOYMENT.md — Market Intel WhatsApp Service

## Prerequisites

| Requirement | Details |
|------------|---------|
| Cloud Provider | AWS (Free Tier EC2 or existing instance) |
| VM shape | t2.micro or t3.micro (or t4g.small for ARM64) |
| OS | Ubuntu 22.04 LTS |
| Domain | A subdomain resolving to the EC2 public IP |
| GitHub | Repository for continuous deployment |

---

## Step 1: AWS EC2 Provisioning (If not using existing)

### Create the Instance

1. AWS Console → EC2 → Instances → Launch instances
2. Name: `market-intel-whatsapp`
3. AMI: Ubuntu Server 22.04 LTS
4. Instance Type: `t2.micro` or `t3.micro` (Free Tier eligible)
5. Key pair: Select or create a key pair for SSH access
6. Network settings: Allow SSH, HTTP, and HTTPS traffic

### Configure Security Group (AWS Firewall)

Ensure your EC2 Security Group has these Inbound Rules:
| Protocol | Port Range | Source | Description |
|---------|------|--------|-------------|
| TCP | 22 | 0.0.0.0/0 | SSH (restrict to your IP if possible) |
| TCP | 80 | 0.0.0.0/0 | HTTP (ACME + redirect) |
| TCP | 443 | 0.0.0.0/0 | HTTPS |

**All other inbound ports must be closed.** (Do NOT open port 3000 to the public).

---

## Step 2: GitHub Actions Continuous Deployment

This project uses GitHub Actions for continuous deployment. Every time you push to the `main` branch, the service will be automatically updated and restarted on your EC2 instance.

### 1. Push to GitHub
Initialize your local repository and push it to GitHub:
```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/yourusername/market-intel-whatsapp.git
git push -u origin main
```

### 2. Configure GitHub Secrets
Go to your GitHub Repository → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

Add the following secrets:
- `HOST`: Your EC2 public IP or domain name.
- `USERNAME`: Your EC2 username (usually `ubuntu`).
- `SSH_PRIVATE_KEY`: The contents of your `.pem` file or private SSH key to access the EC2 instance.

---

## Step 3: First-time Manual Server Setup

Before GitHub Actions can deploy successfully, you need to prepare the EC2 instance for the first time.

### 1. Connect to the EC2 instance
```bash
ssh -i /path/to/key.pem ubuntu@<EC2-PUBLIC-IP>
```

### 2. Clone the repository manually (First time only)
```bash
sudo mkdir -p /opt/market-intel-whatsapp-service
sudo chown ubuntu:ubuntu /opt/market-intel-whatsapp-service

git clone https://github.com/yourusername/market-intel-whatsapp.git /opt/market-intel-whatsapp-service
cd /opt/market-intel-whatsapp-service
```

### 3. Configure .env
```bash
cp .env.example .env
nano .env
```
Fill in all required values (`DOMAIN`, `WAHA_API_KEY`, etc.). Make sure your domain's DNS is already pointing to the EC2 IP!

### 4. Run Initial Setup
```bash
sudo bash scripts/setup.sh
```
This script will install Docker, configure the firewall, issue SSL certificates via Let's Encrypt, and start the service.

---

## Step 4: Verify Deployment

Once the setup is complete, verify the health of the service:
```bash
# Overall health check
bash scripts/health-check.sh

# Test HTTPS
curl -I https://whatsapp.yourdomain.com/health
```

Expected health output:
```json
{"ok":true,"status":"HEALTHY","session_status":"STOPPED","outbound_enabled":false}
```

From now on, whenever you push changes to the `main` branch, GitHub Actions will automatically pull the changes and restart the service!

---

## Step 5: Configure Django Backend

In your main Django backend, update the `.env` file to point to your new cloud domain:
```env
WHATSAPP_ENABLED=true
WHATSAPP_SERVICE_URL=https://whatsapp.yourdomain.com
WHATSAPP_SERVICE_API_KEY=<your-plaintext-api-key>
WHATSAPP_WEBHOOK_SECRET=<your-webhook-secret>
```

---

## Architecture Decisions

### Why WAHA instead of Baileys directly?
WAHA provides a managed HTTP API abstraction over Baileys, with:
- Built-in webhook support
- Session management
- Dashboard UI for QR scanning
- Stable API versioning

### Why AWS EC2?
- Native integration with your existing AWS infrastructure
- Standard Ubuntu environments
- Robust Security Groups

### Why Nginx in front of WAHA?
- WAHA has no built-in TLS support
- Nginx handles TLS termination, rate limiting, request size limits
- Keeps WAHA off the public Internet entirely

---

## Certificate Renewal

Certificates are renewed automatically via cron. To renew manually:
```bash
docker compose --profile certbot run --rm certbot
docker compose exec nginx nginx -s reload
```
