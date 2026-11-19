> 历史文档：当前仅维护新版 UI 与一套 API，请以 [README](README.md) 的安装、接口和迁移说明为准。

# 实例流量与创建配置更新

- 实例管理增加“查看流量”：EC2 和 Lightsail 可查询最近 1 / 7 / 30 天入站、出站、合计与每日汇总（UTC，GiB）。空指标表示无数据，不表示计费为零。指标不是账单流量或 Lightsail 套餐剩余配额。
- 区域下拉框、实例、监控、任务与审计增加中文名称，例如 `ap-northeast-1 · 日本（东京）`。未知区域保留原代码。
- 创建任务采用独立确认弹窗，预览使用 `dry_run=true`，确认使用 `dry_run=false, confirm=CREATE`。只有返回有效任务 ID 和 `queued` 才显示“已入队”。界面会显示任务 ID，避免残留预览 JSON 被误认为最终结果。
- 后端拒绝 `dry_run=true` 与 `confirm=CREATE` 同时出现的矛盾请求；校验镜像和套餐时支持 AWS 分页。

## 新建 Linux 实例

在“创建任务”中填写套餐和系统后，可配置：

1. **Lightsail SSH 密钥对名称**：已有的同账户、同区域密钥对名称，传递给 AWS `keyPairName`；留空使用 AWS 区域默认密钥。此密钥用于镜像默认用户。
2. **额外 SSH 公钥**：粘贴 OpenSSH 公钥，每行一条；同时勾选“允许 root 直接 SSH 登录”，公钥添加到 root。不要上传私钥。
3. **root 密码**：可选，至少 16 位。设置密码本身不打开 SSH 密码认证；还需勾选 root SSH。
4. **允许 root 直接 SSH 登录**：必须提供额外公钥或 root 密码。设置 `PermitRootLogin yes`；填写密码时也启用 `PasswordAuthentication yes`。保留已有普通用户配置。
5. **启动脚本**：粘贴 Bash 脚本或所需的一键安装命令。仅在新实例首次启动时以 root 执行，位于 root SSH 初始化之后。例如：

```bash
#!/bin/bash
set -e
apt-get update
apt-get install -y curl
# 在此添加你确认要执行的一键安装命令
```

以上通过 Linux cloud-init 完成；不支持 Windows root 配置，也不会修改已有实例。自定义脚本可以再次改变 SSH 设置。Lightsail TCP 22 防火墙、主机防火墙和 SSH 服务必须可用，才能 `ssh root@公网IP`。建议限制 22 端口的访问来源。

root 密码转换为 SHA-512 crypt 哈希后用于 cloud-init，不将明文密码写入启动数据。完整启动配置（可能包含脚本内的秘密）复用现有 Fernet 密钥加密保存在任务队列。任务详情和审计不返回密码、脚本或密文。请同时备份数据库与 `credential.key`（或配置的环境加密密钥）。启动数据会发送给 AWS，实例内 cloud-init 文件应只允许管理员访问。

## aaPanel 原生部署升级

将本次完整源码解压到 `/opt/awscc-source`（和运行目录分开），执行：

```bash
cd /opt/awscc-source/frontend
npm install
npm run build
cd ..
sudo awscc upgrade /opt/awscc-source
sudo awscc status
sudo awscc health
```

升级命令会先备份数据库和凭据密钥，安装新增的 `passlib` 依赖，更新前后端并重启三个服务；保留 `/etc/aws-control-center/awscc.env` 和数据。环境配置需单独备份。若尚未安装原生服务，参见 `aapanel_native_install.md`。

EC2 流量需要额外授权 `cloudwatch:GetMetricStatistics`，Lightsail 沿用 `lightsail:GetInstanceMetricData`。创建仍需 `lightsail:CreateInstances` 等现有权限。

任务状态“completed”代表所有创建请求已被 AWS 接受，不证明实例或初始化脚本已成功运行。使用任务“核验”检查 AWS 状态，必要时在实例查看 `/var/log/cloud-init-output.log`。若任务长时间 queued，检查 `sudo awscc task-logs`。对于超时或结果不明确的请求，先核验 AWS 中是否已存在该名称，避免重复创建。

实现参考：[Lightsail CreateInstances](https://docs.aws.amazon.com/lightsail/2016-11-28/api-reference/API_CreateInstances.html)、[cloud-init 用户和 SSH 配置](https://docs.cloud-init.io/en/latest/reference/modules.html)。
