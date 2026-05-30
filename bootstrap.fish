#!/usr/bin/env fish
# Agency stack bootstrap — run once after cloning to ~/agency/
# Requires: Podman rootless, fish shell, systemd --user

set AGENCY_DIR (dirname (realpath (status filename)))
set QUADLET_DIR ~/.config/containers/systemd
set ENV_FILE $AGENCY_DIR/.env

# ── Pre-flight ───────────────────────────────────────────────────────────────
if not test -f $ENV_FILE
    echo "[error] $ENV_FILE not found. Copy .env.template → .env and fill in secrets."
    exit 1
end

echo "==> Checking Podman..."
if not command -q podman
    echo "[error] podman not found"
    exit 1
end

# ── Pull standard images ─────────────────────────────────────────────────────
echo "==> Pulling standard images..."
for image in \
    docker.io/postgres:16-alpine \
    docker.io/n8nio/n8n:latest \
    docker.io/calcom/cal.com:latest \
    docker.io/cloudflare/cloudflared:latest \
    docker.io/nginx:alpine
    echo "    pulling $image"
    podman pull $image
end

# ── Build custom images ──────────────────────────────────────────────────────
echo "==> Building custom images..."

for svc in scraper outreach delivery billing dashboard landing
    echo "    building agency-$svc..."
    podman build \
        --tag localhost/agency-$svc:latest \
        --file $AGENCY_DIR/$svc/Containerfile \
        $AGENCY_DIR/$svc
    or begin
        echo "[error] Build failed for $svc"
        exit 1
    end
end

# ── Create named volumes ─────────────────────────────────────────────────────
echo "==> Creating named volumes..."
for vol in agency-data agency-n8n-data agency-postgres-data
    if not podman volume inspect $vol &>/dev/null
        podman volume create $vol
        echo "    created $vol"
    else
        echo "    $vol already exists, skipping"
    end
end

# ── Seed data directory structure ────────────────────────────────────────────
echo "==> Seeding data volume..."
set DATA_MOUNT (podman volume inspect agency-data --format '{{.Mountpoint}}')
for subdir in leads deliverables logs
    mkdir -p $DATA_MOUNT/$subdir
end

# ── Copy targets.yaml into scraper config ────────────────────────────────────
if not test -d $AGENCY_DIR/scraper
    mkdir -p $AGENCY_DIR/scraper
end
if not test -f $AGENCY_DIR/scraper/targets.yaml
    cp $AGENCY_DIR/targets.yaml $AGENCY_DIR/scraper/targets.yaml
end

# ── Install Quadlet unit files ───────────────────────────────────────────────
echo "==> Installing Quadlet unit files..."
mkdir -p $QUADLET_DIR
cp $AGENCY_DIR/quadlets/*.pod $QUADLET_DIR/
cp $AGENCY_DIR/quadlets/*.container $QUADLET_DIR/

# ── Provision Cloudflare tunnel ───────────────────────────────────────────────
echo "==> Provisioning Cloudflare tunnel..."
if command -q jq
    bash $AGENCY_DIR/scripts/tunnel-setup.sh
else
    echo "    [!] jq not found — skipping tunnel-setup.sh"
    echo "    Install jq then run:  bash ~/agency/scripts/tunnel-setup.sh"
end

# ── Reload systemd user daemon ───────────────────────────────────────────────
echo "==> Reloading systemd user daemon..."
systemctl --user daemon-reload

# ── Enable and start services ────────────────────────────────────────────────
echo "==> Enabling and starting agency pod..."
systemctl --user enable --now agency-pod-pod.service

echo ""
echo "==> Waiting for pod to be ready (15s)..."
sleep 15

# ── Start containers ─────────────────────────────────────────────────────────
for svc in \
    agency-postgres \
    agency-n8n \
    agency-calcom \
    agency-scraper \
    agency-outreach \
    agency-delivery \
    agency-billing \
    agency-dashboard \
    agency-landing \
    agency-tunnel
    echo "    starting $svc..."
    systemctl --user enable --now $svc.service
end

echo ""
echo "==> Status:"
systemctl --user status agency-pod-pod.service --no-pager -l

echo ""
echo "╔════════════════════════════════════════════════════════╗"
echo "║  Agency stack is up                                    ║"
echo "╠════════════════════════════════════════════════════════╣"
echo "║  Landing      http://localhost:8090  (ringcatch.io)     ║"
echo "║  n8n          http://localhost:5678                    ║"
echo "║  Cal.com      http://localhost:3000                    ║"
echo "║  Dashboard    http://localhost:8501                    ║"
echo "║  Billing wh   http://localhost:8082/stripe-webhook     ║"
echo "╠════════════════════════════════════════════════════════╣"
echo "║  NEXT: Fill real secrets in .env (DB, Stripe, Brevo)  ║"
echo "║  NEXT: Add CLOUDFLARE_API_TOKEN to .env then run:     ║"
echo "║    bash ~/agency/scripts/tunnel-setup.sh              ║"
echo "║  NEXT: Import n8n/workflows/*.json into n8n UI         ║"
echo "║  NEXT: Complete Cal.com setup at localhost:3000        ║"
echo "║  NEXT: Update booking hrefs in landing/index.html      ║"
echo "╚════════════════════════════════════════════════════════╝"
