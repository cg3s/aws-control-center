> 历史文档：当前仅维护新版 UI 与一套 API，请以 [README](README.md) 的安装、接口和迁移说明为准。

# V6: durable Lightsail create queue

## Features
- Queue up to 10 Lightsail instances per request, 100 queued globally.
- AWS metadata preview (bundle/blueprint catalog, monthly price estimate); no AWS transactional dry-run.
- Two-step confirmation to create billable resources.
- SQLite durable queue, per-instance states, cancellation of not-yet-started items, audit.
- Separate `aws-task-worker` / `awscc-task-worker.service`.
- No automatic retry after ambiguous AWS create outcome; verify AWS inventory manually before resubmitting.
- Existing V5 Web and CLI supported.

## Docker
`docker compose up -d --build` (only **one** `aws-task-worker` replica).

## Linux native (fresh)
`sudo bash deploy/install.sh --user "$USER"`

## Upgrade from V5 native
`sudo awscc backup && sudo awscc upgrade ./aws-control-center`
**Important:** The V5 `awscc upgrade` command itself does not know the new V6 service.
After that upgrade, run `sudo bash ./aws-control-center/deploy/install.sh --user YOUR_NONROOT_USERNAME`
from the extracted V6 sources to register and start the V6 worker. The installer preserves
existing `/etc/aws-control-center/awscc.env` and `/var/lib/aws-control-center/`.
Check with `sudo awscc status`.

## CLI examples
```bash
sudo awscc tasks create --profile default --region ap-northeast-1 --bundle nano_3_0 --blueprint ubuntu_22_04 --names demo-01,demo-02
# Add --submit to actually create resources (confirmation is interactive); --yes bypasses prompt
sudo awscc tasks create --profile default --region ap-northeast-1 --bundle nano_3_0 --blueprint ubuntu_22_04 --names demo-01,demo-02 --submit
sudo awscc tasks list
sudo awscc tasks show TASK_ID
sudo awscc tasks cancel TASK_ID
````

## IAM
For V6 creation: lightsail:GetBundles, lightsail:GetBlueprints, lightsail:GetInstance, lightsail:CreateInstances, plus existing inventory permissions. Restrict regions/accounts in IAM.

## Limitations
- Price is a Lightsail bundle metadata estimate; not final bill.
- No bulk rollback; AWS creation is asynchronous; "submitted" means AWS accepted request, not running.
- Stale `running` rows are marked failed on worker restart for manual reconciliation; never auto-repeat creates.
- Verify IAM/quota/region/blueprint availability in the AWS account before use.
- No production AWS end-to-end deployment test is claimed.
