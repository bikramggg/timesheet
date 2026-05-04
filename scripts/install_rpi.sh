#!/usr/bin/env bash
# Run on RPi after rsync. Sets up venv, deps, systemd units.
set -euo pipefail

cd /opt/timesheet

# venv
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# .env (idempotent)
if [ ! -f .env ]; then
  cp .env.example .env
  echo "*** Edit /opt/timesheet/.env with your tokens before running ***"
fi

# init DB
.venv/bin/python db.py

# systemd
sudo cp systemd/timesheet-collect.service /etc/systemd/system/
sudo cp systemd/timesheet-collect.timer /etc/systemd/system/
sudo cp systemd/timesheet-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now timesheet-collect.timer
sudo systemctl enable --now timesheet-web.service

echo "Installed. Web: http://raspberrypi.local:8080"
sudo systemctl status timesheet-web.service --no-pager | head -10
