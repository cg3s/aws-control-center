> 历史文档：当前仅维护新版 UI 与一套 API，请以 [README](README.md) 的安装、接口和迁移说明为准。

# AWS Control Center V7.0 — catalog and read-only verification

## What changed

- Regional Lightsail bundle/blueprint catalogs and availability zones (Web and `awscc catalog`). AWS API data is queried live; prices are **catalog monthly estimates**, not bills.
- Per-instance AWS verification after a create task, via task detail button, `POST /api/tasks/{id}/verify`, or `awscc tasks verify TASK_ID`.
- Separate verification values (`unchecked`, `running`, `found`, `not_found`, `unknown`) persisted in SQLite. A task's original AWS request status (`submitted`, `failed`, etc.) is **never overwritten by verification**.
- Non-retrying, on-demand verification: the system does **not** automatically create or replace anything during verification. A 404 can reflect eventual consistency, not definitive non-creation.
- Existing Docker, systemd, AWS credential profiles and database layout remain supported.

## Docker upgrade from V6

```bash
# From the V6 project directory, back up everything first
cp -a data data.backup.$(date +%Y%m%d-%H%M%S)
# Extract the V7 archive elsewhere, then replace source files; retain .env and data/
docker compose up -d --build
docker compose ps
```

## Native systemd upgrade from V6

```bash
sudo awscc backup
sudo awscc upgrade /path/to/extracted/aws-control-center
sudo awscc status
```

V6 already registered the `awscc-task-worker.service`, so no new unit is required for V7.

## CLI

```bash
sudo awscc catalog --profile default --region ap-northeast-1 --json
sudo awscc tasks list
sudo awscc tasks show TASK_ID --json
sudo awscc tasks verify TASK_ID --json
```

`tasks verify` only checks items with status `submitted` or `failed`. Failed items are included because AWS could have created the resource despite a timeout. Check the AWS console/CloudTrail before issuing another paid request. Verification records when and what it observed; `found` is not proof of this task being the creator.

## IAM additions

The read-only catalog uses `lightsail:GetBundles`, `lightsail:GetBlueprints` and `lightsail:GetRegions`. Verification uses `lightsail:GetInstance`. See AWS IAM service authorization reference before assigning policies; grant least privilege.

## Testing

```bash
python -m pytest -q
```

V7 has mock-based tests for paginated catalog lookup, running instance verification, ambiguous create outcome, auth guardrails and CLI commands. No real AWS account, container runtime or production host was used in tests.

## Out of scope

This update does not implement the previously proposed React/Ant Design Pro migration, automatic readiness polling, automatic IP rotation, or accurate billable monthly Lightsail traffic usage. The existing Web UI remains vanilla JavaScript. For internet-facing use, add MFA/SSO, fine-grained permissions, and upstream access controls before production use.
