# AWS Control Center V9.1 — 网页管理 Access Keys

## 核心变化

- 从 Web 管理后台 → 账户管理 → **Web Access Key 凭据账户**，填写别名、Access Key ID 和 Secret Access Key。
- 保存前通过 AWS STS `GetCallerIdentity` 验证身份；验证失败不持久化。
- 通过 **激活** 按钮设置默认 Web 账户，切换后其他模块可以使用该账户管理 EC2、Lightsail、账单等。激活值保存于 SQLite，并在重新登录后恢复。
- Secret Key 使用 Fernet 对称认证加密保存，API 仅返回 Key ID 最后四位；不在页面、审计日志显示秘密。
- 已有 `.aws` Profile / IAM Role 方式仍支持，但**不再是 Web 账号使用的必要条件**。
- 后台任务与监控仍使用其创建时明确指定的 profile，不会因为之后激活别的账户而改变。

## Docker 首次部署（无需 ~/.aws）

```bash
unzip aws-control-center-v9.1.zip
cd aws-control-center
cp .env.example .env
# 编辑 .env 设置 APP_PASSWORD（至少 16 位）、COOKIE_SECURE=true（HTTPS）
docker compose up -d --build
```

Docker Compose 已移除强制的 `${HOME}/.aws` 挂载。EC2/Lightsail 运行时 IAM Role 或环境凭据依然可用。若需要旧 Profile，可额外通过 Compose override 挂载只读 `.aws` 目录。

## Linux systemd 直接部署

```bash
sudo bash deploy/install.sh --user "$USER"
sudo awscc status
```

之后 HTTPS 访问 Web → 账户管理添加 AWS Keys，不要求先创建 `~/.aws/credentials`。

## 安全及备份

- 密钥加密用 `DATA_DIR/credential.key`（首次保存时创建，文件模式 0600），Web 服务和两类 Worker 必须共享同一 `DATA_DIR`；也可显式设置 `AWSCC_ENCRYPTION_KEY`（Fernet 格式）。
- **数据库 `control.db` 与 `credential.key` 必须同时备份**，否则恢复后原密钥无法解密；如果使用环境密钥，也必须安全保存环境密钥。Linux `sudo awscc backup` 已会备份 `.db` 和 `.key`（如有）。
- SQLite 加密的是保存的 Secret Key 字段，不是整个数据库；Access Key ID、AWS 账户 ID 与 ARN 以明文存储。数据库与密钥文件放在一起无法抵御主机完全失陷；应使用受控的权限、加密磁盘及隔离备份。生产场景可进一步接入 AWS Secrets Manager / KMS。
- 所有新增/激活/删除/验证 API 都要求登录且写操作要求 CSRF；请务必使用 HTTPS、长管理员密码和限制管理后台访问来源。
- 推荐最小权限 IAM 用户，禁用不必要的写入 API，不要使用 AWS 根账户 Access Key。尽可能使用短期凭据或 IAM Role。
- 删除面板中的账号不会删除 AWS IAM Key。如需真正吊销，应到 AWS IAM 控制台停用或删除该 Key。
- 激活账户是**当前单管理员系统的全局默认选择**，已有单次请求和已排队任务不会随之切换；未来多用户系统需改为按用户/会话隔离。

## API

- `GET /api/key-accounts` — 已存账户（不返回秘密）
- `POST /api/key-accounts` — STS 验证并存储 `{alias, access_key_id, secret_access_key}`
- `POST /api/key-accounts/{alias}/verify` — 重新验证 AWS 身份
- `POST /api/key-accounts/activate` — 设置当前账户 `{alias}`（兼容 Profile 和 Role）
- `DELETE /api/key-accounts/{alias}` — 删除面板凭据；仍存在关联监控或排队任务时拒绝删除
- `GET /api/config` — 返回 `profiles` 及 `active_profile`

## 升级

现有 V9 数据库会自动创建新表 `key_accounts` 和 `app_settings`，无破坏性迁移。升级前先备份数据及配置。Docker 重新构建镜像，原生安装使用 `sudo awscc upgrade /path/to/aws-control-center`。升级后如果开启了反代 HTTPS，确保 `COOKIE_SECURE=true`。

**验证状态**：自动化测试覆盖密钥验证、加密存储、激活及删除保护；未使用真实 AWS 凭据执行端到端请求。React 需要在可连接 npm 的环境进行构建验证；旧版 `/legacy` 页面已经加入凭据操作入口。
