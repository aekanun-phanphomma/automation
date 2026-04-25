"""
AWS provider — manages EC2 instances and RDS database instances/clusters.

Authentication: uses the standard boto3 credential chain.
  Priority order: env vars → ~/.aws/credentials → IAM instance role → ECS task role
  Never put credentials in this file or in config.yaml.

Required IAM permissions (minimum):
  EC2 : ec2:DescribeInstances, ec2:StartInstances, ec2:StopInstances
  RDS : rds:DescribeDBInstances, rds:StartDBInstance, rds:StopDBInstance
        rds:DescribeDBClusters, rds:StartDBCluster, rds:StopDBCluster (Aurora)
        rds:ListTagsForResource
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError

from core.logger import get_logger
from providers.base import CloudProvider

logger = get_logger(__name__)

# Maximum attempts for transient API errors
_MAX_RETRIES = 3
_RETRY_BACKOFF = [1, 3, 7]  # seconds


def _with_retry(fn, *args, **kwargs):
    """Call fn with simple exponential backoff on transient AWS errors."""
    for attempt, wait in enumerate(_RETRY_BACKOFF, start=1):
        try:
            return fn(*args, **kwargs)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("Throttling", "RequestLimitExceeded", "ServiceUnavailable"):
                if attempt == _MAX_RETRIES:
                    raise
                logger.warning("AWS throttle (%s), retry %d/%d in %ds", code, attempt, _MAX_RETRIES, wait)
                time.sleep(wait)
            else:
                raise


class AWSProvider(CloudProvider):

    def __init__(self, config: dict, env: str) -> None:
        super().__init__(config, env)
        self.regions: list[str] = config.get("regions", ["us-east-1"])
        self.ec2_cfg:  dict     = config.get("ec2", {})
        self.rds_cfg:  dict     = config.get("rds", {})

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _session(self, region: str) -> boto3.Session:
        return boto3.Session(region_name=region)

    def _tag_filters(self, tag_list: list[dict]) -> list[dict]:
        """Convert config tag list → boto3 Filters format."""
        return [{"Name": f"tag:{t['key']}", "Values": [t["value"]]} for t in tag_list]

    def _is_excluded(self, name: str, exclude_prefixes: list[str]) -> bool:
        name_lower = (name or "").lower()
        return any(name_lower.startswith(p.lower()) for p in exclude_prefixes)

    def _get_instance_name(self, tags: list[dict]) -> str:
        for t in tags or []:
            if t["Key"] == "Name":
                return t["Value"]
        return ""

    # ── EC2 ───────────────────────────────────────────────────────────────────

    def _ec2_instances_in_region(self, region: str) -> list[dict]:
        ec2      = self._session(region).client("ec2")
        filters  = self._tag_filters(self.ec2_cfg.get("tag_filters", []))
        filters += [{"Name": "instance-state-name", "Values": ["running", "stopped"]}]

        instances = []
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate(Filters=filters):
            for reservation in page["Reservations"]:
                instances.extend(reservation["Instances"])
        return instances

    def _manage_ec2_region(self, region: str, action: str, dry_run: bool) -> list[dict]:
        ec2      = self._session(region).client("ec2")
        results  = []
        excludes = self.ec2_cfg.get("exclude_name_prefixes", [])

        try:
            instances = _with_retry(self._ec2_instances_in_region, region)
        except ClientError as exc:
            logger.error("EC2 describe_instances failed in %s: %s", region, exc)
            return [self._err(f"ec2:{region}", "vm", action, str(exc))]

        for inst in instances:
            iid   = inst["InstanceId"]
            state = inst["State"]["Name"]
            name  = self._get_instance_name(inst.get("Tags", []))
            label = f"{name} ({iid})" if name else iid

            if self._is_excluded(name, excludes):
                logger.info("Skipping excluded EC2 %s", label)
                results.append(self._skip(iid, "vm", action, f"excluded prefix: {name}"))
                continue

            if action == "status":
                results.append(self._ok(iid, "vm", "status", f"state={state} name={name} region={region}"))
                continue

            if action == "stop" and state != "running":
                results.append(self._skip(iid, "vm", action, f"already {state}"))
                continue

            if action == "start" and state != "stopped":
                results.append(self._skip(iid, "vm", action, f"already {state}"))
                continue

            try:
                if dry_run:
                    logger.info("[DRY-RUN] Would %s EC2 %s", action, label)
                    results.append(self._ok(iid, "vm", action, f"dry-run {action} region={region}"))
                    continue

                if action == "stop":
                    _with_retry(ec2.stop_instances, InstanceIds=[iid])
                    logger.info("Stopped EC2 %s", label)
                else:
                    _with_retry(ec2.start_instances, InstanceIds=[iid])
                    logger.info("Started EC2 %s", label)

                results.append(self._ok(iid, "vm", action, f"region={region} name={name}"))

            except ClientError as exc:
                logger.error("EC2 %s %s failed: %s", action, label, exc)
                results.append(self._err(iid, "vm", action, str(exc)))

        return results

    def manage_vms(self, action: str, dry_run: bool) -> list[dict]:
        all_results = []
        with ThreadPoolExecutor(max_workers=len(self.regions)) as ex:
            futures = {ex.submit(self._manage_ec2_region, r, action, dry_run): r for r in self.regions}
            for future in as_completed(futures):
                try:
                    all_results.extend(future.result())
                except Exception as exc:  # noqa: BLE001
                    region = futures[future]
                    logger.error("EC2 worker for region %s raised: %s", region, exc, exc_info=True)
        return all_results

    # ── RDS ───────────────────────────────────────────────────────────────────

    def _get_rds_tags(self, rds_client, resource_arn: str) -> dict:
        try:
            resp = _with_retry(rds_client.list_tags_for_resource, ResourceName=resource_arn)
            return {t["Key"]: t["Value"] for t in resp.get("TagList", [])}
        except ClientError:
            return {}

    def _tags_match(self, resource_tags: dict, filter_list: list[dict]) -> bool:
        return all(resource_tags.get(f["key"]) == f["value"] for f in filter_list)

    def _manage_rds_region(self, region: str, action: str, dry_run: bool) -> list[dict]:
        rds      = self._session(region).client("rds")
        results  = []
        filters  = self.rds_cfg.get("tag_filters", [])

        # ── RDS instances (single) ───────────────────────────────────────────
        paginator = rds.get_paginator("describe_db_instances")
        try:
            for page in paginator.paginate():
                for db in page["DBInstances"]:
                    db_id    = db["DBInstanceIdentifier"]
                    db_arn   = db["DBInstanceArn"]
                    state    = db["DBInstanceStatus"]
                    engine   = db["Engine"]

                    # Skip cluster members — managed at cluster level
                    if db.get("DBClusterIdentifier"):
                        continue

                    tags = self._get_rds_tags(rds, db_arn)
                    if not self._tags_match(tags, filters):
                        continue

                    if action == "status":
                        results.append(self._ok(db_id, "db", "status", f"state={state} engine={engine} region={region}"))
                        continue

                    if action == "stop" and state != "available":
                        results.append(self._skip(db_id, "db", action, f"already {state}"))
                        continue
                    if action == "start" and state != "stopped":
                        results.append(self._skip(db_id, "db", action, f"already {state}"))
                        continue

                    try:
                        if dry_run:
                            logger.info("[DRY-RUN] Would %s RDS %s (%s)", action, db_id, engine)
                            results.append(self._ok(db_id, "db", action, f"dry-run region={region}"))
                            continue

                        if action == "stop":
                            _with_retry(rds.stop_db_instance, DBInstanceIdentifier=db_id)
                        else:
                            _with_retry(rds.start_db_instance, DBInstanceIdentifier=db_id)

                        logger.info("%s RDS instance %s", action.capitalize() + "ed", db_id)
                        results.append(self._ok(db_id, "db", action, f"engine={engine} region={region}"))

                    except ClientError as exc:
                        logger.error("RDS %s %s failed: %s", action, db_id, exc)
                        results.append(self._err(db_id, "db", action, str(exc)))

        except ClientError as exc:
            logger.error("RDS describe_db_instances failed in %s: %s", region, exc)
            results.append(self._err(f"rds:{region}", "db", action, str(exc)))

        # ── Aurora clusters ──────────────────────────────────────────────────
        if self.rds_cfg.get("include_aurora_clusters", False):
            try:
                paginator_c = rds.get_paginator("describe_db_clusters")
                for page in paginator_c.paginate():
                    for cluster in page["DBClusters"]:
                        cid    = cluster["DBClusterIdentifier"]
                        c_arn  = cluster["DBClusterArn"]
                        state  = cluster["Status"]
                        engine = cluster["Engine"]

                        tags = self._get_rds_tags(rds, c_arn)
                        if not self._tags_match(tags, filters):
                            continue

                        if action == "status":
                            results.append(self._ok(cid, "db", "status", f"cluster state={state} engine={engine} region={region}"))
                            continue

                        if action == "stop" and state != "available":
                            results.append(self._skip(cid, "db", action, f"cluster already {state}"))
                            continue
                        if action == "start" and state != "stopped":
                            results.append(self._skip(cid, "db", action, f"cluster already {state}"))
                            continue

                        try:
                            if dry_run:
                                logger.info("[DRY-RUN] Would %s Aurora cluster %s", action, cid)
                                results.append(self._ok(cid, "db", action, f"dry-run cluster region={region}"))
                                continue

                            if action == "stop":
                                _with_retry(rds.stop_db_cluster, DBClusterIdentifier=cid)
                            else:
                                _with_retry(rds.start_db_cluster, DBClusterIdentifier=cid)

                            logger.info("%s Aurora cluster %s", action.capitalize() + "ed", cid)
                            results.append(self._ok(cid, "db", action, f"cluster engine={engine} region={region}"))

                        except ClientError as exc:
                            logger.error("Aurora %s %s failed: %s", action, cid, exc)
                            results.append(self._err(cid, "db", action, str(exc)))

            except ClientError as exc:
                logger.error("RDS describe_db_clusters failed in %s: %s", region, exc)

        return results

    def manage_databases(self, action: str, dry_run: bool) -> list[dict]:
        all_results = []
        with ThreadPoolExecutor(max_workers=len(self.regions)) as ex:
            futures = {ex.submit(self._manage_rds_region, r, action, dry_run): r for r in self.regions}
            for future in as_completed(futures):
                try:
                    all_results.extend(future.result())
                except Exception as exc:  # noqa: BLE001
                    region = futures[future]
                    logger.error("RDS worker for region %s raised: %s", region, exc, exc_info=True)
        return all_results
