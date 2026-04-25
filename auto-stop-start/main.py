"""
Auto Stop/Start — entry point.

Usage examples:
  python main.py --action stop  --env dev  --provider aws   --resource-type vm
  python main.py --action start --env prod --provider azure --resource-type db
  python main.py --action stop  --env dev  --provider all   --resource-type all --dry-run
  python main.py --action status --env prod
"""

import argparse
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from core.logger import get_logger
from core.config import load_config
from core.scheduler import is_within_schedule

logger = get_logger(__name__)

BASE_DIR = Path(__file__).parent


def _build_providers(env_config: dict, env: str) -> list:
    """Instantiate provider objects based on what is configured for the env."""
    providers = []

    if "aws" in env_config:
        from providers.aws import AWSProvider
        providers.append(AWSProvider(env_config["aws"], env))

    if "azure" in env_config:
        from providers.azure import AzureProvider
        providers.append(AzureProvider(env_config["azure"], env))

    return providers


def _run_provider(provider, action: str, resource_type: str, dry_run: bool) -> list[dict]:
    results = []
    if resource_type in ("vm", "all"):
        results.extend(provider.manage_vms(action, dry_run))
    if resource_type in ("db", "all"):
        results.extend(provider.manage_databases(action, dry_run))
    return results


def _print_summary(results: list[dict]) -> None:
    success = [r for r in results if r["status"] == "success"]
    failed  = [r for r in results if r["status"] == "error"]
    skipped = [r for r in results if r["status"] == "skipped"]

    logger.info(
        "Run summary",
        extra={
            "succeeded": len(success),
            "failed":    len(failed),
            "skipped":   len(skipped),
        },
    )

    for r in success:
        logger.info("  OK       %s  (%s)", r["resource_id"], r.get("detail", ""))
    for r in skipped:
        logger.info("  SKIPPED  %s  (%s)", r["resource_id"], r.get("detail", ""))
    for r in failed:
        logger.error("  FAILED   %s  — %s", r["resource_id"], r.get("error", "unknown"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto stop/start VMs and databases on AWS and Azure.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--action",
        choices=["start", "stop", "status"],
        required=True,
        help="Operation to perform.",
    )
    parser.add_argument(
        "--env",
        required=True,
        help="Environment name matching a key in config.yaml environments section.",
    )
    parser.add_argument(
        "--provider",
        choices=["aws", "azure", "all"],
        default="all",
        help="Cloud provider to target (default: all).",
    )
    parser.add_argument(
        "--resource-type",
        choices=["vm", "db", "all"],
        default="all",
        dest="resource_type",
        help="Resource type to manage (default: all).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate actions without making any real changes.",
    )
    parser.add_argument(
        "--schedule-check",
        action="store_true",
        dest="schedule_check",
        help="Skip execution if current time is outside the configured schedule window.",
    )
    parser.add_argument(
        "--config",
        default=str(BASE_DIR / "config.yaml"),
        help="Path to config.yaml (default: ./config.yaml).",
    )
    parser.add_argument(
        "--output-json",
        action="store_true",
        dest="output_json",
        help="Print results as JSON to stdout (useful for Lambda / CI integration).",
    )

    args = parser.parse_args()

    # ── Load config ──────────────────────────────────────────────────────────
    config = load_config(args.config)
    env_config = config.get("environments", {}).get(args.env)

    if not env_config:
        logger.error("Environment '%s' not found in %s", args.env, args.config)
        sys.exit(1)

    # ── Optional schedule gate ───────────────────────────────────────────────
    if args.schedule_check:
        schedule_name = env_config.get("schedule")
        schedule_def  = config.get("schedules", {}).get(schedule_name)
        if schedule_def and not is_within_schedule(schedule_def, args.action):
            logger.info(
                "Current time is outside schedule '%s' for action '%s' — skipping.",
                schedule_name,
                args.action,
            )
            sys.exit(0)

    if args.dry_run:
        logger.info("DRY RUN — no real changes will be made")

    # ── Build providers ──────────────────────────────────────────────────────
    all_providers = _build_providers(env_config, args.env)

    if args.provider != "all":
        all_providers = [
            p for p in all_providers
            if p.__class__.__name__.lower().startswith(args.provider)
        ]

    if not all_providers:
        logger.warning("No providers configured for env='%s' provider='%s'", args.env, args.provider)
        sys.exit(0)

    # ── Execute concurrently ─────────────────────────────────────────────────
    all_results: list[dict] = []

    with ThreadPoolExecutor(max_workers=len(all_providers)) as executor:
        futures = {
            executor.submit(_run_provider, p, args.action, args.resource_type, args.dry_run): p
            for p in all_providers
        }
        for future in as_completed(futures):
            provider = futures[future]
            try:
                all_results.extend(future.result())
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Provider %s raised an unhandled exception: %s",
                    provider.__class__.__name__,
                    exc,
                    exc_info=True,
                )

    # ── Output ───────────────────────────────────────────────────────────────
    if args.output_json:
        print(json.dumps(all_results, indent=2, default=str))
    else:
        _print_summary(all_results)

    failed = [r for r in all_results if r["status"] == "error"]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
