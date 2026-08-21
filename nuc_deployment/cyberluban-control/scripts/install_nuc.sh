#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run: sudo ./scripts/install_nuc.sh"
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-}"

if [[ -z "${SERVICE_USER}" || "${SERVICE_USER}" == "root" ]]; then
  echo "Run this installer with sudo from the normal NUC login account."
  exit 1
fi

SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"
INSTALL_DIR="/opt/cyberluban-control"
ENV_FILE="/etc/cyberluban-control.env"
SERVICE_FILE="/etc/systemd/system/cyberluban-control.service"

apt-get update
apt-get install -y python3 python3-venv python3-pip unzip

install -d -m 0755 "${INSTALL_DIR}"
cp -a "${SOURCE_DIR}/." "${INSTALL_DIR}/"
rm -rf "${INSTALL_DIR}/.venv"

python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${INSTALL_DIR}/.venv/bin/python" -m pip install -r "${INSTALL_DIR}/requirements.txt"

usermod -aG dialout "${SERVICE_USER}"
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${INSTALL_DIR}"

if [[ ! -f "${ENV_FILE}" ]]; then
  CONTROL_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  sed "s/^CONTROL_TOKEN=.*/CONTROL_TOKEN=${CONTROL_TOKEN}/" \
    "${INSTALL_DIR}/.env.example" > "${ENV_FILE}"
  chmod 0600 "${ENV_FILE}"
else
  CONTROL_TOKEN="(kept existing token in ${ENV_FILE})"
fi

sed \
  -e "s/__SERVICE_USER__/${SERVICE_USER}/g" \
  -e "s/__SERVICE_GROUP__/${SERVICE_GROUP}/g" \
  "${INSTALL_DIR}/systemd/cyberluban-control.service.template" > "${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable --now cyberluban-control.service

NUC_IP="$(hostname -I | awk '{print $1}')"
echo
echo "Installation complete."
echo "Control URL: http://${NUC_IP:-NUC_IP}:8000"
echo "Control token: ${CONTROL_TOKEN}"
echo "Save the token now. If this was an update, read it with:"
echo "  sudo grep '^CONTROL_TOKEN=' ${ENV_FILE}"
echo
echo "A logout/reboot may be required before new dialout group membership applies."
