#!/usr/bin/env bash
# QSYSRadio service launcher (systemd ExecStart).
# Every start: sync Python deps if requirements.txt changed, apply pending DB
# migrations, then exec gunicorn. So an update is just:
#   git pull && sudo systemctl restart qsysradio
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${APP_DIR}/venv"
cd "${APP_DIR}"

if [ ! -x "${VENV}/bin/python" ]; then
    echo "[qsysradio] No virtualenv at ${VENV}. Run: sudo ./install.sh" >&2
    exit 1
fi

# ── Python dependencies (only when requirements.txt changed) ────────────────
REQ_STAMP="${VENV}/.requirements.sha256"
REQ_HASH="$(sha256sum requirements.txt | cut -d' ' -f1)"
if [ "$(cat "${REQ_STAMP}" 2>/dev/null || true)" != "${REQ_HASH}" ]; then
    echo "[qsysradio] requirements.txt changed, installing dependencies..."
    "${VENV}/bin/pip" install --quiet -r requirements.txt
    echo "${REQ_HASH}" > "${REQ_STAMP}"
fi

# ── Database migrations (fail loud: no start on a half-migrated schema) ──────
"${VENV}/bin/python" -m server.migrate

# ── Web server ───────────────────────────────────────────────────────────────
PORT="$("${VENV}/bin/python" -m server.config APP_PORT 5410)"
WORKERS="$("${VENV}/bin/python" -m server.config WEB_WORKERS 1)"
THREADS="$("${VENV}/bin/python" -m server.config WEB_THREADS 8)"
echo "[qsysradio] $(cat VERSION) starting on port ${PORT} (${WORKERS}x${THREADS})"

exec "${VENV}/bin/gunicorn" \
    --workers "${WORKERS}" \
    --threads "${THREADS}" \
    --bind "0.0.0.0:${PORT}" \
    --timeout 120 \
    --access-logfile - \
    --chdir "${APP_DIR}" \
    server.app:app
