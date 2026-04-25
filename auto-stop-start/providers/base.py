"""Abstract base class for cloud providers."""

from abc import ABC, abstractmethod


class CloudProvider(ABC):
    """
    All provider implementations must return lists of result dicts from their
    manage_* methods.  Each dict has at minimum:

        {
            "resource_id": str,
            "resource_type": "vm" | "db",
            "provider": str,
            "action": "start" | "stop" | "status",
            "status": "success" | "error" | "skipped",
            "detail": str,        # human-readable description of what happened
            "error": str | None,  # present only when status == "error"
        }
    """

    def __init__(self, config: dict, env: str) -> None:
        self.config = config
        self.env    = env

    @abstractmethod
    def manage_vms(self, action: str, dry_run: bool) -> list[dict]:
        """Start, stop, or report status for virtual machines."""

    @abstractmethod
    def manage_databases(self, action: str, dry_run: bool) -> list[dict]:
        """Start, stop, or report status for managed database instances."""

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _ok(self, resource_id: str, resource_type: str, action: str, detail: str = "") -> dict:
        return {
            "resource_id":   resource_id,
            "resource_type": resource_type,
            "provider":      self.__class__.__name__,
            "action":        action,
            "status":        "success",
            "detail":        detail,
            "error":         None,
        }

    def _skip(self, resource_id: str, resource_type: str, action: str, reason: str = "") -> dict:
        return {
            "resource_id":   resource_id,
            "resource_type": resource_type,
            "provider":      self.__class__.__name__,
            "action":        action,
            "status":        "skipped",
            "detail":        reason,
            "error":         None,
        }

    def _err(self, resource_id: str, resource_type: str, action: str, error: str) -> dict:
        return {
            "resource_id":   resource_id,
            "resource_type": resource_type,
            "provider":      self.__class__.__name__,
            "action":        action,
            "status":        "error",
            "detail":        "",
            "error":         error,
        }
