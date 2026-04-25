"""Config loader — resolves ${ENV_VAR} placeholders from the environment."""

import os
import re
import sys
from pathlib import Path

import yaml

from core.logger import get_logger

logger = get_logger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _resolve_env_vars(obj):
    """Recursively expand ${VAR} tokens in string values."""
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(i) for i in obj]
    if isinstance(obj, str):
        def _replace(m):
            var = m.group(1)
            val = os.environ.get(var)
            if val is None:
                logger.warning("Environment variable '%s' is not set — using empty string.", var)
                return ""
            return val
        return _ENV_VAR_RE.sub(_replace, obj)
    return obj


def load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        logger.error("Config file not found: %s", path)
        sys.exit(1)

    with config_path.open() as fh:
        raw = yaml.safe_load(fh)

    return _resolve_env_vars(raw or {})
