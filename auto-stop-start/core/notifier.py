"""
Optional notifications — Slack/Teams webhook and AWS SNS.
Both channels are no-ops when disabled in config.
"""

import json
import os
import urllib.request
from typing import Any

from core.logger import get_logger

logger = get_logger(__name__)


def _should_notify(cfg: dict, has_error: bool) -> bool:
    if not cfg.get("enabled", False):
        return False
    on = cfg.get("on", "error")
    if on == "all":
        return True
    if on == "error" and has_error:
        return True
    if on == "success" and not has_error:
        return True
    return False


def _post_webhook(url: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("Webhook notified, HTTP %s", resp.status)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Webhook notification failed: %s", exc)


def _post_sns(topic_arn: str, subject: str, message: str) -> None:
    try:
        import boto3  # noqa: PLC0415
        sns = boto3.client("sns")
        sns.publish(TopicArn=topic_arn, Subject=subject, Message=message)
        logger.info("SNS notification sent to %s", topic_arn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("SNS notification failed: %s", exc)


def notify(config: dict, results: list[dict], action: str, env: str) -> None:
    """Send notifications based on run results and config."""
    notify_cfg = config.get("notifications", {})
    has_error  = any(r["status"] == "error" for r in results)

    summary = {
        "action":    action,
        "env":       env,
        "succeeded": sum(1 for r in results if r["status"] == "success"),
        "failed":    sum(1 for r in results if r["status"] == "error"),
        "skipped":   sum(1 for r in results if r["status"] == "skipped"),
        "errors":    [r for r in results if r["status"] == "error"],
    }

    # ── Webhook ──────────────────────────────────────────────────────────────
    webhook_cfg = notify_cfg.get("webhook", {})
    if _should_notify(webhook_cfg, has_error):
        url = webhook_cfg.get("url", "")
        if url:
            _post_webhook(url, {
                "text": (
                    f"[auto-stop-start] `{action}` on *{env}* — "
                    f"{summary['succeeded']} ok, {summary['failed']} failed, "
                    f"{summary['skipped']} skipped"
                ),
                "details": summary,
            })

    # ── SNS ───────────────────────────────────────────────────────────────────
    sns_cfg = notify_cfg.get("sns", {})
    if _should_notify(sns_cfg, has_error):
        topic_arn = sns_cfg.get("topic_arn", "")
        if topic_arn:
            _post_sns(
                topic_arn,
                subject=f"[auto-stop-start] {action} {env} — {'ERROR' if has_error else 'OK'}",
                message=json.dumps(summary, indent=2),
            )
