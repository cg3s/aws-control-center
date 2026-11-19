# 快速部署 AWS Control Center

当前只提供新版 React 控制台和一套 `/api`，旧版页面与接口已移除。完整功能、升级和迁移说明见 [README](README.md)。

## Docker

在 Ubuntu/Debian 等已安装 Docker Engine 和 Compose 的服务器运行：

```bash
sudo bash setup.sh docker
```

部署包自带 `static/app/` 预编译前端，Docker 直接打包这些文件，无需 Node.js 或 npm。首次执行生成 `.env` 和随机管理员密码，默认只监听 `127.0.0.1:8000`，使用本地 HTTP Cookie 配置。

## 原生 systemd / aaPanel

发布包已有 `static/app/index.html` 时可运行：

```bash
sudo bash setup.sh native
```

仅有源码时，先在构建机器执行以下命令，并将生成的 `static/app` 随源码一起部署：

```bash
cd frontend
npm ci
npm run typecheck
npm run build
```

原生安装要求 Ubuntu/Debian、systemd，以及 apt/Python 包下载网络。与 Docker 二选一，不要同时运行两组 Worker。

## 登录与账户

远程访问先建立隧道：

```bash
ssh -L 8000:127.0.0.1:8000 USER@SERVER_IP
```

浏览器打开 `http://127.0.0.1:8000`。Docker 密码在项目 `.env`，原生密码在 `/etc/aws-control-center/awscc.env`。登录后在“AWS 账户”添加凭据，然后在顶部账户列表点击“激活”。业务页直接使用当前账户。使用 Access Key 时无需提前准备 `.aws` 文件夹。

使用 HTTPS 反代时将 `COOKIE_SECURE=true` 写入对应配置并重启。实例创建、删除与自动化执行均在界面中单独确认和配置。

## 管理、备份、升级

Docker 日志：`sudo docker compose logs -f`。

原生日常命令：`sudo awscc status`、`sudo awscc logs`、`sudo awscc restart`。

原生备份：`sudo awscc backup`，包含 `control.db`、`metrics.db`、加密密钥及配置。Docker 先停止三个服务，备份 `.env` 和整个 `data/` 后再启动。

原生升级：完成新前端构建后运行 `sudo awscc upgrade /path/to/source`。数据库与加密密钥必须配对保留；旧任务会归档，旧监控会导入为新规则，无法验证的规则默认禁用待复核。
