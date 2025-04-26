#!/bin/bash
# This script installs systemd units / monitors and timers that are intended for use with systemd-networkd
# which is a sensible choice on servers running Debian 12 (bookworm), Ubuntu and other distributions
# You may need to modify the script to make it work on systems that use old-school ifup/down or netplan

# safe mode
set -eufCo pipefail

PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Check if script is run with sudo
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script with sudo"
  exit 1
fi
# Determine actual user - used for SERVICE_USER default if needed
if [ -n "${SUDO_USER:-}" ]; then
  CURRENT_USER="$SUDO_USER"
else
  CURRENT_USER="$(whoami)"
fi

# Configuration - set defaults if not defined in environment
: "${SERVICE_USER:=$CURRENT_USER}"
: "${SERVICE_NAME:=netcup-ddns-subdomain}"
: "${SERVICE_DESCRIPTION:=Netcup DynDNS Subdomain update script}"
: "${TIMER_SCHEDULE:=*:0/15}"  # Run every 15 minutes
: "${INSTALL_PATH:=/opt/${SERVICE_NAME}}"

: "${SCRIPT_PATH:=/$PROJECT_DIR/update_ddns_subdomain.py}"
: "${VENV_PATH:=/$PROJECT_DIR/.venv}"


if [[ "${INSTALL_PATH}" != "${PROJECT_DIR}" ]]; then
  mkdir -p "${INSTALL_PATH}"
  chown -R root:${SERVICE_USER} "${INSTALL_PATH}"
  #restrict permissions for config file
  chmod 640 "${INSTALL_PATH}"/netcup-ddns.conf
fi

#install venv
"${INSTALL_PATH}"/setup_exec_venv.sh


# Create systemd service file, which runs as one-shot
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=${SERVICE_DESCRIPTION}
After=network.target systemd-networkd-wait-online.service

[Service]
Type=oneshot
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_PATH}
Environment=PATH=${VENV_PATH}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=${VENV_PATH}/bin/python ${SCRIPT_PATH} --one-shot
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# create a systemd timer that calls the one-shot service above
cat > "/etc/systemd/system/${SERVICE_NAME}.timer" << EOL
[Unit]
Description=${DESCRIPTION} Timer
After=network.target systemd-networkd-wait-online.service

[Timer]
OnBootSec=1min
OnUnitActiveSec=${TIMER_SCHEDULE}
Unit=${SERVICE_NAME}.service

[Install]
WantedBy=timers.target
EOL

# Create systemd path unit to watch for network changes
echo "Creating systemd path unit for network change detection..."
cat > "/etc/systemd/system/${SERVICE_NAME}-netchange.path" << EOL
[Unit]
Description=${DESCRIPTION} Network Change Monitor
After=network.target systemd-networkd-wait-online.service

[Path]
PathChanged=/run/systemd/netif/state
Unit=${SERVICE_NAME}.service

[Install]
WantedBy=multi-user.target
EOL

# Create a corresponding service unit for the path unit
cat > "/etc/systemd/system/${SERVICE_NAME}-netchange.service" << EOL
[Unit]
Description=${DESCRIPTION} Network Change Service
After=network.target systemd-networkd-wait-online.service

[Service]
Type=oneshot
ExecStart=/bin/systemctl start ${SERVICE_NAME}.service

[Install]
WantedBy=multi-user.target
EOL

# Set proper permissions
chmod 644 /etc/systemd/system/${SERVICE_NAME}.{service,timer}

# Reload systemd, enable and start the service
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}.service
systemctl start ${SERVICE_NAME}.service

# Print status
echo "Service ${SERVICE_NAME} has been created and started."
echo "Check status with: sudo systemctl status ${SERVICE_NAME}"
echo "View logs with: sudo journalctl -u ${SERVICE_NAME}"