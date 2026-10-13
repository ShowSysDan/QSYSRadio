#!/usr/bin/env bash
# QSYSRadio server: install / update.
# Clone into your home folder, fill in .env, then:   sudo ./install.sh
# (Without sudo it sets up the venv and database only, no systemd service.)
# After the first install, updates are:  git pull && sudo systemctl restart qsysradio
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="qsysradio"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
ENV_FILE="${APP_DIR}/.env"
VENV="${APP_DIR}/venv"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()  { echo -e "\n${CYAN}==> $*${NC}"; }

# ── Who runs the service: the user who owns the clone ────────────────────────
if [ "$(id -u)" -eq 0 ]; then
    RUN_USER="${SUDO_USER:-}"
    [ -n "$RUN_USER" ] && [ "$RUN_USER" != "root" ] \
        || error "Run as your own user via sudo (sudo ./install.sh), not as root directly."
else
    RUN_USER="$(id -un)"
fi
as_user() { if [ "$(id -u)" -eq 0 ]; then sudo -u "$RUN_USER" -H "$@"; else "$@"; fi; }
info "App directory: ${APP_DIR} (service user: ${RUN_USER})"

# ── Python ───────────────────────────────────────────────────────────────────
step "Checking Python 3..."
command -v python3 &>/dev/null || error "python3 not found. Install Python 3.9+ (and python3-venv)."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || error "Python 3.9+ is required (found $(python3 --version))."
info "Found $(python3 --version)"

step "Virtual environment..."
if [ ! -x "${VENV}/bin/python" ]; then
    as_user python3 -m venv "$VENV" || error "venv failed. On Debian/Ubuntu: sudo apt-get install -y python3-venv"
    info "Created ${VENV}"
fi
as_user "${VENV}/bin/pip" install --quiet --upgrade pip
as_user "${VENV}/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
sha256sum "${APP_DIR}/requirements.txt" | cut -d' ' -f1 | as_user tee "${VENV}/.requirements.sha256" >/dev/null
info "Dependencies installed."

# ── .env ─────────────────────────────────────────────────────────────────────
step "Configuration..."
if [ ! -f "$ENV_FILE" ]; then
    as_user cp "${APP_DIR}/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    error "Created ${ENV_FILE} from .env.example. Fill in PG_DBNAME / PG_USER / PG_PASSWORD, then re-run."
fi
if ! grep -q '^SECRET_KEY=.' "$ENV_FILE"; then
    KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    if grep -q '^SECRET_KEY=[[:space:]]*$' "$ENV_FILE"; then
        sed -i "s/^SECRET_KEY=[[:space:]]*\$/SECRET_KEY=${KEY}/" "$ENV_FILE"
    else
        echo "SECRET_KEY=${KEY}" >> "$ENV_FILE"
    fi
    info "Generated SECRET_KEY"
fi
if ! grep -q '^RADIO_API_TOKEN=.' "$ENV_FILE"; then
    TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    if grep -q '^#\?RADIO_API_TOKEN=[[:space:]]*$' "$ENV_FILE"; then
        sed -i "s/^#\?RADIO_API_TOKEN=[[:space:]]*\$/RADIO_API_TOKEN=${TOKEN}/" "$ENV_FILE"
    else
        echo "RADIO_API_TOKEN=${TOKEN}" >> "$ENV_FILE"
    fi
    info "Generated RADIO_API_TOKEN (put it in radio.lua as CFG.SERVER_TOKEN)"
fi
chmod 600 "$ENV_FILE"
[ "$(id -u)" -eq 0 ] && chown "${RUN_USER}:" "$ENV_FILE"

# ── Database ─────────────────────────────────────────────────────────────────
step "Database..."
(cd "$APP_DIR" && as_user "${VENV}/bin/python" -m server.migrate) \
    || error "Migration failed (see above). Check the PG_* values in ${ENV_FILE}."

# ── systemd (root only) ──────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    step "Done (no systemd service without sudo)."
    echo "  Run in the foreground:  ${APP_DIR}/start.sh"
    echo "  Install the service:    sudo ./install.sh"
    exit 0
fi

step "systemd service..."
chmod +x "${APP_DIR}/start.sh"
cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=QSYSRadio - Q-SYS radio automation server
Documentation=https://github.com/ShowSysDan/QSYSRadio
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=$(id -gn "$RUN_USER")
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/start.sh
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${APP_NAME}

[Install]
WantedBy=multi-user.target
UNIT
info "Wrote ${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable "$APP_NAME" --quiet
systemctl restart "$APP_NAME"
sleep 2
if systemctl is-active --quiet "$APP_NAME"; then
    info "Service is running."
else
    warn "Service may have failed to start: journalctl -u ${APP_NAME} -n 50"
fi

PORT="$(cd "$APP_DIR" && "${VENV}/bin/python" -m server.config APP_PORT 5410)"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo -e "  Health:  ${CYAN}http://${LAN_IP:-localhost}:${PORT}/api/v1/health${NC}"
echo "  Core:    in radio.lua set CFG.SERVER_URL = \"http://${LAN_IP:-<this server>}:${PORT}\""
echo "           and CFG.SERVER_TOKEN = \"$(grep '^RADIO_API_TOKEN=' "$ENV_FILE" | cut -d= -f2-)\""
echo "  Update:  cd ${APP_DIR} && git pull && sudo systemctl restart ${APP_NAME}"
echo "  Logs:    journalctl -u ${APP_NAME} -f"
