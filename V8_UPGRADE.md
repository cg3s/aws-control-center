> 历史文档：当前仅维护新版 UI 与一套 API，请以 [README](README.md) 的安装、接口和迁移说明为准。

# V8.0 升级说明

## 变化
- 新增 `/api/dashboard/summary`：实例状态、账户/区域分布、任务状态和近 24 小时告警（汇总数据）。
- 新增 React + TypeScript + Ant Design 源码，支持资产看板、实例列表、任务进度自动刷新和只读实例核验。
- `/legacy` 永久保留全功能旧版页面，创建任务及其他配置入口仍在旧版。
- 保留所有 V7 API、任务状态、SQLite 数据及 CLI。无需迁移数据库。

## 安装及升级
1. 完整备份 `data/`、`.env` 与凭据；使用 V8 项目源码替换 V7 源码。
2. 可选：在有 npm 网络访问的环境中执行 `cd frontend && npm install && npm run build`，生成 `static/v8`。编译好的 `static/v8` 必须与后端一起部署。
3. Docker：`docker compose up -d --build`；原生 Linux：`sudo awscc upgrade /path/to/aws-control-center`。
4. 访问 `/` 检查新版前端，`/legacy` 检查旧版全功能界面。

## 已知限制
- 新版 UI 暂不覆盖全部旧版 Web 操作，需要继续使用 `/legacy`。
- 当前交付环境无法连接 npm Registry，React 依赖未安装，尚未完成实际 React 生产构建验证。
- AWS API 汇总接口每次刷新都会执行一次实例扫描，可能较慢，不宜高频轮询；任务列表采用 10 秒轮询。
- 不自动重试 AWS 创建请求，避免付费资源重复创建。
