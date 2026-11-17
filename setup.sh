#!/usr/bin/env bash
# AWS Control Center V9.1 streamlined deployment. Run: sudo bash setup.sh [docker|native]
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo '请使用 sudo bash setup.sh'; exit 1; }
cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
MODE="${1:-auto}"
[[ "$MODE" == auto || "$MODE" == docker || "$MODE" == native ]] || { echo '用法: sudo bash setup.sh [docker|native]'; exit 2; }
if [[ $MODE == auto ]]; then
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then MODE=docker; else MODE=native; fi
fi
if [[ "$MODE" == docker ]]; then
  command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 || { echo 'Docker Compose 不可用。先安装 Docker Compose，或运行 sudo bash setup.sh native'; exit 1; }
  if [[ ! -f .env ]]; then
    umask 077
    PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))' 2>/dev/null || head -c 24 /dev/urandom | base64 -w0)"
    cp .env.example .env
    sed -i "s|^APP_PASSWORD=.*|APP_PASSWORD=$PASSWORD|; s|^COOKIE_SECURE=.*|COOKIE_SECURE=false|" .env
    chmod 0600 .env
  fi
  install -d -m 0700 data
  docker compose up -d --build
  echo 'Docker 服务已启动。'
  echo '本机: http://127.0.0.1:8000'
  echo '远程建议通过 SSH 转发: ssh -L 8000:127.0.0.1:8000 USER@SERVER_IP'
  echo '管理密码: sudo grep ^APP_PASSWORD= .env'
  echo '启用公网 HTTPS 后，请将 .env 中 COOKIE_SECURE 改为 true 并运行 docker compose up -d'
else
  [[ -d /run/systemd/system ]] || { echo '原生安装要求 systemd (Ubuntu/Debian)'; exit 1; }
  if ! command -v apt-get >/dev/null; then
    echo '当前仅支持 Ubuntu/Debian 的一键原生安装'; exit 1
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3 python3-venv python3-pip ca-certificates
  if ! id awscc >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/awscc-user --shell /usr/sbin/nologin awscc
  fi
  bash deploy/install.sh --user awscc
  echo '原生服务已启动。'
  echo '本机: http://127.0.0.1:8000'
  echo '远程建议通过 SSH 转发: ssh -L 8000:127.0.0.1:8000 USER@SERVER_IP'
  echo '管理密码: sudo grep ^APP_PASSWORD= /etc/aws-control-center/awscc.env'
  echo '命令: sudo awscc status / sudo awscc logs'
fi
