#!/bin/bash

# safe mode
set -eufCo pipefail

PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Check if script is run with sudo
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script with sudo"
  exit 1
fi

# Get the actual username (not sudo user)
if [ -n "$SUDO_USER" ]; then
  ACTUAL_USER="$SUDO_USER"
else
  ACTUAL_USER="$(whoami)"
fi

# Configuration - edit these values
SCRIPT_PATH="/$PROJECT_DIR/update_ddns_subdomain.py"
VENV_PATH="/$PROJECT_DIR/.venv"
SERVICE_NAME="netcup-ddns-subdomain"
SERVICE_DESCRIPTION="Netcup DynDNS Subdomain update script"

#install venv
"${PROJECT_DIR}"/setup_exec_venv.sh

#restrict permissions for config file
chmod 600 "${PROJECT_DIR}"/netcup-ddns.conf

# Create systemd service file
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=${SERVICE_DESCRIPTION}
After=network.target

[Service]
Type=simple
User=${ACTUAL_USER}
Group=${ACTUAL_USER}
WorkingDirectory=${PROJECT_DIR}
Environment=PATH=${VENV_PATH}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=${VENV_PATH}/bin/python ${SCRIPT_PATH}
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Set proper permissions
chmod 644 /etc/systemd/system/${SERVICE_NAME}.service

# Reload systemd, enable and start the service
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}.service
systemctl start ${SERVICE_NAME}.service

# Print status
echo "Service ${SERVICE_NAME} has been created and started."
echo "Check status with: sudo systemctl status ${SERVICE_NAME}"
echo "View logs with: sudo journalctl -u ${SERVICE_NAME}"