#!/bin/bash
# ═══════════════════════════════════════════════
# PolyEdge v4 — VPS Deploy Script
# ═══════════════════════════════════════════════
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh              # First-time setup + deploy
#   ./deploy.sh update       # Pull latest + restart
#   ./deploy.sh status       # Check all services
#   ./deploy.sh logs         # Tail v4 logs
#   ./deploy.sh stop         # Stop v4 only
# ═══════════════════════════════════════════════

set -e

# ── Config ──
VPS_HOST="178.156.253.21"
VPS_USER="root"
REMOTE_DIR="/root/polyedge"
REPO_URL="https://github.com/carlosjoaquinv-eng/Poly-edge-4.0.git"
BRANCH="main"
V4_PROCESS="polyedge-v4"
V31_PROCESS="polyedge-v3"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

# ── SSH Helper ──
remote() {
    ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no ${VPS_USER}@${VPS_HOST} "$1"
}

remote_check() {
    ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no ${VPS_USER}@${VPS_HOST} "$1" 2>/dev/null
}

# ═══════════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════════

cmd_status() {
    echo -e "\n${CYAN}═══ PolyEdge VPS Status ═══${NC}\n"
    
    info "PM2 processes:"
    remote "pm2 list" 2>/dev/null || warn "PM2 not running"
    
    echo ""
    info "Disk usage:"
    remote "df -h / | tail -1"
    
    info "Memory:"
    remote "free -h | grep Mem"
    
    info "v4 dashboard:"
    remote "curl -s -o /dev/null -w '%{http_code}' http://localhost:8081/health 2>/dev/null" && echo "" || warn "v4 not responding"
    
    info "v3.1 dashboard:"
    remote "curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/health 2>/dev/null" && echo "" || warn "v3.1 not responding"
}

cmd_logs() {
    info "Tailing v4 logs (Ctrl+C to stop)..."
    remote "pm2 logs ${V4_PROCESS} --lines 50"
}

cmd_stop() {
    info "Stopping v4..."
    remote "pm2 stop ${V4_PROCESS} 2>/dev/null || true"
    log "v4 stopped"
}

cmd_update() {
    echo -e "\n${CYAN}═══ Updating PolyEdge v4 ═══${NC}\n"
    
    info "Pulling latest from GitHub..."
    remote "cd ${REMOTE_DIR}/v4 && git pull origin ${BRANCH}"
    
    info "Restarting v4..."
    remote "pm2 restart ${V4_PROCESS}"
    
    sleep 2
    info "Status after restart:"
    remote "pm2 list | grep polyedge"
    
    log "Update complete"
}

cmd_deploy() {
    echo -e "\n${CYAN}═══════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  PolyEdge v4 — Full Deploy${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════${NC}\n"
    
    # ── Step 1: Test SSH ──
    info "Testing SSH connection..."
    if ! remote_check "echo ok" | grep -q "ok"; then
        err "Cannot connect to ${VPS_HOST}"
        echo "  Make sure your SSH key is set up:"
        echo "  ssh-copy-id ${VPS_USER}@${VPS_HOST}"
        exit 1
    fi
    log "SSH connection OK"
    
    # ── Step 2: Install system deps ──
    info "Installing system dependencies..."
    remote "apt-get update -qq && apt-get install -y -qq python3 python3-pip git curl > /dev/null 2>&1"
    log "System deps OK"
    
    # ── Step 3: Install Python deps ──
    info "Installing Python packages..."
    remote "pip3 install --break-system-packages py-clob-client aiohttp eth-account 2>&1 | tail -3"
    log "Python deps OK"
    
    # ── Step 4: Install PM2 if needed ──
    info "Checking PM2..."
    if ! remote_check "command -v pm2" | grep -q "pm2"; then
        info "Installing PM2..."
        remote "npm install -g pm2 2>/dev/null || (curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs && npm install -g pm2)"
    fi
    log "PM2 OK"
    
    # ── Step 5: Clone or pull repo ──
    info "Setting up repo..."
    remote "mkdir -p ${REMOTE_DIR}"
    
    if remote_check "test -d ${REMOTE_DIR}/v4/.git && echo yes" | grep -q "yes"; then
        info "Repo exists — pulling latest..."
        remote "cd ${REMOTE_DIR}/v4 && git fetch origin && git reset --hard origin/${BRANCH}"
    else
        info "Cloning repo..."
        remote "cd ${REMOTE_DIR} && rm -rf v4 && git clone ${REPO_URL} v4"
    fi
    log "Repo synced"
    
    # ── Step 6: Create data directories ──
    info "Creating data directories..."
    remote "mkdir -p ${REMOTE_DIR}/v4/data_v4 ${REMOTE_DIR}/v4/logs"
    log "Directories OK"
    
    # ── Step 7: Create .env file if not exists ──
    info "Checking .env..."
    if ! remote_check "test -f ${REMOTE_DIR}/v4/.env && echo yes" | grep -q "yes"; then
        warn "Creating .env template — you'll need to fill in credentials"
        remote "cat > ${REMOTE_DIR}/v4/.env << 'ENVFILE'
# PolyEdge v4 Environment
# Fill in your credentials and restart with: pm2 restart polyedge-v4

# Mode
PAPER_MODE=true

# Polymarket CLOB
PRIVATE_KEY=
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=

# Anthropic (Meta Strategist)
ANTHROPIC_API_KEY=

# Telegram Alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Football API (Sniper feeds)
FOOTBALL_API_KEY=

# Tuning
BANKROLL=5000
MM_MAX_MARKETS=5
MM_HALF_SPREAD=0.015
SNIPER_MIN_GAP=3.0
META_INTERVAL_HOURS=12
META_AUTO_APPLY=false
DASHBOARD_PORT=8081
ENVFILE"
        warn ".env created at ${REMOTE_DIR}/v4/.env — edit it with your keys"
    else
        log ".env already exists"
    fi
    
    # ── Step 8: Create PM2 ecosystem ──
    info "Setting up PM2 ecosystem..."
    remote "cat > ${REMOTE_DIR}/v4/ecosystem.config.js << 'PM2FILE'
module.exports = {
  apps: [
    {
      name: 'polyedge-v4',
      script: 'main_v4.py',
      interpreter: 'python3',
      cwd: '${REMOTE_DIR}/v4',
      env_file: '${REMOTE_DIR}/v4/.env',
      max_memory_restart: '500M',
      restart_delay: 5000,
      max_restarts: 10,
      min_uptime: '30s',
      error_file: '${REMOTE_DIR}/v4/logs/v4-error.log',
      out_file: '${REMOTE_DIR}/v4/logs/v4-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      merge_logs: true,
    }
  ]
};
PM2FILE"
    log "PM2 ecosystem configured"
    
    # ── Step 9: Load .env and start ──
    info "Starting PolyEdge v4..."
    remote "cd ${REMOTE_DIR}/v4 && set -a && source .env && set +a && pm2 delete ${V4_PROCESS} 2>/dev/null; pm2 start ecosystem.config.js"
    
    sleep 3
    
    # ── Step 10: Verify ──
    info "Verifying..."
    remote "pm2 list | grep polyedge"
    
    echo ""
    HEALTH=$(remote "curl -s http://localhost:8081/health 2>/dev/null" || echo "not responding yet")
    if echo "$HEALTH" | grep -q "ok"; then
        log "Dashboard responding on :8081"
    else
        warn "Dashboard not responding yet (may need 10-15s to start)"
    fi
    
    # ── Step 11: Save PM2 config ──
    remote "pm2 save 2>/dev/null"
    
    echo -e "\n${GREEN}═══════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Deploy complete!${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  Dashboard:  ${CYAN}http://${VPS_HOST}:8081${NC}"
    echo -e "  v3.1:       ${CYAN}http://${VPS_HOST}:8080${NC}"
    echo -e ""
    echo -e "  ${YELLOW}Next steps:${NC}"
    echo -e "  1. Edit credentials: ${CYAN}ssh ${VPS_USER}@${VPS_HOST} nano ${REMOTE_DIR}/v4/.env${NC}"
    echo -e "  2. Restart after edit: ${CYAN}ssh ${VPS_USER}@${VPS_HOST} 'cd ${REMOTE_DIR}/v4 && set -a && source .env && set +a && pm2 restart ${V4_PROCESS}'${NC}"
    echo -e "  3. Watch logs: ${CYAN}./deploy.sh logs${NC}"
    echo -e "  4. Check status: ${CYAN}./deploy.sh status${NC}"
    echo ""
}

# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

case "${1:-deploy}" in
    deploy)  cmd_deploy ;;
    update)  cmd_update ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    stop)    cmd_stop ;;
    *)
        echo "Usage: $0 {deploy|update|status|logs|stop}"
        exit 1
        ;;
esac
