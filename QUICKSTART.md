# AWS Control Center V9.1 极简部署

**不需要 `.aws` 文件夹，不需要提前安装 Node.js，也不需要手动配置 AWS Access Key。** 默认使用项目自带的旧版 Web 页面；React 新界面属于可选构建。

## 一、三步启动（Ubuntu / Debian）

1. 解压下载的 ZIP，进入 `aws-control-center` 目录。
2. 执行 `sudo bash setup.sh`。如果服务器已有 Docker Compose，会使用 Docker；否则自动采用 Linux 原生 systemd 安装（自动安装 Python 依赖）。也可指定 `sudo bash setup.sh docker` 或 `sudo bash setup.sh native`。
3. 用 `ssh -L 8000:127.0.0.1:8000 用户名@服务器IP` 建立安全隧道，然后在电脑浏览器访问 `http://127.0.0.1:8000`。

## 二、查找登录密码

Docker：`sudo grep '^APP_PASSWORD=' .env`

原生：`sudo grep '^APP_PASSWORD=' /etc/aws-control-center/awscc.env`

登录后到 **账户管理** 添加账户别名、Access Key ID、Secret Access Key，STS 验证后保存并点击 **激活**，再访问 Lightsail / EC2 等资源。不要使用 root 账号长期密钥；使用最小权限 IAM 密钥。

## 三、日常管理

Docker：`sudo docker compose ps`、`sudo docker compose logs -f`、`sudo docker compose restart`

原生：`sudo awscc status`、`sudo awscc logs`、`sudo awscc restart`

## 四、配置域名（可选）

将 Nginx/aaPanel 的 HTTPS 站点反向代理到 `http://127.0.0.1:8000`。确保已签发有效 TLS 证书，再将 `COOKIE_SECURE=true` 写入对应配置，并重启服务。不要直接将后端 8000 端口暴露到公网。

## 五、备份

Docker：备份项目 `.env`、`data/control.db`、`data/credential.key` 及整个 `data` 目录。备份前暂停写入或使用 SQLite 在线备份 API，避免直接复制活动数据库导致数据不一致。

原生：备份 `/etc/aws-control-center/awscc.env`、`/var/lib/aws-control-center/control.db`、`/var/lib/aws-control-center/credential.key`；可运行 `sudo awscc backup` 备份数据库，**另外备份 credential.key**。

**数据库和 credential.key 必须配对保存。** 丢失加密密钥会导致保存的 AWS Secret Access Key 无法解密。

## 六、注意

- Docker 模式需要预先安装 Docker Engine 和 Compose 插件；一键脚本不会自动安装 Docker。
- 原生安装只适配 Ubuntu / Debian + systemd，且需要 apt 网络与 Python 包安装网络。
- 默认只绑定 `127.0.0.1:8000`。远程先用 SSH 转发；公网使用需另配 HTTPS、网络访问限制与额外登录防护。
- 原生安装脚本为了兼容 V9.1，会保留传统 `.aws` 路径环境变量，但 Web 保存密钥并不依赖 `.aws` 目录存在。
- 本简化包已做静态检查；未在真实 Linux 机器、Docker 环境或真实 AWS 账户上完成端到端部署验证。
