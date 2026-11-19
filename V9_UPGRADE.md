> 历史文档：当前仅维护新版 UI 与一套 API，请以 [README](README.md) 的安装、接口和迁移说明为准。

# AWS Control Center V9.0 升级说明

## 本次功能

V9 将 React 18 + TypeScript + Ant Design 5 管理端进一步完善，统一接入现有 FastAPI 后端：

- 资源看板、跨区域 Lightsail/EC2 实例列表、单台/批量启停与重启（批量上限 20 台）。
- Lightsail 创建任务：区域实时套餐/镜像/可用区查询、价格预览、二次确认、状态轮询、只读核验与取消尚未提交的任务项。
- 静态 IP 查询与分配/绑定/解绑/释放（释放要求二次输入完整资源名称）。
- 监控规则新增/删除、最新告警列表；假定监控服务已正常运行。
- AssumeRole 账户别名管理、最近 200 条操作审计查询。
- 旧版管理入口 `/legacy` 保留；CLI、三进程（Web / monitor / task worker）以及 SQLite 格式不变。

**本次没有修改数据库结构，也不需要迁移已有数据。** 新前端直接使用已有 API。

## 构建新版前端

需要 Node.js 20.19+ 或 22.12+，且构建机器能够访问 npm Registry：

```bash
cd aws-control-center/frontend
npm install
npm run typecheck
npm run build
```

编译产物会写入 `../static/v8/`：路径保持兼容，尽管 UI 版本已升级至 V9。请勿改变 Vite 的 `/v8/` base，否则对应的 FastAPI 静态资源路由也需要同步修改。

如无构建产物，`/` 仍自动返回旧版静态 HTML；`/legacy` 始终提供旧版页面。**React 源码不等于已构建的生产页面。**

## 升级 Docker

先备份 `.env` 和 `data/`，从项目根目录执行：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail 100
```

Dockerfile 不会安装 Node/npm；请在构建容器镜像前先完成上述前端构建，或继续使用旧版页面。

## 升级原生 Linux/systemd

先备份 SQLite 数据与配置。完成前端构建后，使用现有 CLI 和升级包：

```bash
sudo awscc backup
sudo awscc upgrade /path/to/aws-control-center
sudo awscc status
```

新前端构建产物位于 `static/v8`，CLI 升级逻辑应将 `static/` 一并替换。如未替换，可按 `deploy/install.sh` 文档重新部署，注意保留 `/etc/aws-control-center/awscc.env` 和 `/var/lib/aws-control-center/`。

## 安全与限制

- 仍为单管理员架构，**不是多租户 SaaS，也没有细粒度 RBAC/MFA**。不要不经网关保护直接向公网开放。
- 点击启停/重启、静态 IP 修改或真实创建实例会操作 AWS，并可能产生费用。任务预览只是元数据校验，**不是 AWS 事务性 dry-run**。
- CloudWatch 网络采样不是 Lightsail 实际账单流量；无法据此准确计算套餐剩余额度。
- 前端对创建操作使用两步确认；中断/超时后不得盲目重试。请在 AWS 控制台或通过 `tasks verify` 核实实际状态。
- 本环境后端 29 项测试通过，前端构建因 npm Registry DNS 无法解析而未验证；未使用真实 AWS 账户或 Docker/systemd 进行端到端测试。
