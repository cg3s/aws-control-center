#!/usr/bin/env bash
# Native Linux/systemd deployment. Run: sudo bash deploy/install.sh [--user USER] [--domain FQDN --tls-cert FILE --tls-key FILE]
set -Eeuo pipefail
if [[ $EUID -ne 0 ]]; then echo 'Run with sudo/root' >&2; exit 1; fi
if [[ ! -d /run/systemd/system ]]; then echo 'systemd is required (Ubuntu/Debian systemd host)' >&2; exit 1; fi
APP_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_USER="${SUDO_USER:-}"; DOMAIN=''; TLS_CERT=''; TLS_KEY=''; INSTALL_DIR='/opt/aws-control-center'; CONFIG_DIR='/etc/aws-control-center'; DATA_DIR='/var/lib/aws-control-center'
while (($#)); do
  case "$1" in
    --user) DEPLOY_USER="${2:?}"; shift 2;;
    --domain) DOMAIN="${2:?}"; shift 2;;
    --tls-cert) TLS_CERT="${2:?}"; shift 2;;
    --tls-key) TLS_KEY="${2:?}"; shift 2;;
    *) echo "Unknown argument: $1" >&2; exit 2;;
  esac
done
if [[ -z "$DEPLOY_USER" || "$DEPLOY_USER" == root ]]; then echo 'Use sudo from a non-root user or pass --user nonroot' >&2; exit 1; fi
id "$DEPLOY_USER" >/dev/null
HOME_DIR="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
[[ "$HOME_DIR" == /* && -d "$HOME_DIR" ]] || { echo 'Invalid home directory' >&2; exit 1; }
if [[ -n "$DOMAIN" ]]; then
  [[ "$DOMAIN" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*[a-zA-Z0-9]$ && "$DOMAIN" == *.* ]] || { echo 'Invalid domain' >&2; exit 1; }
  [[ -f "$TLS_CERT" && -f "$TLS_KEY" ]] || { echo 'Provide existing --tls-cert and --tls-key files' >&2; exit 1; }
  command -v nginx >/dev/null || { echo 'Install nginx first to use --domain' >&2; exit 1; }
fi
if ! command -v python3 >/dev/null || ! python3 -c 'import sys; assert sys.version_info >= (3,10)' 2>/dev/null; then
  echo 'Python 3.10+ is required; install python3, python3-venv and python3-pip' >&2; exit 1
fi
if ! python3 -m venv --help >/dev/null 2>&1; then echo 'Install python3-venv first' >&2; exit 1; fi
install -d -m 0755 "$INSTALL_DIR" "$CONFIG_DIR"
install -d -m 0700 -o "$DEPLOY_USER" -g "$(id -gn "$DEPLOY_USER")" "$DATA_DIR"
# Never overwrite customer config and data. Avoid copying .env/AWS credentials from source.
for p in app.py worker.py taskqueue.py cli.py requirements.txt; do install -m 0644 "$APP_SRC/$p" "$INSTALL_DIR/$p"; done
install -d -m 0755 "$INSTALL_DIR/static"
cp -a "$APP_SRC/static/." "$INSTALL_DIR/static/"
ENV_FILE="$CONFIG_DIR/awscc.env"
if [[ ! -e "$ENV_FILE" ]]; then
  PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  # Keep the private config umask scoped; the service user must read the venv.
  (
    umask 077
    cat > "$ENV_FILE" <<ENV
APP_PASSWORD=$PASS
COOKIE_SECURE=$( [[ -n "$DOMAIN" ]] && echo true || echo false )
AWS_REGIONS=us-east-1,us-west-2,ap-northeast-1,ap-northeast-2,ap-southeast-1,eu-west-1
DATA_DIR=$DATA_DIR
MONITOR_INTERVAL_SECONDS=900
ALERT_COOLDOWN_SECONDS=3600
TASK_POLL_SECONDS=3
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
AWS_CONFIG_FILE=$HOME_DIR/.aws/config
AWS_SHARED_CREDENTIALS_FILE=$HOME_DIR/.aws/credentials
AWS_EC2_METADATA_DISABLED=true
ENV
  )
  echo "Created $ENV_FILE. Retrieve APP_PASSWORD using: sudo grep '^APP_PASSWORD=' $ENV_FILE"
else
  echo "Preserved existing configuration $ENV_FILE"
fi
# Restrict environment access to root and the service account's primary group.
# systemd EnvironmentFile is read by the manager, but the CLI also needs access
# to non-secret settings. Avoid world-readable environment files.
chown root:"$(id -gn "$DEPLOY_USER")" "$ENV_FILE"
chmod 0640 "$ENV_FILE"
# Dedicated environment, upgraded in place; no system Python mutations.
(
  umask 022
  python3 -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"
)
# Also repair environments created by older installers with umask 077.
# Add read/traverse access without granting write access or changing ownership.
chmod -R go+rX "$INSTALL_DIR/.venv"
cat > /etc/systemd/system/awscc-web.service <<UNIT
[Unit]
Description=AWS Control Center Web
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$DEPLOY_USER
Group=$(id -gn "$DEPLOY_USER")
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
ProtectHome=read-only
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/awscc-worker.service <<UNIT
[Unit]
Description=AWS Control Center Monitor Worker
After=network-online.target awscc-web.service
Wants=network-online.target
[Service]
Type=simple
User=$DEPLOY_USER
Group=$(id -gn "$DEPLOY_USER")
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/.venv/bin/python worker.py
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
ProtectHome=read-only
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/awscc-task-worker.service <<UNIT
[Unit]
Description=AWS Control Center Create Task Worker
After=network-online.target awscc-web.service
Wants=network-online.target
[Service]
Type=simple
User=$DEPLOY_USER
Group=$(id -gn "$DEPLOY_USER")
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/.venv/bin/python taskqueue.py
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
ProtectHome=read-only
[Install]
WantedBy=multi-user.target
UNIT
install -m 0755 "$APP_SRC/deploy/awscc" /usr/local/bin/awscc
install -m 0755 "$APP_SRC/deploy/awscc-resource-launcher.py" "$INSTALL_DIR/resource-launcher.py"
if [[ -n "$DOMAIN" ]]; then
  nginx_conf="/etc/nginx/conf.d/awscc.conf"
  cat > "$nginx_conf" <<NGINX
server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}
server {
    listen 443 ssl;
    server_name $DOMAIN;
    ssl_certificate $TLS_CERT;
    ssl_certificate_key $TLS_KEY;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 1m;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_connect_timeout 10s;
        proxy_read_timeout 120s;
    }
}
NGINX
  nginx -t && systemctl reload nginx
fi
systemctl daemon-reload
systemctl enable --now awscc-web.service awscc-worker.service awscc-task-worker.service
systemctl restart awscc-web.service awscc-worker.service awscc-task-worker.service
printf '\nInstalled. Services: sudo awscc status\n'
if [[ -n "$DOMAIN" ]]; then echo "HTTPS: https://$DOMAIN"; else echo 'Local-only: http://127.0.0.1:8000 (use SSH tunnel for remote access)'; fi
echo "Data: $DATA_DIR   Config: $ENV_FILE"
