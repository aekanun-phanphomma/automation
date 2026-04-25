"""
Schedule evaluation.

Determines whether the current moment falls inside a start/stop window
defined by cron expressions.  Uses `croniter` to compute the previous
and next fire times for each expression.

Install dependency:  pip install croniter
"""

from datetime import datetime, timedelta

from core.logger import get_logger

logger = get_logger(__name__)

try:
    import pytz
    from croniter import croniter
    _DEPS_AVAILABLE = True
except ImportError:
    _DEPS_AVAILABLE = False
    logger.warning(
        "croniter / pytz not installed — schedule-check is disabled. "
        "Run: pip install croniter pytz"
    )


def is_within_schedule(schedule: dict, action: str) -> bool:
    """
    Return True when the current wall-clock time matches the schedule
    well enough that the given action should proceed.

    Logic:
    - For 'stop' : True when the stop cron fired within the last 10 min.
    - For 'start': True when the start cron fired within the last 10 min.
    - For 'status': always True.

    The 10-minute window means the script can be invoked by a cron job
    every 5–10 minutes without concern for exact-second alignment.
    """
    if action == "status":
        return True

    if not _DEPS_AVAILABLE:
        logger.warning("Skipping schedule check — dependencies missing.")
        return True

    tz_name   = schedule.get("timezone", "UTC")
    cron_expr = schedule.get(action, {}).get("cron")

    if not cron_expr:
        logger.debug("No cron expression for action '%s' — treating as in-schedule.", action)
        return True

    try:
        tz  = pytz.timezone(tz_name)
        now = datetime.now(tz)

        # Previous fire time of the cron expression
        itr  = croniter(cron_expr, now)
        prev = itr.get_prev(datetime)

        delta = now - prev
        in_window = delta <= timedelta(minutes=10)

        logger.info(
            "Schedule check: action=%s cron='%s' tz=%s now=%s prev_fire=%s delta=%ds in_window=%s",
            action, cron_expr, tz_name,
            now.strftime("%Y-%m-%dT%H:%M:%S%z"),
            prev.strftime("%Y-%m-%dT%H:%M:%S%z"),
            delta.total_seconds(),
            in_window,
        )
        return in_window

    except Exception as exc:  # noqa: BLE001
        logger.error("Schedule evaluation failed: %s", exc, exc_info=True)
        return False
