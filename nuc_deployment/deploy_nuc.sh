#!/usr/bin/env bash
set -euo pipefail

PATCH_ROOT=/tmp/nuc_command_patch
CONTROL_ROOT=/opt/cyberluban-control
ROS_ROOT=/home/haoyu/campuscar_ws/src/u2r_r2u_bridge
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_ROOT=/root/cyberluban-backups/$STAMP

mkdir -p "$BACKUP_ROOT/control-nuc" "$BACKUP_ROOT/ros-package"
cp -a "$CONTROL_ROOT/nuc/app.py" "$CONTROL_ROOT/nuc/serial_bridge.py" \
  "$CONTROL_ROOT/nuc/settings.py" "$CONTROL_ROOT/nuc/static/index.html" \
  "$CONTROL_ROOT/nuc/static/app.js" "$CONTROL_ROOT/nuc/static/style.css" \
  "$BACKUP_ROOT/control-nuc/"
cp -a /etc/cyberluban-control.env \
  /etc/systemd/system/campuscar-rtk-ue.service \
  "$BACKUP_ROOT/"
cp -a "$ROS_ROOT/setup.py" "$ROS_ROOT/package.xml" \
  "$ROS_ROOT/launch/rtk_ue_bringup.launch.py" \
  "$ROS_ROOT/u2r_r2u_bridge" "$BACKUP_ROOT/ros-package/"

install -m 0644 "$PATCH_ROOT/control/app.py" "$CONTROL_ROOT/nuc/app.py"
install -m 0644 "$PATCH_ROOT/control/serial_bridge.py" \
  "$CONTROL_ROOT/nuc/serial_bridge.py"
install -m 0644 "$PATCH_ROOT/control/settings.py" "$CONTROL_ROOT/nuc/settings.py"
install -m 0644 "$PATCH_ROOT/control/index.html" "$CONTROL_ROOT/nuc/static/index.html"
install -m 0644 "$PATCH_ROOT/control/app.js" "$CONTROL_ROOT/nuc/static/app.js"
install -m 0644 "$PATCH_ROOT/control/style.css" "$CONTROL_ROOT/nuc/static/style.css"

install -m 0644 "$PATCH_ROOT/ros/setup.py" "$ROS_ROOT/setup.py"
install -m 0644 "$PATCH_ROOT/ros/package.xml" "$ROS_ROOT/package.xml"
install -m 0644 "$PATCH_ROOT/ros/rtk_ue_bringup.launch.py" \
  "$ROS_ROOT/launch/rtk_ue_bringup.launch.py"
install -m 0644 "$PATCH_ROOT/ros/campus_command_bridge.py" \
  "$ROS_ROOT/u2r_r2u_bridge/campus_command_bridge.py"

if ! grep -q '^ROS_COMMAND_TOKEN=' /etc/cyberluban-control.env; then
  printf 'ROS_COMMAND_TOKEN=%s\n' "$(openssl rand -hex 32)" \
    >> /etc/cyberluban-control.env
fi
chmod 600 /etc/cyberluban-control.env

if ! grep -q '^EnvironmentFile=/etc/cyberluban-control.env$' \
    /etc/systemd/system/campuscar-rtk-ue.service; then
  sed -i '/^Environment=ROS_DOMAIN_ID=/a EnvironmentFile=/etc/cyberluban-control.env' \
    /etc/systemd/system/campuscar-rtk-ue.service
fi

systemctl stop campuscar-rtk-ue.service || true

runuser -u haoyu -- bash -c '
  set -eo pipefail
  source /opt/ros/humble/setup.bash
  cd /home/haoyu/campuscar_ws
  colcon build --packages-select u2r_r2u_bridge --symlink-install
'

systemctl daemon-reload
systemctl restart cyberluban-control.service
sleep 4
systemctl restart campuscar-rtk-ue.service
sleep 5

printf 'BACKUP_ROOT=%s\n' "$BACKUP_ROOT"
systemctl is-active cyberluban-control.service
systemctl is-active campuscar-rtk-ue.service
