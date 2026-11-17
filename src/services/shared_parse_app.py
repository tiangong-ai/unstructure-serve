"""Celery app for the host's shared asynchronous parse queue."""

import os

from celery import Celery
from kombu import Queue

from src.config.config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND, CELERY_RESULT_EXPIRES
from src.services.celery_runtime import runtime_options

SHARED_PARSE_QUEUE = os.getenv("CELERY_SHARED_PARSE_QUEUE", "").strip() or "queue_parse_shared"
SHARED_PARSE_PRIORITY = {"urgent": 0, "normal": 9}

celery_app = Celery(
    "mineru_shared_parse",
    set_as_current=False,
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    task_default_queue=SHARED_PARSE_QUEUE,
    task_queues=[Queue(SHARED_PARSE_QUEUE)],
    task_create_missing_queues=False,
    task_default_priority=SHARED_PARSE_PRIORITY["normal"],
    worker_prefetch_multiplier=1,
    **runtime_options(CELERY_BROKER_URL, CELERY_RESULT_EXPIRES),
)


@celery_app.task(
    name="shared_parse.parse_document", acks_late=True, ignore_result=True, shared=False
)
def parse_document(ref):
    from src.services.shared_parse import run_parse

    return run_parse(ref)
