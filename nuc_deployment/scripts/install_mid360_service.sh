#!/usr/bin/env bash
set -e
install -m 0644 /home/haoyu/radar_deploy/campuscar-mid360.service /etc/systemd/system/campuscar-mid360.service
systemctl daemon-reload
systemctl enable campuscar-mid360.service
systemctl restart campuscar-mid360.service
sleep 8
systemctl is-enabled campuscar-mid360.service
systemctl is-active campuscar-mid360.service
systemctl --no-pager --full status campuscar-mid360.service | sed -n '1,35p'
