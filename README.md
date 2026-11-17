**V9.1 更新：** Web 添加多个 AWS Access Key 并激活，无需强制 `.aws`。详见 [V9_1_ACCESS_KEYS.md](V9_1_ACCESS_KEYS.md)。

> **V7.0:** See [V7_UPGRADE.md](V7_UPGRADE.md) for AWS regional catalog, read-only task verification and new CLI commands.

> **V6.0 upgrade:** See [V6_UPGRADE.md](V6_UPGRADE.md) for the persistent Lightsail create queue, Web UI, CLI, Docker and systemd instructions.

# AWS Control Center V4

Self-hosted single-administrator AWS dashboard, built with FastAPI, boto3, vanilla JavaScript and persistent SQLite. This is an independently built management panel, not a copy of SLauncher software.

## Features

- Multiple AWS shared profiles and cross-account STS AssumeRole aliases; V9.1 additionally saves encrypted Secret Access Keys in SQLite (Fernet key stored separately)
- Concurrent EC2 and Lightsail inventory by region with isolated per-service errors and pagination
- Start, stop, reboot and confirmation-based batch actions (up to 20 resources; sequential)
- Lightsail NetworkIn and NetworkOut CloudWatch sample sums (1/7/30 days), with daily chart
- Current-month Cost Explorer UnblendedCost by service
- Persistent session store, CSRF protection and basic per-IP login lockouts, SQLite operation audit
- Role account create/list/delete, without deleting IAM roles

## Deploy

```bash
cp .env.example .env
# Set a unique random APP_PASSWORD of 16+ characters, change regions as needed
# No ~/.aws directory is required for Web Access Key accounts. Legacy profiles are optional.
mkdir -p data
chmod 700 data
# When using HTTPS through Nginx, keep COOKIE_SECURE=true
docker compose up -d --build
```

App binds `127.0.0.1:8000` on the host. Reverse proxy behind HTTPS and restrict access with VPN/zero-trust access controls. Use `COOKIE_SECURE=false` ONLY for local HTTP testing. Never expose unauthenticated AWS management endpoints to the Internet. V9.1 no longer mounts `~/.aws` by default; optional legacy Profile mounts must be configured separately and protected. `AWS_EC2_METADATA_DISABLED=true` is set in Compose as a defensive default, so EC2 instance profile credentials do not work there unless explicitly reconfigured. Credential processes / SSO profiles may need additional files or host credential forwarding.

## Accounts

A role alias records `alias`, `source_profile`, `role_arn`, optional `external_id` in SQLite. When querying, the server calls STS AssumeRole using source credentials. Target role must trust the source principal. Role credentials are not persisted, but short-lived credentials exist in application process memory. Only the profile names visible in the panel can be used for mutation.

## Example least-privilege IAM actions

Read: `sts:GetCallerIdentity`, `sts:AssumeRole` (when needed), `ec2:DescribeInstances`, `lightsail:GetInstances`, `lightsail:GetInstanceMetricData`, `ce:GetCostAndUsage`.

Mutations (optional): `ec2:StartInstances`, `ec2:StopInstances`, `ec2:RebootInstances`, `lightsail:StartInstance`, `lightsail:StopInstance`, `lightsail:RebootInstance`.

Use IAM resource restrictions for state changes, where supported, and separate read-only roles. `ce:GetCostAndUsage` generally needs billing permissions and data access enabled by the account.

## API overview

- `POST /api/login` returns a CSRF secret to the JS client, sets `HttpOnly; SameSite=Strict` session cookie
- `GET /api/session`, `POST /api/logout`, `GET /api/config`, `GET /api/accounts`, `POST /api/accounts`, `DELETE /api/accounts/{alias}`
- `GET /api/identity`, `GET /api/instances`, `POST /api/instance/action`, `POST /api/instance/batch`
- `GET /api/traffic?profile=default&region=ap-northeast-1&instance=myserver&days=7`
- `GET /api/cost`, `GET /api/audit?limit=100`

State-changing calls after login require `X-AWSCC-CSRF` header, set to the secret returned from `/api/session`.

## Limitations & guardrails

- NetworkIn/NetworkOut are metric samples, **not** Lightsail plan traffic quota, billable transfer, or remaining plan allowance. CloudWatch metrics can be delayed or incomplete.
- Batch actions are bounded but not atomic: some items may succeed even when others fail. Read the per-item response and audit log.
- Cost Explorer can lag and final invoices may differ. Each AWS account is billed independently.
- Single-admin UI, no MFA/SSO/RBAC. For Internet-facing production deployments add an identity-aware access layer, MFA, alerting, immutable audit exports, backups, IAM scoping, observability, structured tests, and rate limits for all API endpoints.
- Login brute-force lockout is based on socket peer IP (not untrusted proxy headers), so behind Nginx it may lock all clients together if five failed attempts occur from that proxy IP. Restrict upstream at Nginx or use auth gateway / WAF in front.
- External IDs are stored as readable SQLite values. Lock down `./data` and maintain secure backups.
- SQLite persists sessions across restarts. Deploy only one application process; for multi-replica setups replace it with a central session/rate limiting store.

## V3: independent scheduled monitoring and Telegram alerts

V3 adds a dedicated `aws-monitor-worker` container (one replica only), V3 UI "流量告警", and new monitoring APIs. Existing `control.db` is upgraded additively using SQLite `CREATE TABLE IF NOT EXISTS` on startup. **Back up `./data` before updating.**

Set the following in `.env`:

```env
MONITOR_INTERVAL_SECONDS=900
ALERT_COOLDOWN_SECONDS=3600
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Leave the Telegram fields empty for database-only alerts. To enable notifications, supply an existing Telegram bot token and your designated chat ID. Keep `.env` private. Start both services via `docker compose up -d --build`. Inspect worker status with `docker compose logs -f aws-monitor-worker`. Do not scale the worker above 1 replica; there is no distributed lease in V3.

Create a watch from the monitoring tab or use the authenticated `POST /api/monitors` endpoint with `{ "profile":"default", "region":"ap-northeast-1", "instance":"example", "threshold_gib":1.0, "enabled":true }`. The threshold compares the **sum of Lightsail NetworkIn and NetworkOut samples over a sliding hour** (delayed 5 minutes for metric availability). This is neither monthly billable data transfer nor the included transfer quota. 15-minute samples overlap by design; do not sum them to compute a monthly transfer total.

New endpoints: `GET/POST /api/monitors`, `DELETE /api/monitors/{id}`, `GET /api/monitor/samples?monitor_id=...`, `GET /api/alerts`, and `POST /api/ip/check`. The last endpoint resolves the target instance public IPv4 using the AWS Lightsail API, rejects private/non-global IPs and limits tests to TCP 22, 80 or 443 from the panel server. It does not change IP allocation, test ICMP, or evaluate geographic connectivity. All mutation/probe requests require login and CSRF.

Additional IAM read permission: `lightsail:GetInstance`. Monitoring has an AWS API cost/usage impact depending on the service and metrics; keep the watch list deliberately small and verify current AWS pricing. Network metric errors appear in the sample table, but do not themselves trigger Telegram alerts. Alerts are recorded once per threshold crossing check with a cooldown (default 1h) and no automatic instance actions. Failed Telegram deliveries are recorded as undelivered, but **V3 does not retry** them. Deleted monitor samples remain in SQLite until regular retention cleanup or manual maintenance.

## V3 status and next steps

- Automatic monitoring, threshold alerts, Telegram send and TCP check are implemented; automated tests use mocked AWS APIs and Telegram. Real AWS integration and Telegram delivery have **not** been exercised in this package.
- No static-IP rotation, Lightsail monthly quota tracking, EC2 network monitoring, distributed worker coordination, MFA or React migration in V3.
- API values and errors are sanitized but the UI remains a small vanilla JS single-admin tool; review IAM and reverse-proxy protection before deploying publicly.

Test in development with `python -m pytest -q` and `node --check` on the extracted UI script.

---

## V4.0 更新（兼容 V3 的 SQLite 数据文件）

### 新增功能

- Lightsail 静态 IP 列表（逐页拉取），新建、绑定、解绑和释放。
- 释放静态 IP 前，必须再次输入准确名称；后端会拒绝释放仍然挂载的 IP。
- 每次通过 `/api/ip/check` 发起的 TCP 端口检测会写入 `ip_checks` 表；`/api/ip/history` 提供历史检索。
- `/api/lightsail/allowance` 查询实例对应的 Lightsail bundle 元数据，展示 `transferPerMonthInGb` 和标称套餐价格。**并非账单流量、当月实际用量或剩余流量**，不可据此自动判断是否超出 AWS 套餐。
- Web 界面新增「静态 IP」管理和检测历史页；不修改 V3 数据表结构，升级会自动创建新的 `ip_checks` 表。

### IAM 权限

为使用静态 IP 管理，需要针对所管理 Lightsail 资源授予 `lightsail:GetStaticIps`、`lightsail:GetStaticIp`、`lightsail:AllocateStaticIp`、`lightsail:AttachStaticIp`、`lightsail:DetachStaticIp`、`lightsail:ReleaseStaticIp`、`lightsail:GetInstance`、`lightsail:GetBundles` 等权限。请按 AWS IAM 官方服务授权参考核对操作名称、区域和资源粒度，并按需授权，而不是直接使用 `AdministratorAccess`。

### 升级

1. 停止 V3 服务：`docker compose down`。
2. 备份 `.env`、`data/` 和 AWS 凭据。不要删除 `data/control.db`。
3. 用 V4 源码覆盖程序文件，保留 `.env`、`data/`。
4. 执行 `docker compose up -d --build`，浏览器硬刷新页面。
5. 执行 `docker compose logs --tail 100 aws-control-center aws-monitor-worker` 查看服务日志。

### 限制及部署安全

- 静态 IP 释放是危险操作，释放后旧地址不能保证重新申请得到；页面要求人工确认。请先核对安全组、防火墙、DNS 和外部依赖。
- AWS 静态 IP 分配、未绑定资源或其他相关服务可能产生费用；操作调用成功并不等于目标状态立即生效。
- IP 健康检查仅限从面板所在服务器探测当前 AWS 返回的公网 IPv4 的 TCP 22、80、443，不代表全球连通性。
- 单管理员安全模型依然是早期版本，生产公网部署前需要额外的 MFA、细粒度 RBAC、TLS、访问控制和独立审计备份。
- 本地使用 Mock AWS Client 的 10 项自动化测试通过，但本环境未连接真实 AWS 账户，也未实际启动 Docker 容器。

---

## V4.1: 原生 Linux 命令行部署（无需 Docker）

支持 Ubuntu / Debian 等 **systemd Linux** 系统和 Python 3.10+；需已有 `python3-venv`、pip 可用、联网安装 Python 依赖。与 Docker 模式二选一，**不要同时运行**，以避免端口冲突和重复采集。

### 快速安装（仅本机监听）

```bash
# Ubuntu/Debian 先确保依赖已安装
sudo apt update
sudo apt install -y python3 python3-venv python3-pip curl

# 在解压源码后的 aws-control-center 目录执行
sudo bash deploy/install.sh --user "$USER"
sudo awscc status
sudo awscc health
sudo grep '^APP_PASSWORD=' /etc/aws-control-center/awscc.env
```

程序默认监听 `127.0.0.1:8000`，**不会直接暴露公网**。本地 HTTP 模式设置 `COOKIE_SECURE=false`；通过 SSH 隧道安全访问，例如本机运行 `ssh -L 8000:127.0.0.1:8000 user@server`，访问 `http://127.0.0.1:8000`。

AWS 凭据默认使用安装指定用户的 `~/.aws/config` 和 `~/.aws/credentials`，与 Docker 挂载的 `~/.aws` 相互独立，确保对应非 root 用户具有读取权限。EC2 Instance Profile 默认禁用，如需实例角色需按实际环境调整（不要无意打开元数据访问）。

### 带域名和现有 TLS 证书的 Nginx 部署

```bash
sudo apt install -y nginx
sudo bash deploy/install.sh --user "$USER" \
  --domain aws.example.com \
  --tls-cert /etc/letsencrypt/live/aws.example.com/fullchain.pem \
  --tls-key /etc/letsencrypt/live/aws.example.com/privkey.pem
```

**注意：** 证书必须事先获取且可供 Nginx 读取。安装器不自动申请证书，也不会修改防火墙策略、开放端口或创建 DNS 记录。若前置 aaPanel/Nginx 已有代理，直接采用仅本机监听的方式，配置 HTTPS 反代到 `127.0.0.1:8000`，并将 `/etc/aws-control-center/awscc.env` 的 `COOKIE_SECURE` 改为 `true` 后执行 `sudo awscc restart`。

### CLI 管理命令

```bash
sudo awscc status         # systemd 状态
sudo awscc start          # 启动 Web 和 Worker
sudo awscc stop           # 停止 Web 和 Worker
sudo awscc restart        # 重启
sudo awscc logs           # 实时全部日志
sudo awscc web-logs       # Web 日志
sudo awscc worker-logs    # Worker 日志
sudo awscc health         # 本机 HTTP 健康探测
sudo awscc backup         # SQLite 一致性备份到 /var/backups
sudo awscc upgrade /path/to/new/aws-control-center  # 备份并升级（保留配置及数据）
```

- 代码：`/opt/aws-control-center/`
- 环境配置：`/etc/aws-control-center/awscc.env`，仅 root 可读
- 数据库：`/var/lib/aws-control-center/control.db`（归部署用户所有）
- 后台服务：`awscc-web.service`、`awscc-worker.service`（独立进程）
- 安全：默认单进程 Web、单进程 Worker、localhost 绑定、systemd 限制写入目录；此版本依然缺乏 MFA/RBAC，公网部署必须添加独立身份认证或零信任访问控制。

**升级 V4 Docker 用户注意：** 原生部署不会自动读取旧项目的 `./data/control.db`。如需迁移，停止旧容器/服务后备份 SQLite，人工复制数据库文件及关联数据，调整属主，再启动新服务，避免两个 Worker 同时采集。不要把凭据复制进 Web 静态目录。

---

## V5 — AWS Resource CLI (native Linux deployment)

V5 extends V4.1 with AWS resource commands, using **the existing Python `app.py` AWS implementation**, account aliases, region allowlist and SQLite audit database. Commands execute locally under the **same non-root service identity** as the Web process; the launcher reads the root-restricted configuration then drops privileges before running resource commands. No Web login, cookies or CSRF token is used by the local CLI. Protect `sudo` permissions appropriately.

### Upgrade from V4.1

Back up the current installation and unpack this ZIP to a staging directory:

```bash
sudo awscc backup
unzip aws-control-center-v5.zip
sudo awscc upgrade ./aws-control-center
sudo awscc status
```

`awscc upgrade` installs `cli.py`, `deploy/awscc` and the CLI launcher in addition to existing backend files. It preserves `/etc/aws-control-center/awscc.env` and `/var/lib/aws-control-center/control.db`. Run commands with `sudo` on the host where native systemd deployment was installed. **This new resource CLI is not installed inside the Docker container by default**; Docker retains the Web dashboard/worker.

For a fresh native installation:

```bash
sudo bash deploy/install.sh --user "$USER"
sudo awscc status
sudo awscc instances list --service lightsail --region ap-northeast-1
```

### Resource commands

```bash
# Inventory and exportable JSON output
sudo awscc instances list --profile default --region all --service all
sudo awscc instances list --profile default --region ap-northeast-1 --service lightsail --json

# Lifecycle commands: interactive confirmation; --yes for unattended scripts
sudo awscc instances stop --service ec2 --region us-east-1 --name i-1234567890abcdef0 --yes
sudo awscc instances start --service ec2 --region us-east-1 --name i-1234567890abcdef0 --yes
sudo awscc instances reboot --service lightsail --region ap-northeast-1 --name my-server

# Lightsail static IP operations
sudo awscc ip list --region ap-northeast-1
sudo awscc ip allocate --region ap-northeast-1 --name my-static-ip
sudo awscc ip attach --region ap-northeast-1 --name my-static-ip --instance my-server
sudo awscc ip detach --region ap-northeast-1 --name my-static-ip
sudo awscc ip release --region ap-northeast-1 --name my-static-ip

# Reports / account aliases / audit
sudo awscc traffic report --region ap-northeast-1 --instance my-server --days 7 --json
sudo awscc cost report --profile default --json
sudo awscc accounts list
sudo awscc audit list --limit 50
```

Do **not** use `instances restart`: the actual supported command is `instances reboot`. `--profile` accepts either a local shared AWS profile or a Web-configured AssumeRole alias. `--region all` works for inventory only; any write or single-instance report needs a specific region. A write in a non-interactive shell **must** include `--yes`; the confirmation is not an AWS dry-run. All CLI mutations have AWS side effects and may incur charges; IP allocation may also incur charges.

The CLI supports `--json` (positioned at command end) for automation. Exit codes: `0` success, `1` API/operation failure, `2` argument/confirmation error, `3` inventory partial failure (results may still have been printed). Never mistake the CloudWatch sample sum for Lightsail billed monthly allowance.

### CLI limitations

- Requires native Linux/systemd install; no Windows installer yet.
- CLI runs under the same privilege set as the single-admin Web backend. There is no additional CLI RBAC policy.
- AWS SDK may retry transient failures and state changes are asynchronous; a submitted operation is not proof that the requested final instance state was reached.
- No automatic IP rotation, bulk provisioning, or task queue is implemented in this version.
- `--yes` disables interactive confirmation: allow it only in carefully reviewed scripts and tightly scoped IAM roles.
- For `.env` values with unusual quoting, use simple `KEY=value` assignments. The native CLI launcher is not a full shell parser. Do not store multiline values in this file.

### Tests

`python -m pytest -q` — 16 tests passing locally (mocked AWS APIs). `python -m py_compile ...`, `bash -n deploy/awscc deploy/install.sh`, and ZIP integrity checked. Real AWS changes, end-to-end systemd deployment and Nginx were **not** tested in this environment.

## V8 — React/TypeScript 管理后台（渐进迁移）

V8 提供可选的 `frontend/` React 18 + TypeScript + Ant Design 5 管理界面源码：资源看板、实例清单、创建任务自动轮询（10 秒）、AWS 实例状态核验。**此版本并非全站 React 重写**：静态 IP、监控告警、角色配置、创建表单、审计等仍在 `/legacy` 页面可用。

V8 的新增 `/api/dashboard/summary` 接口汇总跨区域实例、运行状态、任务状态与 24 小时告警；接口会保留部分 AWS 区域失败信息。跨区域扫描产生 AWS API 调用，请控制刷新频率。旧版任务页也增加 10 秒可见性轮询，不会自动重试创建请求。

### 构建新 UI（需要 Node.js 20.19+ 或 22.12+、可访问 npm Registry）

```bash
cd frontend
npm install
npm run build
cd ..
```

构建输出到 `static/v8/`。如果构建产物存在，根路径 `/` 自动提供 React 管理界面；否则自动回退原有 HTML 管理界面。`/legacy` 始终访问旧版全功能页面。Docker 镜像只复制已经构建的 `static/`，不要求在容器启动时安装 npm。Linux 原生安装脚本也复制 `static/`。Vite 开发服务器 `/api` 默认代理到 `127.0.0.1:8000`，需先启动 FastAPI 后端。

**安全与升级**：先备份 `.env` 和 SQLite。React 界面复用现有 HttpOnly Session 与 CSRF 机制。V8 不改变数据库表结构，不引入新的 systemd 服务。构建成功之前请继续使用 V7/V8 兼容页面；实际 AWS、Docker 和生产 Nginx 环境仍须单独联调。

## V9 — React 统一管理控制台

V9 迭代将原先仅有资源看板/清单的 React 页面扩展至实例启停及批量操作、Lightsail 创建任务表单、静态 IP 操作、监控规则与告警、跨账户 IAM Role、审计日志。依旧保留 `/legacy` 页面作为兼容入口，API、CLI 和数据库沿用 V8。

请阅读 `V9_UPGRADE.md` 的具体构建和升级命令。注意 React UI **尚未在本执行环境完成 npm 安装、类型检查或生产构建**；原因是 npm Registry DNS 请求失败。后端测试不能代替前端构建和真实 AWS 联调。
