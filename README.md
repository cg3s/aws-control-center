# AWS Control Center

单管理员、自托管的 AWS 多账户控制台。只提供一套 React 管理界面与 `/api` 接口；旧 HTML、`/legacy`、旧实例操作接口和旧专用创建队列已移除。

## 功能

- **账户**：本地 AWS Profile、加密 Access Key、AssumeRole；支持使用保存的 Access Key 作为角色来源、账户分组与禁用。
- **资源**：跨账户/区域 EC2 与 Lightsail 清单，按真实账户身份去重，后台缓存、搜索、局部失败反馈。
- **批量任务**：创建、删除、启动、停止、重启，单批最多 100 项；预览固定目标后确认提交，逐项状态及审计。
- **EC2 创建**：按需 Linux、AMI、架构检查、现有子网/安全组、根盘、公网 IP、密钥对、实例角色，以及固定版本的 Launch Template。
- **Lightsail 创建**：区域套餐、Linux 镜像、可用区、密钥对、价格预览。
- **DIY 初始化**：默认用户/普通用户/root 公钥，root 密码哈希，独立 SSH 登录策略，首次启动 Bash 脚本，版本化模板及单次覆盖。
- **SSH 脚本**：加密连接凭据、强制 SHA256 主机指纹、按实例绑定的独立连接、环境变量参数、可选免密 sudo、超时、退出码与加密保存的截断输出。
- **流量**：入站/出站、平均 Mbps、1 小时/1 日/7 日/30 日及自然日/月查询；固定桶去重、迟到数据修正、完整性标识和监控实例排名。
- **自动化**：通知、SSH 脚本、停机；连续超限、恢复阈值、冷却、每日上限、维护期、标签动态匹配、集合汇总阈值、历史模拟、规则暂停和全局紧急暂停。
- **其他**：Lightsail 静态 IP、套餐元数据、TCP 检测与历史、跨账户费用汇总、任务历史与审计。

网络流量是 AWS 指标，**不是账单流量或套餐剩余额度**。停机后磁盘、IP、Lightsail 套餐等费用可能继续产生。删除实例默认不释放独立 IP、磁盘和快照，EC2 按资源原有磁盘删除策略执行，尊重终止保护。

## 启动

### Docker

```bash
cp .env.example .env
# 编辑 APP_PASSWORD，至少 16 位；按需配置 AWS_REGIONS、Telegram、COOKIE_SECURE。
docker compose up -d --build
```

Docker 镜像直接打包仓库中的 `static/app/` 预编译前端。部署不需要 Node.js，也不执行 npm 安装或构建。发布和上传时请包含整个 `static/app/`。修改前端后，在开发机或 CI 执行下方的前端构建命令，将新产物随代码一起部署。

Web 默认仅映射到 `127.0.0.1:8000`，可通过已有 HTTPS 反代访问。纯本机 HTTP 测试需设置 `COOKIE_SECURE=false`。

首次启动后，在“ AWS 账户”添加凭据。共享 Profile 可另行只读挂载，Web Access Key 账户不依赖 `.aws` 目录。

### 原生 Linux / aaPanel

```bash
cd frontend
npm ci
npm run typecheck
npm run build
cd ..
sudo bash deploy/install.sh --user "$USER"
sudo awscc status
sudo awscc health
```

源代码与运行目录分开。升级前在源目录完成前端构建，再执行：

```bash
sudo awscc upgrade /path/to/aws-control-center
```

安装器要求存在 `static/app/index.html`。没有构建产物时主页返回 503，不再回退旧 UI。

服务是 `awscc-web`、`awscc-worker`、`awscc-task-worker`。工作进程均需要同一数据目录、配置和加密密钥。不要额外运行旧版 Worker。

## 使用流程

1. 添加 AWS 账户，验证身份；等待后台清单更新，或请求刷新清单。
2. 在创建页选择账户、区域与服务，填写配置、模板或脚本。
3. 核对预览中的账户、区域、实例及关联磁盘策略，输入 `EXECUTE`；删除需输入 `DELETE`。
4. 在任务中心查看 AWS 提交与最终资源核验。实例运行不代表初始化已完成，可配置 SSH 后执行“核验初始化”。
5. 创建 SSH 连接时，通过可信渠道核对主机指纹；私钥/密码只在提交时传送，不回显。复制连接版本时需重新提供凭据。
6. 在监控规则选择实例、窗口和阈值。默认只通知；明确选择自动执行模式后，按动作顺序执行脚本和停机。

同名规则保存新版本时会停用旧版本；任务已保存配置快照。全局暂停阻止未开始的自动化动作，不会终止已经运行的远程命令。

## 监控与可靠性

- 默认 5 分钟采样；已开启详细监控的 EC2 可选择 1 分钟。不会自动开启付费详细监控。监控 Worker 默认每 5 分钟评估一次（可设置 MONITOR_INTERVAL_SECONDS，最低 60 秒），预留 10 分钟指标延迟。
- 首次采集回填最近 30 天及当月可用数据；1 分钟数据首次回填限制为 14 天。回填后仍有缺失的窗口不会执行自动资源修改。
- 最近 2 小时重复拉取并更新同一个时间桶，不能累加重叠窗口。原始数据保留 14 天，小时聚合 90 天，日聚合 730 天。
- 已知创建时间的实例从创建后的完整采样周期计算覆盖率；外部 EC2 不使用可能随开机变化的 LaunchTime 假定创建时间。
- 默认连续两次完整评估超限触发；冷却 3,600 秒，恢复阈值为触发值的 90%，每条规则每天最多触发 3 次，可修改。
- 持续超限默认只触发一次；滑动窗口恢复后重置，自然日/月规则在周期切换后重新布防。
- 自动化触发事件先落库，通过固定事件标识关联任务；丢失回执后复用同一任务。Telegram 通知最多尝试 5 次，不阻塞实例动作。
- 修改 AWS 资源的 SDK 调用禁止自动重放。超时、服务端不明确错误或 Worker 租约中断进入 `unknown`，先核验再决定是否新建任务。
- EC2 创建使用稳定 ClientToken；Lightsail 使用任务标签核验归属。重启核验只证明 AWS 请求被接受，结果标注 `request_accepted`。
- 任务 Worker 默认最多 5 个执行线程，SSH 最多 2 个并行。自动化同一任务的步骤顺序执行，脚本超时后仍可继续停机。

`control.db` 保存账户、配置、任务与审计；`metrics.db` 保存指标。连接凭据、模板和任务执行配置使用现有 Fernet 密钥加密。root 明文密码不写入任务，启动数据仍会传给 AWS；不要将长期秘密写入自定义脚本。

## API 与 CLI

主要接口：

| 接口 | 用途 |
| --- | --- |
| `/api/login`、`/api/session`、`/api/logout` | 登录、会话与 CSRF |
| `/api/key-accounts`、`/api/accounts`、`/api/account-settings` | 凭据、角色、分组和启用状态 |
| `/api/resources`、`/api/resources/refresh` | 缓存查询与请求刷新 |
| `/api/catalog` | 区域创建选项 |
| `/api/tasks/preview`、`/api/tasks` | 固定预览与确认入队 |
| `/api/tasks/{id}`、`cancel`、`verify` 子路径 | 任务详情、取消与核验 |
| `/api/configurations?kind=bootstrap\|script\|connection\|rule` | 版本化配置 |
| `/api/traffic`、`/api/traffic/summary` | 单实例流量与监控资源汇总 |
| `/api/events`、`/api/rules/{id}/simulate` | 触发历史与模拟 |
| `/api/automation/pause?paused=true` | 全局暂停 |
| `/api/health`、`/api/audit`、`/api/cost` | 状态、审计与成本 |

所有 API 需要登录；修改操作需要 `X-AWSCC-CSRF`。费用可使用 `profile=all`，清单使用 `profiles` 逗号列表。前端轮询缓存和任务，不持续调用全量 AWS 扫描。

原生 CLI 与 Web 复用同一服务层，使用服务用户身份：

```bash
sudo awscc instances list --profile all --json
sudo awscc tasks list --json
sudo awscc tasks show TASK_ID --json
sudo awscc tasks verify TASK_ID --yes
sudo awscc tasks cancel TASK_ID --yes
sudo awscc configurations list --kind rule
sudo awscc rules save --file rule.json --yes
sudo awscc rules disable RULE_ID --yes
sudo awscc traffic report --profile default --region us-east-1 --service ec2 --instance i-EXAMPLE --window 7d
sudo awscc automation pause --yes
sudo awscc automation resume --yes
sudo awscc automation events
sudo awscc cost report --profile all
```

批量任务 JSON 示例，先保存为 `task.json`：

```json
{
  "request_key": "replace-with-a-unique-uuid",
  "items": [{
    "kind": "stop",
    "target": {"profile": "default", "region": "us-east-1", "service": "lightsail", "instance": "my-server"}
  }]
}
```

执行 `sudo awscc tasks preview --file task.json`，将返回的 `preview_id` 和 `confirm: "EXECUTE"` 加入同一文件，保留原 `request_key`，再执行 `sudo awscc tasks submit --file task.json --yes`。预览 15 分钟有效；请求不明确时保留标识，检查任务而不是换标识重试。

## IAM 权限与网络

按使用的功能分别授权，避免直接使用 AdministratorAccess：

- 身份：`sts:GetCallerIdentity`，角色账户另需 `sts:AssumeRole` 与目标信任关系。
- EC2：实例、镜像、规格、子网、VPC、安全组、密钥对、启动模板及实例属性的 Describe 权限；按需授予 `RunInstances`、`CreateTags`、`StartInstances`、`StopInstances`、`RebootInstances`、`TerminateInstances`。
- 实例角色需要针对指定角色的 `iam:PassRole`；主机 SSH 脚本不使用面板的 AWS 密钥。
- Lightsail：实例、区域、套餐、镜像、密钥对和指标查询；按需授权创建、删除、启停、重启及静态 IP 操作。
- 流量：EC2 需要 `cloudwatch:GetMetricStatistics`；Lightsail 需要 `lightsail:GetInstanceMetricData`。
- 成本：`ce:GetCostAndUsage` 与账户账单访问能力。

权限错误会显示在清单、预览、任务或采集详情中。创建预览仅验证配置与元数据，不能保证提交时的配额、容量或所有写入权限。

SSH 必须从面板服务器到实例可达，安全组及主机防火墙应允许相应来源。仅接受 AWS 返回的实例地址，拒绝回环、链路本地、组播等地址，主机指纹变化会阻止执行。

## 升级、备份和历史数据

升级前备份配置、数据库和加密密钥。原生部署执行：

```bash
sudo awscc backup
```

备份位于 `/var/backups/aws-control-center/`，包含两份 SQLite 一致性备份、`credential.key` 和配置，文件权限为 0600。使用环境变量加密密钥时必须保留对应配置。Docker 部署可停止三个服务后备份 `.env` 和整个 `data/`，再启动服务。

升级采用新增表迁移，不删除已有记录：

- 旧创建任务在新任务中心显示为 `archived`，保留原提交状态；不会自动继续旧版尚未完成的创建请求。
- 旧监控规则由新 Worker 验证资源后一次性导入，仅通知，不启用自动资源动作。无法访问的实例以禁用规则导入并标记复核，原数据仍保留；重新启用时再次验证账户和实例身份。
- 旧一小时滑动窗口采样不进入新日/月累计，避免重复统计。
- 旧告警和采样通过新事件接口提供历史记录，不恢复旧 API 或页面。

回退需要停止全部服务，恢复匹配版本的代码、数据库、密钥和配置；不要将旧 Worker 指向已经运行新任务的数据库。

## 开发验证

```bash
python -m pip install -r requirements.txt pytest httpx
python -m pytest -q
cd frontend
npm ci
npm run typecheck
npm run build
```

CI 执行后端测试、前端类型检查和生产构建，以及 Linux 脚本语法检查。测试使用临时数据库和模拟 AWS/SSH，不创建真实实例。

本次实现完成本地接口与生产构建验证；真实 AWS 创建/删除、Linux 各发行版 cloud-init、Docker/systemd/aaPanel 部署及真实 SSH 连通性仍需目标环境联调。浏览器控制服务在本次环境不可用，未完成交互式浏览器验收。

当前支持单管理员和单机部署；没有团队 RBAC、Spot、自动创建网络、进程级流量 Agent、自动创建/删除/替换实例规则。规则支持显式实例集合或标签动态匹配，可逐实例或按集合汇总判断；每条规则最多 100 台，触发时冻结目标。
