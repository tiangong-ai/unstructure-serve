"""Shared delivery budgets for applications using the same Celery broker."""

import os


def runtime_options(broker_url: str, result_expires: int) -> dict:
    if result_expires <= 0:
        raise ValueError("CELERY_RESULT_EXPIRES must be positive")
    options = {"result_expires": result_expires}
    if broker_url.startswith(("redis://", "rediss://")):
        try:
            visibility = int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "3600"))
            if visibility <= 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError("CELERY_VISIBILITY_TIMEOUT must be a positive integer") from exc
        options.update(
            visibility_timeout=visibility,
            broker_transport_options={
                "visibility_timeout": visibility,
                "queue_order_strategy": "priority",
            },
            result_backend_transport_options={"visibility_timeout": visibility},
        )
    return options
