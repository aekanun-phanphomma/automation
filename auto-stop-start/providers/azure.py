"""
Azure provider — manages Azure VMs and flexible-server databases
(PostgreSQL Flexible Server and MySQL Flexible Server).

Authentication: uses DefaultAzureCredential.
  Priority order: env vars (AZURE_CLIENT_ID / SECRET / TENANT) →
                  Managed Identity → Azure CLI → Visual Studio Code
  Never put credentials in config.yaml.

Required RBAC roles (minimum):
  VMs       : Virtual Machine Contributor  (or custom with start/deallocate)
  Databases : Contributor on the resource group
              (Azure DB SDKs don't yet expose a fine-grained built-in role)

Install:
  pip install azure-identity azure-mgmt-compute azure-mgmt-rdbms
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from core.logger import get_logger
from providers.base import CloudProvider

logger = get_logger(__name__)

try:
    from azure.core.exceptions import AzureError, HttpResponseError
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.rdbms.postgresql_flexibleservers import PostgreSQLManagementClient
    from azure.mgmt.rdbms.mysql_flexibleservers import MySQLManagementClient
    _AZURE_AVAILABLE = True
except ImportError:
    _AZURE_AVAILABLE = False
    logger.warning(
        "Azure SDK packages not installed. "
        "Run: pip install azure-identity azure-mgmt-compute azure-mgmt-rdbms"
    )

# VM states where a start/stop is meaningful
_STARTABLE_STATES = {"deallocated", "stopped"}
_STOPPABLE_STATES = {"running", "starting"}


class AzureProvider(CloudProvider):

    def __init__(self, config: dict, env: str) -> None:
        super().__init__(config, env)
        self.subscription_id: str  = config.get("subscription_id", "")
        self.resource_groups: list = config.get("resource_groups", [])
        self.vm_cfg:   dict        = config.get("vm", {})
        self.db_cfg:   dict        = config.get("database", {})

        if not _AZURE_AVAILABLE:
            return

        self._credential = DefaultAzureCredential()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _tags_match(self, resource_tags: dict | None, filter_map: dict) -> bool:
        tags = resource_tags or {}
        return all(tags.get(k) == v for k, v in filter_map.items())

    def _is_excluded(self, name: str, exclude_prefixes: list[str]) -> bool:
        name_lower = (name or "").lower()
        return any(name_lower.startswith(p.lower()) for p in exclude_prefixes)

    # ── Virtual Machines ──────────────────────────────────────────────────────

    def _manage_vms_in_rg(
        self,
        compute_client: "ComputeManagementClient",
        rg: str,
        action: str,
        dry_run: bool,
    ) -> list[dict]:
        results      = []
        tag_filters  = self.vm_cfg.get("tag_filters", {})
        excludes     = self.vm_cfg.get("exclude_name_prefixes", [])

        try:
            vms = list(compute_client.virtual_machines.list(rg))
        except HttpResponseError as exc:
            logger.error("Failed to list VMs in %s: %s", rg, exc)
            return [self._err(f"azure-vm:{rg}", "vm", action, str(exc))]

        for vm in vms:
            name = vm.name

            if self._is_excluded(name, excludes):
                results.append(self._skip(name, "vm", action, f"excluded prefix"))
                continue

            if not self._tags_match(vm.tags, tag_filters):
                continue

            # Get power state (requires instance_view)
            try:
                iv        = compute_client.virtual_machines.instance_view(rg, name)
                statuses  = {s.code for s in (iv.statuses or [])}
                power     = next(
                    (s.display_status for s in (iv.statuses or []) if s.code.startswith("PowerState/")),
                    "unknown",
                )
            except HttpResponseError as exc:
                logger.error("instance_view failed for %s: %s", name, exc)
                results.append(self._err(name, "vm", action, str(exc)))
                continue

            if action == "status":
                results.append(self._ok(name, "vm", "status", f"power={power} rg={rg}"))
                continue

            power_code = power.replace("VM ", "").lower()

            if action == "stop" and power_code not in _STOPPABLE_STATES:
                results.append(self._skip(name, "vm", action, f"already {power}"))
                continue
            if action == "start" and power_code not in _STARTABLE_STATES:
                results.append(self._skip(name, "vm", action, f"already {power}"))
                continue

            try:
                if dry_run:
                    logger.info("[DRY-RUN] Would %s Azure VM %s/%s", action, rg, name)
                    results.append(self._ok(name, "vm", action, f"dry-run rg={rg}"))
                    continue

                if action == "stop":
                    # deallocate = release compute resources → no charges
                    poller = compute_client.virtual_machines.begin_deallocate(rg, name)
                    poller.result()
                    logger.info("Deallocated Azure VM %s/%s", rg, name)
                else:
                    poller = compute_client.virtual_machines.begin_start(rg, name)
                    poller.result()
                    logger.info("Started Azure VM %s/%s", rg, name)

                results.append(self._ok(name, "vm", action, f"rg={rg}"))

            except AzureError as exc:
                logger.error("Azure VM %s %s/%s failed: %s", action, rg, name, exc)
                results.append(self._err(name, "vm", action, str(exc)))

        return results

    def manage_vms(self, action: str, dry_run: bool) -> list[dict]:
        if not _AZURE_AVAILABLE:
            return [self._err("azure-vm", "vm", action, "Azure SDK not installed")]

        if not self.subscription_id:
            return [self._err("azure-vm", "vm", action, "subscription_id not configured")]

        compute_client = ComputeManagementClient(self._credential, self.subscription_id)
        all_results    = []

        with ThreadPoolExecutor(max_workers=max(len(self.resource_groups), 1)) as ex:
            futures = {
                ex.submit(self._manage_vms_in_rg, compute_client, rg, action, dry_run): rg
                for rg in self.resource_groups
            }
            for future in as_completed(futures):
                try:
                    all_results.extend(future.result())
                except Exception as exc:  # noqa: BLE001
                    rg = futures[future]
                    logger.error("Azure VM worker for rg=%s raised: %s", rg, exc, exc_info=True)

        return all_results

    # ── Databases ─────────────────────────────────────────────────────────────

    def _manage_postgresql_rg(self, rg: str, action: str, dry_run: bool) -> list[dict]:
        results     = []
        tag_filters = self.db_cfg.get("tag_filters", {})

        try:
            pg_client = PostgreSQLManagementClient(self._credential, self.subscription_id)
            servers   = list(pg_client.servers.list_by_resource_group(rg))
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to list PostgreSQL flexible servers in %s: %s", rg, exc)
            return [self._err(f"azure-pg:{rg}", "db", action, str(exc))]

        for server in servers:
            name  = server.name
            state = (server.state or "unknown").lower()

            if not self._tags_match(server.tags, tag_filters):
                continue

            if action == "status":
                results.append(self._ok(name, "db", "status", f"type=postgresql state={state} rg={rg}"))
                continue

            if action == "stop" and state != "ready":
                results.append(self._skip(name, "db", action, f"postgresql server already {state}"))
                continue
            if action == "start" and state != "stopped":
                results.append(self._skip(name, "db", action, f"postgresql server already {state}"))
                continue

            try:
                if dry_run:
                    logger.info("[DRY-RUN] Would %s PostgreSQL Flexible Server %s/%s", action, rg, name)
                    results.append(self._ok(name, "db", action, f"dry-run type=postgresql rg={rg}"))
                    continue

                if action == "stop":
                    poller = pg_client.servers.begin_stop(rg, name)
                else:
                    poller = pg_client.servers.begin_start(rg, name)
                poller.result()

                logger.info("%s PostgreSQL Flexible Server %s/%s", action.capitalize() + "ed", rg, name)
                results.append(self._ok(name, "db", action, f"type=postgresql rg={rg}"))

            except AzureError as exc:
                logger.error("PostgreSQL %s %s/%s failed: %s", action, rg, name, exc)
                results.append(self._err(name, "db", action, str(exc)))

        return results

    def _manage_mysql_rg(self, rg: str, action: str, dry_run: bool) -> list[dict]:
        results     = []
        tag_filters = self.db_cfg.get("tag_filters", {})

        try:
            mysql_client = MySQLManagementClient(self._credential, self.subscription_id)
            servers      = list(mysql_client.servers.list_by_resource_group(rg))
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to list MySQL flexible servers in %s: %s", rg, exc)
            return [self._err(f"azure-mysql:{rg}", "db", action, str(exc))]

        for server in servers:
            name  = server.name
            state = (server.state or "unknown").lower()

            if not self._tags_match(server.tags, tag_filters):
                continue

            if action == "status":
                results.append(self._ok(name, "db", "status", f"type=mysql state={state} rg={rg}"))
                continue

            if action == "stop" and state != "ready":
                results.append(self._skip(name, "db", action, f"mysql server already {state}"))
                continue
            if action == "start" and state != "stopped":
                results.append(self._skip(name, "db", action, f"mysql server already {state}"))
                continue

            try:
                if dry_run:
                    logger.info("[DRY-RUN] Would %s MySQL Flexible Server %s/%s", action, rg, name)
                    results.append(self._ok(name, "db", action, f"dry-run type=mysql rg={rg}"))
                    continue

                if action == "stop":
                    poller = mysql_client.servers.begin_stop(rg, name)
                else:
                    poller = mysql_client.servers.begin_start(rg, name)
                poller.result()

                logger.info("%s MySQL Flexible Server %s/%s", action.capitalize() + "ed", rg, name)
                results.append(self._ok(name, "db", action, f"type=mysql rg={rg}"))

            except AzureError as exc:
                logger.error("MySQL %s %s/%s failed: %s", action, rg, name, exc)
                results.append(self._err(name, "db", action, str(exc)))

        return results

    def manage_databases(self, action: str, dry_run: bool) -> list[dict]:
        if not _AZURE_AVAILABLE:
            return [self._err("azure-db", "db", action, "Azure SDK not installed")]

        if not self.subscription_id:
            return [self._err("azure-db", "db", action, "subscription_id not configured")]

        engines     = self.db_cfg.get("engines", ["postgresql_flexible", "mysql_flexible"])
        all_results = []

        tasks = []
        for rg in self.resource_groups:
            if "postgresql_flexible" in engines:
                tasks.append(("pg", rg))
            if "mysql_flexible" in engines:
                tasks.append(("mysql", rg))

        with ThreadPoolExecutor(max_workers=max(len(tasks), 1)) as ex:
            futures = {}
            for engine, rg in tasks:
                fn = self._manage_postgresql_rg if engine == "pg" else self._manage_mysql_rg
                futures[ex.submit(fn, rg, action, dry_run)] = (engine, rg)

            for future in as_completed(futures):
                try:
                    all_results.extend(future.result())
                except Exception as exc:  # noqa: BLE001
                    engine, rg = futures[future]
                    logger.error("Azure DB worker %s/%s raised: %s", engine, rg, exc, exc_info=True)

        return all_results
