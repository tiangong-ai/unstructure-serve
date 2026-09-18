from celery import Celery
from kombu import Queue
from src.services.celery_runtime import runtime_options

from src.config.config import (
    CELERY_BROKER_URL,
    CELERY_RESULT_BACKEND,
    CELERY_RESULT_EXPIRES,
    CELERY_TASK_DEFAULT_QUEUE,
    CELERY_TASK_MINERU_QUEUE,
    CELERY_TASK_URGENT_QUEUE,
)

# Single Celery application for the service; workers import this module.
celery_app = Celery(
    "tiangong_ai_unstructure",
    include=["src.services.tasks.mineru_tasks"],
)

celery_app.conf.update(
    broker_url=CELERY_BROKER_URL,
    result_backend=CELERY_RESULT_BACKEND,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    enable_utc=True,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    task_default_queue=CELERY_TASK_DEFAULT_QUEUE,
    task_queues=[
        Queue(CELERY_TASK_DEFAULT_QUEUE),
        Queue(CELERY_TASK_MINERU_QUEUE),
        Queue(CELERY_TASK_URGENT_QUEUE),
    ],
    task_routes={
        "mineru.parse": {"queue": CELERY_TASK_MINERU_QUEUE},
        "mineru.parse_images": {"queue": CELERY_TASK_MINERU_QUEUE},
    },
    **runtime_options(CELERY_BROKER_URL, CELERY_RESULT_EXPIRES),
)

celery_app.autodiscover_tasks(["src.services"], force=True)
