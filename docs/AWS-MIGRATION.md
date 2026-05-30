# RingCatch Agency — AWS Hybrid Migration Plan

**Status:** Planning
**Target:** Move public-facing sales-critical services to AWS free tier EC2
**Internal services:** Stay on archbox via Tailscale
**Future path:** EKS-compatible manifests from day one

---

## Architecture Overview

```
BEFORE:
  Visitor → Cloudflare → archbox (Tunnel) → agency-pod (all containers)

AFTER:
  Visitor → Cloudflare → EC2 (Tunnel) → [landing, outreach, billing, postgres]
                                              ↕ Tailscale
                                         archbox → [orchestrator, scraper, n8n,
                                                    discord, sales, cfo, etc.]
                                              ↕ Tailscale
                                         MikePC → Ollama (100.97.45.57:11434)
```

---

## 1. AWS Account Setup & Security Hardening

### Account Setup

```bash
# After creating AWS account at console.aws.amazon.com:

# 1. Enable MFA on root account immediately
# Console → Account → Security credentials → MFA → Add MFA device

# 2. Create admin IAM user (never use root for day-to-day)
# IAM → Users → Create user → Attach: AdministratorAccess
# Enable console access + programmatic access

# 3. Set billing alerts (free tier protection)
# Billing → Budgets → Create budget
# Type: Cost budget, Amount: $5/month, Alert at 80%
# Email: molszewski423@gmail.com
```

### Security Hardening Checklist

```
[ ] MFA enabled on root account
[ ] Root account has no access keys
[ ] IAM admin user created (not root)
[ ] MFA enabled on IAM user
[ ] Billing alert set at $5/month
[ ] Default VPC security group reviewed
[ ] CloudTrail enabled (free tier: management events)
[ ] S3 public access block enabled account-wide
```

### Security Group for EC2

Create security group `ringcatch-sg`:

```
Inbound:
  SSH     TCP 22      Your home IP only (not 0.0.0.0/0)
  HTTP    TCP 80      0.0.0.0/0   (Cloudflare needs this initially)
  HTTPS   TCP 443     0.0.0.0/0
  Custom  TCP 41641   0.0.0.0/0   (Tailscale UDP — add UDP rule too)

Outbound:
  All traffic  0.0.0.0/0  (default — keep)
```

**After Cloudflare Tunnel is confirmed working:** Remove HTTP/HTTPS inbound rules.
Tunnel uses outbound-only connections — no open inbound ports needed.

---

## 2. EC2 Instance Setup

### Launch Instance

```bash
# Via AWS Console or CLI:
# AMI: Ubuntu 22.04 LTS (ami-0c7217cdde317cfec in us-east-1)
# Instance type: t3.small (2 vCPU, 2GB RAM)
# Key pair: Create new → ringcatch-key → Download .pem
# Security group: ringcatch-sg (from above)
# Storage: 20GB gp3 EBS (root) + 10GB gp3 EBS (data volume)
#
# Note: t3.small is NOT free tier — t2.micro/t3.micro is.
# t3.small recommended because 2GB RAM is tight for postgres + outreach.
# Cost: ~$17/month. If budget is critical, start with t3.micro and monitor.
```

### Initial Server Setup

```bash
# SSH in (first time)
chmod 400 ~/ringcatch-key.pem
ssh -i ~/ringcatch-key.pem ubuntu@<EC2_PUBLIC_IP>

# Update
sudo apt-get update && sudo apt-get upgrade -y

# Install Podman
sudo apt-get install -y podman podman-compose uidmap slirp4netns

# Install useful tools
sudo apt-get install -y curl wget git jq htop

# Create app user (don't run as ubuntu)
sudo useradd -m -s /bin/bash ringcatch
sudo loginctl enable-linger ringcatch

# Mount data EBS volume
sudo mkfs.ext4 /dev/xvdb          # format data volume (first time only)
sudo mkdir -p /data
sudo mount /dev/xvdb /data
sudo chown ringcatch:ringcatch /data

# Persist mount across reboots
echo '/dev/xvdb /data ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab

# Switch to ringcatch user for all app work
sudo su - ringcatch
```

### SSH Config (add to ~/.ssh/config on MikeInspiron)

```
Host ringcatch-aws
    HostName <EC2_PUBLIC_IP>
    User ubuntu
    IdentityFile ~/.ssh/ringcatch-key.pem
```

---

## 3. Podman Compose — EC2 Services

Save as `/home/ringcatch/agency/docker-compose.yml` on EC2:

```yaml
version: "3.9"

networks:
  agency:
    driver: bridge

volumes:
  postgres-data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /data/postgres

services:

  # ── PostgreSQL 16 ──────────────────────────────────────────────────────────
  postgres:
    image: docker.io/library/postgres:16-alpine
    container_name: agency-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: agency
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: agency
    volumes:
      - postgres-data:/var/lib/postgresql/data
    networks:
      - agency
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U agency"]
      interval: 10s
      timeout: 5s
      retries: 5

  # ── Landing Page (nginx static) ────────────────────────────────────────────
  landing:
    image: localhost/agency-landing:latest
    container_name: agency-landing
    restart: unless-stopped
    build:
      context: ./landing
    networks:
      - agency
    depends_on:
      - outreach
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:80/"]
      interval: 30s
      timeout: 5s
      retries: 3

  # ── Outreach (chatbot + email engine) ──────────────────────────────────────
  outreach:
    image: localhost/agency-outreach:latest
    container_name: agency-outreach
    restart: unless-stopped
    build:
      context: ./outreach
    environment:
      DATABASE_URL: postgresql://agency:${POSTGRES_PASSWORD}@postgres:5432/agency
      GEMINI_API_KEY: ${GEMINI_API_KEY}
      GROQ_API_KEY: ${GROQ_API_KEY}
      BREVO_API_KEY: ${BREVO_API_KEY}
      RESEND_API_KEY: ${RESEND_API_KEY}
      # Tailscale address of MikePC — Ollama fallback
      OLLAMA_BASE_URL: http://100.97.45.57:11434
      # Tailscale address of archbox orchestrator
      ORCHESTRATOR_URL: http://100.96.122.27:8109
    networks:
      - agency
    depends_on:
      postgres:
        condition: service_healthy

  # ── Billing (Stripe webhooks) ──────────────────────────────────────────────
  billing:
    image: localhost/agency-billing:latest
    container_name: agency-billing
    restart: unless-stopped
    build:
      context: ./billing
    environment:
      DATABASE_URL: postgresql://agency:${POSTGRES_PASSWORD}@postgres:5432/agency
      STRIPE_SECRET_KEY: ${STRIPE_SECRET_KEY}
      STRIPE_WEBHOOK_SECRET: ${STRIPE_WEBHOOK_SECRET}
    networks:
      - agency
    depends_on:
      postgres:
        condition: service_healthy

  # ── Cloudflare Tunnel ──────────────────────────────────────────────────────
  cloudflared:
    image: docker.io/cloudflare/cloudflared:latest
    container_name: agency-tunnel
    restart: unless-stopped
    command: tunnel --no-autoupdate run --token ${CLOUDFLARE_TUNNEL_TOKEN}
    networks:
      - agency
    depends_on:
      - landing
      - outreach
      - billing
```

### .env file on EC2 (`/home/ringcatch/agency/.env`)

```bash
# Database
POSTGRES_PASSWORD=<strong-random-password>

# LLM APIs
GEMINI_API_KEY=<from archbox agency/.env>
GROQ_API_KEY=<from archbox agency/.env>

# Email
BREVO_API_KEY=<from archbox agency/.env>
RESEND_API_KEY=<from archbox agency/.env>

# Stripe
STRIPE_SECRET_KEY=<from archbox agency/.env>
STRIPE_WEBHOOK_SECRET=<from archbox agency/.env>

# Cloudflare
CLOUDFLARE_TUNNEL_TOKEN=<new tunnel token — see section 4>
```

---

## 4. Cloudflare Tunnel — Reconfiguration for EC2

### Create New Tunnel (Zero Trust Dashboard)

```
1. Go to one.dash.cloudflare.com → Zero Trust → Networks → Tunnels
2. Click "Create tunnel" → Cloudflare Tunnel → Name: ringcatch-aws
3. Copy the tunnel token (set as CLOUDFLARE_TUNNEL_TOKEN in .env)
4. Configure public hostnames:

   ringcatch.io         → http://landing:80
   dashboard.ringcatch.io → http://outreach:8080    # or keep on archbox?
   billing.ringcatch.io → http://billing:8082
```

### Disable Old Tunnel on Archbox

```bash
# On archbox — after EC2 tunnel is confirmed working
systemctl --user stop agency-tunnel
systemctl --user disable agency-tunnel
```

---

## 5. Database: SQLite → PostgreSQL

### Recommendation

**Use PostgreSQL in a container on EC2 (not RDS) initially.**

| Option | Pros | Cons |
|---|---|---|
| SQLite on EBS | Zero migration effort | Write contention with multiple services, no connection pooling |
| PostgreSQL on EC2 | Production-ready, all services can connect | Requires schema migration |
| RDS PostgreSQL | Managed, automated backups | Costs ~$25/month after free tier (12 months) |

**Decision:** PostgreSQL on EC2. Keep RDS as a future upgrade when revenue justifies it.

### Migration: SQLite → PostgreSQL

```bash
# On archbox — export current SQLite data
sqlite3 /data/agency.db .dump > /tmp/agency-dump.sql

# Copy to EC2
scp /tmp/agency-dump.sql ringcatch-aws:/tmp/

# On EC2 — convert SQLite dump to PostgreSQL syntax
# SQLite uses different syntax for some types/sequences
# Tool: pgloader (easiest)
sudo apt-get install -y pgloader

pgloader sqlite:///tmp/agency.db postgresql://agency:<password>@localhost/agency
```

### EBS Backup (automated)

```bash
# Cron on EC2 — daily postgres dump to EBS
0 3 * * * pg_dump postgresql://agency:<password>@localhost/agency \
  > /data/backups/agency-$(date +%Y%m%d).sql
```

---

## 6. Fix Cal.com Hardcoded Tailscale IP

**Current problem:** Cal.com webhook points to `100.97.45.57:8109` (MikePC IP) but orchestrator runs on archbox at port 8109.

**Fix — use environment variable and Tailscale MagicDNS:**

```bash
# In archbox agency/.env
ORCHESTRATOR_URL=http://archbox:8109      # Tailscale MagicDNS name
# OR
ORCHESTRATOR_URL=http://100.96.122.27:8109  # archbox Tailscale IP (more reliable)
```

Update Cal.com webhook URL in the Cal.com dashboard:
```
Old: http://100.97.45.57:8109/webhook/calcom
New: http://100.96.122.27:8109/webhook/calcom
```

Update `agency-calcom.container` quadlet to use env var:
```ini
[Container]
Environment=NEXT_PUBLIC_WEBAPP_URL=https://cal.ringcatch.io
# Cal.com webhook target via Tailscale
Environment=CALENDSO_ENCRYPTION_KEY=${CALENDSO_ENCRYPTION_KEY}
```

In orchestrator code — replace any hardcoded `100.97.45.57` with `os.getenv("ORCHESTRATOR_URL")`.

---

## 7. Tailscale on EC2

EC2 needs to join the Tailscale network to reach archbox (orchestrator, n8n, etc.) and MikePC (Ollama).

```bash
# On EC2
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=ringcatch-aws

# Verify connectivity
tailscale ping archbox       # should reach 100.96.122.27
tailscale ping mikepc        # should reach 100.97.45.57

# Test Ollama from EC2
curl http://100.97.45.57:11434/api/tags

# Test orchestrator from EC2
curl http://100.96.122.27:8109/health
```

Tailscale IP assigned to EC2 will appear in `tailscale status` on archbox.

---

## 8. DNS Cutover Plan (ringcatch.io)

### Before Cutting Over

```
[ ] EC2 running and all 5 services healthy
[ ] Cloudflare Tunnel on EC2 confirmed working (test via curl)
[ ] Database migrated and verified
[ ] Outreach chatbot tested end-to-end
[ ] Billing webhooks tested (Stripe CLI)
[ ] Old archbox tunnel still running (parallel)
```

### Cutover Steps

Cloudflare Tunnel handles DNS automatically — no manual DNS changes needed.
The tunnel's public hostname config IS the DNS routing.

```
1. In Zero Trust dashboard, update tunnel hostnames to point to EC2 tunnel
2. ringcatch.io → EC2 tunnel (was archbox tunnel)
3. Verify: curl https://ringcatch.io — should serve from EC2
4. Monitor for 30 minutes
5. Disable archbox tunnel
```

### Verification

```bash
# Check which tunnel is serving
curl -I https://ringcatch.io | grep cf-ray
# Different CF-Ray IDs before/after means different tunnel

# Check chat works
curl -X POST https://ringcatch.io/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "hello"}'
```

---

## 9. Kubernetes-Ready Notes (EKS Path)

Design decisions now that make EKS migration easier later:

### Use Environment Variables — Not Hardcoded IPs

Every service URL, API key, and hostname must come from env vars. Already done above.

### No Shared Filesystem Between Containers

SQLite over a shared volume breaks in Kubernetes (pods on different nodes can't share EBS).
**PostgreSQL is the right call** — it's the only stateful service, and it gets a PersistentVolumeClaim.

### Health Checks on Every Container

Already added in compose file. In Kubernetes these become `livenessProbe` and `readinessProbe`.

### Resource Limits

Add to compose now — translates directly to Kubernetes `resources.limits`:

```yaml
# Add to each service in compose:
deploy:
  resources:
    limits:
      memory: 512M
      cpus: "0.5"
```

### What Changes for EKS

| Compose | Kubernetes |
|---|---|
| `docker-compose.yml` | `Deployment` + `Service` manifests |
| `.env` file | `Secret` objects (or AWS Secrets Manager via CSI driver) |
| Volume mount | `PersistentVolumeClaim` (EBS via StorageClass) |
| Container health check | `livenessProbe` / `readinessProbe` |
| `depends_on` | `initContainers` or readiness gates |
| Cloudflare Tunnel | Same — runs as a `Deployment` |
| Tailscale | Tailscale operator for Kubernetes |

### Suggested Manifest Structure (for later)

```
k8s/
├── namespace.yaml
├── secrets/           # applied via kubectl, not committed
├── postgres/
│   ├── deployment.yaml
│   ├── service.yaml
│   └── pvc.yaml
├── landing/
│   ├── deployment.yaml
│   └── service.yaml
├── outreach/
│   ├── deployment.yaml
│   └── service.yaml
├── billing/
│   ├── deployment.yaml
│   └── service.yaml
└── cloudflared/
    ├── deployment.yaml
    └── secret.yaml    # tunnel token
```

---

## 10. Rollback Plan

### If EC2 Deploy Fails Before DNS Cutover

Nothing to roll back — archbox tunnel still active, production unaffected.
Fix the issue, redeploy to EC2.

### If Issues Found After DNS Cutover

```bash
# 1. In Zero Trust dashboard — repoint hostnames to archbox tunnel
#    (< 1 minute — no DNS propagation needed, Cloudflare is instant)

# 2. Start archbox tunnel if stopped
ssh archbox
systemctl --user start agency-tunnel

# 3. Verify archbox serving
curl -I https://ringcatch.io
```

### Data Rollback

If database migration caused issues:
```bash
# Archbox SQLite is unchanged — it was never modified during migration
# Old data is still at /data/agency.db on archbox
# Services on archbox reconnect to it immediately on tunnel switch
```

### Runbook Summary

| Scenario | Action | Time to recover |
|---|---|---|
| EC2 instance fails | Switch tunnel back to archbox | < 2 min |
| Postgres data issue | Restore from /data/backups/ on EC2 | 5-10 min |
| Outreach service broken | Roll back to archbox tunnel | < 2 min |
| Stripe webhooks fail | Update webhook URL in Stripe dashboard | 2 min |

---

## Phase Timeline

| Phase | What | When |
|---|---|---|
| 1 | AWS account setup, EC2 launch, Tailscale join | Day 1 (2 hours) |
| 2 | Database migration SQLite → PostgreSQL | Day 1 (1 hour) |
| 3 | Build and test containers on EC2 | Day 2 (2-3 hours) |
| 4 | Cloudflare Tunnel on EC2, parallel test | Day 2 (1 hour) |
| 5 | DNS cutover + monitoring | Day 3 (30 min + watch) |
| 6 | Decommission archbox tunnel services | Day 3+ (after stable) |
| 7 | Kubernetes manifests (post-homelab cluster) | After k3s cluster ready |
