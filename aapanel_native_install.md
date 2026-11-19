> 历史部署说明：当前单 UI/API 的构建和升级步骤请优先使用 [README](README.md) 与 [QUICKSTART](QUICKSTART.md)。

项目自带了 **Python 虚拟环境 + systemd 的原生安装脚本**。在 aaPanel 中，用该脚本运行程序，再由 aaPanel 配置域名和 HTTPS 反向代理即可。

下面以 **Ubuntu 22.04 / 24.04、Debian 12** 为例，在 aaPanel 终端使用 root 执行。

1. **上传项目源码**

   将完整项目上传并解压到：

   ```text
   /opt/awscc-source
   ```

   确认目录中有 `setup.sh`、`app.py`、`deploy/`、`static/` 等文件。

2. **构建新版界面（可选）**

   如果需要 React 管理界面，先通过 aaPanel 安装并启用 Node.js **22.12+**，在终端执行：

   ```bash
   cd /opt/awscc-source/frontend
   npm install
   npm run build
   ```

   应在安装后端**之前**完成构建，让安装脚本复制 `static/v8/` 产物。跳过时会使用旧版界面。

3. **执行原生安装**

   ```bash
   cd /opt/awscc-source
   bash setup.sh native
   ```

   脚本会自动安装 Python 依赖、创建专用运行用户和虚拟环境，并设置三个服务开机自启：

   | 服务 | 用途 |
   |---|---|
   | `awscc-web` | 网页和 API |
   | `awscc-worker` | 监控、告警 |
   | `awscc-task-worker` | 执行创建任务 |

   检查运行情况：

   ```bash
   apt-get install -y curl
   awscc status
   awscc health
   ```

   健康检查应返回 `HTTP 200`。程序监听 `127.0.0.1:8000`。

   **不需要再到 aaPanel 的 Python 项目管理器重复启动程序。**

4. **修改 HTTPS 配置并读取密码**

   原生安装实际读取的配置文件是：

   ```text
   /etc/aws-control-center/awscc.env
   ```

   通过 aaPanel 文件管理器编辑，将：

   ```env
   COOKIE_SECURE=false
   ```

   改为：

   ```env
   COOKIE_SECURE=true
   ```

   然后执行：

   ```bash
   awscc restart
   grep '^APP_PASSWORD=' /etc/aws-control-center/awscc.env
   ```

   最后一条命令显示安装脚本自动生成的管理密码。**原生部署修改项目目录里的 `.env` 不会生效。**

5. **在 aaPanel 配置反向代理**

   将域名，例如 `aws.example.com`，解析到服务器公网 IP。在 aaPanel 的网站管理中添加代理项目，或在站点设置中添加反向代理：

   | 配置项 | 填写内容 |
   |---|---|
   | 域名 | `aws.example.com` |
   | 目标地址 | `http://127.0.0.1:8000` |
   | 代理路径 | `/` |
   | 缓存 | 关闭 |

   aaPanel 支持通过代理项目将域名转发到本机服务。[官方配置说明](https://www.aapanel.com/docs/Function/proxy.html)

   在该站点的 SSL 设置中申请证书，并开启强制 HTTPS。安全组、防火墙放行 **80、443**，无需开放 8000。

   **网站根目录不要指向项目源码或数据目录**；所有页面和 API 都通过反向代理访问。使用现有 aaPanel Nginx 即可，无需额外安装系统 Nginx。

6. **登录和日常管理**

   访问 `https://aws.example.com`，使用上面获取的密码登录。在“账户管理”中添加并激活 AWS Access Key，无需提前配置 `~/.aws`。

   常用命令：

   ```bash
   awscc status       # 查看三个服务
   awscc restart      # 重启
   awscc logs         # 查看实时日志，Ctrl+C退出
   awscc backup       # 备份数据库和凭据加密密钥
   ```

   安装后的目录：

   | 内容 | 路径 |
   |---|---|
   | 程序及虚拟环境 | `/opt/aws-control-center/` |
   | 实际配置 | `/etc/aws-control-center/awscc.env` |
   | 数据库及凭据加密密钥 | `/var/lib/aws-control-center/` |
   | 自动备份 | `/var/backups/aws-control-center/` |

   `awscc backup` **不包含配置文件**，还需单独保护和备份 `awscc.env`。管理域名建议限制访问 IP 或接入零信任访问控制。
