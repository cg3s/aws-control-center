> 历史部署说明：当前单 UI/API 的构建和升级步骤请优先使用 [README](README.md) 与 [QUICKSTART](QUICKSTART.md)。

这个项目已经带有 Docker Compose 配置，在 aaPanel 上建议采用 **Docker 运行程序，aaPanel 的 Nginx 负责域名、HTTPS 和反向代理**。

项目需要启动 **Web、监控 Worker、任务 Worker 共三个服务**，直接使用现有 Compose 文件即可。

**1. 准备环境**

在 aaPanel 中安装 Nginx 和 Docker，终端确认：

```bash
docker --version
docker compose version
```

将完整项目上传并解压到：

```text
/opt/aws-control-center
```

源码、`.env` 和数据目录放在这个目录，避免作为网站静态文件公开。

**2. 配置环境变量**

在 aaPanel 终端执行：

```bash
cd /opt/aws-control-center
cp .env.example .env
mkdir -p data
chmod 700 data
chmod 600 .env
```

用 aaPanel 文件编辑器修改 `.env`，保留其他参数，主要设置：

```dotenv
APP_PASSWORD=替换为至少16位的随机强密码
COOKIE_SECURE=true
DATA_DIR=/app/data
AWS_REGIONS=us-east-1,us-west-2,ap-northeast-1,ap-northeast-2,ap-southeast-1,eu-west-1
```

`DATA_DIR=/app/data` 是容器内路径，保持这个值。Telegram 配置可以暂时留空。

**3. 检查预编译前端**

确认上传的项目包含完整的 `static/app/` 目录。Docker 直接打包这份前端产物，aaPanel 服务器无需安装 Node.js 或执行 npm。修改前端后，在开发机或 CI 构建并上传新的 `static/app/`。

**4. 启动程序**

```bash
cd /opt/aws-control-center
docker compose up -d --build
docker compose ps
docker compose logs --tail=100
```

应能看到三个服务运行：

```text
aws-control-center
aws-monitor-worker
aws-task-worker
```

测试本机访问：

```bash
curl -I http://127.0.0.1:8000/
```

项目默认只映射到 `127.0.0.1:8000`，**无需在安全组或防火墙开放 8000 端口**。两个 Worker 都保持一个实例。

**5. 在 aaPanel 配置域名和 HTTPS**

先把域名，例如 `aws.example.com`，解析到服务器公网 IP。

然后在 aaPanel 的 **Website → Proxy / Proxy Project** 中创建反向代理；旧版界面可通过“添加站点 → 设置 → 反向代理”配置。aaPanel 官方支持将域名代理到本地 Docker 映射端口。[官方反向代理文档](https://www.aapanel.com/docs/Function/proxy.html)

填写：

| 配置项 | 值 |
|---|---|
| 域名 | `aws.example.com` |
| 目标 URL | `http://127.0.0.1:8000` |
| 代理路径 | `/` |
| 缓存 | 关闭，尤其是 `/api/` |

在该站点的 SSL 设置中申请证书，并开启强制 HTTPS。服务器安全组和防火墙放行网站需要的 **80、443** 端口。

这里按 **Nginx 安装在宿主机上** 配置；若 Nginx 本身在容器中运行，代理地址需要根据容器网络调整。

**6. 登录并添加 AWS 账户**

访问：

```text
https://aws.example.com
```

使用 `.env` 中的 `APP_PASSWORD` 登录，然后进入：

```text
账户管理 → Web Access Key 凭据账户 → 添加 → 激活
```

当前版本支持网页添加 AWS Key，**不必提前配置服务器的 `~/.aws`**。建议使用具备所需权限的 IAM 账户，并为管理域名设置 IP 白名单或零信任访问控制。

日常查看日志和更新：

```bash
cd /opt/aws-control-center

# 查看日志
docker compose logs -f --tail=100

# 更新源码后重新构建
docker compose up -d --build
```

升级前备份 `.env` 和整个 `data/`。其中 **`control.db` 与 `credential.key` 必须一起备份**，否则恢复后保存的 AWS Secret Key 可能无法解密。
