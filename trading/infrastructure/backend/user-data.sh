#!/bin/bash
# EC2 user-data / cloud-init for a FRESH Backend instance (Amazon Linux
# 2023, ap-south-1). Idempotent enough to also paste into an SSM RunCommand
# on an already-running box. It installs the platform only -- it does NOT
# create /etc/centralized-algo/backend.env (secrets) and does NOT start the
# app; do those steps by hand per DEPLOY.md so nothing sensitive lands in
# user-data (which is readable from inside the instance).
set -euxo pipefail

dnf update -y
dnf install -y git nginx docker amazon-cloudwatch-agent

# --- Docker ---
systemctl enable --now docker
usermod -aG docker ec2-user || true
# docker compose v2 plugin
mkdir -p /usr/libexec/docker/cli-plugins
curl -sSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
  -o /usr/libexec/docker/cli-plugins/docker-compose
chmod +x /usr/libexec/docker/cli-plugins/docker-compose

# --- dirs ---
mkdir -p /opt/centralized-algo /var/lib/centralized-algo/pgdata \
         /var/log/centralized-algo /etc/centralized-algo /etc/ssl/centralized-algo
chmod 700 /etc/centralized-algo

# --- code ---
if [ ! -d /opt/centralized-algo/app/.git ]; then
  git clone --branch web-base-algo-trading-control \
    https://github.com/ssamirr123/CentralizedAlgoSystem.git /opt/centralized-algo/app
fi

# --- Nginx (config placed by DEPLOY.md; just enable the service) ---
systemctl enable --now nginx

echo "user-data platform bootstrap done. Continue with DEPLOY.md from step 5."
