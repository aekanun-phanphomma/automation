# Auto Stop/Start — VM & Database Scheduler

Tag-based automation to stop and start VMs and managed databases on AWS and Azure.
Designed to be called by a cron job, Lambda, Azure Function, or Jenkins pipeline.

---

## Directory Structure

```
auto-stop-start/
├── main.py                 # CLI entry point
├── config.yaml             # Resource tags, schedules, environment config
├── requirements.txt
├── core/
│   ├── config.py           # YAML loader with ${ENV_VAR} expansion
│   ├── logger.py           # Structured JSON logger
│   ├── scheduler.py        # Cron-based schedule evaluation
│   └── notifier.py         # Slack webhook + SNS notifications
└── providers/
    ├── base.py             # Abstract base class
    ├── aws.py              # EC2 + RDS (single instances + Aurora clusters)
    └── azure.py            # Azure VMs + PostgreSQL/MySQL Flexible Servers
```

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials (never put them in config.yaml)

# AWS — use any standard boto3 method:
export AWS_PROFILE=my-profile        # or IAM role if running on EC2/Lambda

# Azure — DefaultAzureCredential picks up:
export AZURE_SUBSCRIPTION_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
az login                             # or set AZURE_CLIENT_ID / SECRET / TENANT for service principal

# 3. Tag your resources
#    EC2 / RDS:   add tag  auto-schedule=true  +  Environment=dev
#    Azure VM/DB: add tag  auto-schedule=true  +  Environment=dev

# 4. Run
python main.py --action stop  --env dev --provider all --resource-type all
python main.py --action start --env dev --provider aws --resource-type vm
python main.py --action status --env prod

# Dry-run (no changes):
python main.py --action stop --env dev --dry-run
```

---

## CLI Reference

```
python main.py [OPTIONS]

Required:
  --action   {start,stop,status}    Operation to perform
  --env      ENV_NAME               Environment from config.yaml

Optional:
  --provider   {aws,azure,all}      Cloud provider (default: all)
  --resource-type {vm,db,all}       Resource type (default: all)
  --dry-run                         Simulate — no real changes
  --schedule-check                  Skip if outside cron schedule window
  --config     PATH                 Config file path (default: ./config.yaml)
  --output-json                     Print results as JSON (for CI/Lambda)
```

---

## How Resource Discovery Works

Resources are discovered **by tags**, not by hardcoded IDs.
Add these two tags to any resource you want to manage:

| Tag key        | Tag value       | Purpose                      |
|----------------|-----------------|------------------------------|
| `auto-schedule`| `true`          | Opts the resource in         |
| `Environment`  | `dev` / `staging` / `prod` | Matches env in config |

Resources **without** these tags are ignored completely.

### Exclusion by name prefix

You can exclude specific resources even if they have the opt-in tags by listing
name prefixes in `config.yaml`:

```yaml
ec2:
  exclude_name_prefixes:
    - "bastion"
    - "jenkins"
```

---

## Schedule Gate (`--schedule-check`)

When you pass `--schedule-check`, the script checks whether the current time
falls inside the cron window defined for the environment before doing anything.

```yaml
schedules:
  office-hours-th:
    timezone: Asia/Bangkok
    start:
      cron: "0 8 * * 1-5"    # Mon–Fri 08:00
    stop:
      cron: "0 20 * * 1-5"   # Mon–Fri 20:00
```

The window is ±10 minutes around the cron fire time — so if your cron job
runs every 5 minutes, it will always catch the right window.

**Recommended cron job setup:**

```cron
# /etc/cron.d/auto-stop-start

# Evaluate stop schedule every 5 min — only fires when inside the window
*/5 * * * * ubuntu cd /opt/auto-stop-start && python main.py --action stop  --env dev --provider all --schedule-check >> /var/log/auto-stop.log 2>&1
*/5 * * * * ubuntu cd /opt/auto-stop-start && python main.py --action start --env dev --provider all --schedule-check >> /var/log/auto-stop.log 2>&1
```

---

## AWS Setup

### Permissions required

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:StartInstances",
        "ec2:StopInstances"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "rds:DescribeDBInstances",
        "rds:DescribeDBClusters",
        "rds:StartDBInstance",
        "rds:StopDBInstance",
        "rds:StartDBCluster",
        "rds:StopDBCluster",
        "rds:ListTagsForResource"
      ],
      "Resource": "*"
    }
  ]
}
```

### Running on Lambda

```python
import subprocess, sys

def handler(event, context):
    action = event.get("action", "stop")
    env    = event.get("env", "dev")
    subprocess.run(
        [sys.executable, "main.py", "--action", action, "--env", env, "--output-json"],
        check=True,
    )
```

---

## Azure Setup

### Credentials (choose one)

```bash
# Option 1 — Azure CLI (local dev)
az login

# Option 2 — Service principal
export AZURE_TENANT_ID=...
export AZURE_CLIENT_ID=...
export AZURE_CLIENT_SECRET=...

# Option 3 — Managed Identity (when running on Azure VM / Function)
# No env vars needed — DefaultAzureCredential picks it up automatically.
```

### Required RBAC roles

| Scope | Role |
|-------|------|
| Resource group containing VMs | `Virtual Machine Contributor` |
| Resource group containing DBs | `Contributor` |

---

## Supported Resource Types

| Provider | Type | Notes |
|----------|------|-------|
| AWS | EC2 instances | All instance families |
| AWS | RDS single instances | MySQL, PostgreSQL, MariaDB, Oracle, SQL Server |
| AWS | Aurora clusters | Set `include_aurora_clusters: true` |
| Azure | Virtual Machines | Uses `deallocate` (no billing) not just power-off |
| Azure | PostgreSQL Flexible Server | `postgresql_flexible` engine |
| Azure | MySQL Flexible Server | `mysql_flexible` engine |

> **Azure VM note:** `stop` triggers `deallocate`, which releases compute resources
> and stops billing. A simple power-off (without deallocate) still incurs charges.

---

## Output

Every run prints structured JSON log lines:

```json
{"ts": "2024-04-25T08:00:01+00:00", "level": "INFO", "logger": "providers.aws", "message": "Stopped EC2 dev-app-01 (i-0abc123)"}
{"ts": "2024-04-25T08:00:02+00:00", "level": "INFO", "logger": "__main__", "message": "Run summary", "succeeded": 4, "failed": 0, "skipped": 1}
```

Use `--output-json` to get a machine-readable list of all resource results — useful
for Lambda return values or CI pipeline assertions.

---

## Notifications

Configure in `config.yaml` under `notifications:`.

```yaml
notifications:
  webhook:
    enabled: true
    url: "${NOTIFY_WEBHOOK_URL}"   # Slack / Teams incoming webhook
    on: error                      # error | success | all
  sns:
    enabled: false
    topic_arn: "${SNS_TOPIC_ARN}"
    on: error
```

---

## Adding a New Provider

1. Create `providers/yourcloud.py` inheriting from `providers.base.CloudProvider`
2. Implement `manage_vms(action, dry_run)` and `manage_databases(action, dry_run)`
3. Return result dicts using `self._ok()`, `self._skip()`, `self._err()` helpers
4. Register it in `main.py` → `_build_providers()`
