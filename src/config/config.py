import os
import tempfile
from typing import Optional

import toml
from dotenv import load_dotenv

# Load .env early so environment overrides are visible before config values are read.
load_dotenv()

config = toml.load(".secrets/secrets.toml")
_CELERY_CONFIG = config.get("CELERY", {})
_MINERU_CONFIG = config.get("MINERU", {})


def _env_override(var_name: str, fallback: Optional[str]) -> Optional[str]:
    """Return a stripped environment override when present, else the fallback."""
    raw = os.getenv(var_name)
    if raw is None:
        return fallback
    stripped = raw.strip()
    return stripped or fallback


def _bool_from_env(var_name: str, default: bool) -> bool:
    """Read boolean flags from the environment while keeping TOML defaults."""
    raw_value = os.getenv(var_name)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


FASTAPI_AUTH = _bool_from_env("FASTAPI_AUTH", config["FASTAPI"]["AUTH"])
FASTAPI_BEARER_TOKEN = _env_override("FASTAPI_BEARER_TOKEN", config["FASTAPI"]["BEARER_TOKEN"])
FASTAPI_MIDDLEWARE_SECRECT_KEY = _env_override(
    "FASTAPI_MIDDLEWARE_SECRECT_KEY", config["FASTAPI"]["MIDDLEWARE_SECRECT_KEY"]
)

OPENAI_API_KEY = _env_override("OPENAI_API_KEY", config["OPENAI"]["API_KEY"])

GENIMI_API_KEY = _env_override("GENIMI_API_KEY", config["GOOGLE"]["API_KEY"])

VLLM_API_KEY = _env_override("VLLM_API_KEY", config["VLLM"]["API_KEY"])
_VLLM_BASE_URL_DEFAULT = config["VLLM"].get("BASE_URL") or config["VLLM"].get("BASE_URLS")
VLLM_BASE_URL = _env_override("VLLM_BASE_URL", _VLLM_BASE_URL_DEFAULT)
VLLM_BASE_URLS = _env_override("VLLM_BASE_URLS", config["VLLM"].get("BASE_URLS"))

# Celery/Redis task queue configuration
CELERY_BROKER_URL = _env_override("CELERY_BROKER_URL", _CELERY_CONFIG.get("BROKER_URL")) or (
    "redis://localhost:6379/0"
)
CELERY_RESULT_BACKEND = (
    _env_override("CELERY_RESULT_BACKEND", _CELERY_CONFIG.get("RESULT_BACKEND"))
    or CELERY_BROKER_URL
)
CELERY_TASK_DEFAULT_QUEUE = (
    _env_override("CELERY_TASK_DEFAULT_QUEUE", _CELERY_CONFIG.get("DEFAULT_QUEUE")) or "default"
)
CELERY_TASK_MINERU_QUEUE = (
    _env_override("CELERY_TASK_MINERU_QUEUE", _CELERY_CONFIG.get("MINERU_QUEUE")) or "queue_normal"
)
CELERY_TASK_URGENT_QUEUE = (
    _env_override("CELERY_TASK_URGENT_QUEUE", _CELERY_CONFIG.get("URGENT_QUEUE")) or "queue_urgent"
)
CELERY_RESULT_EXPIRES = int(
    os.getenv("CELERY_RESULT_EXPIRES", _CELERY_CONFIG.get("RESULT_EXPIRES", "86400"))
)

# Local task workspace for mineru async jobs
MINERU_TASK_STORAGE_DIR = _env_override(
    "MINERU_TASK_STORAGE_DIR", _MINERU_CONFIG.get("TASK_STORAGE_DIR")
) or os.path.join(tempfile.gettempdir(), "tiangong_mineru_tasks")
