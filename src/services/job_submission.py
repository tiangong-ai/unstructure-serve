"""Durable API submission and queue routing; broker messages contain only IDs."""

from pathlib import Path
import tempfile

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from src.services import job_store
from src.utils.upload_io import persist_upload


def publish_record(record):
    ref = {"job_id": record["job_id"], "generation": record["generation"]}
    priority = record["options"].get("priority", "normal")
    if record["mode"] == "two-stage":
        from src.services.two_stage_pipeline import (
            resolve_two_stage_queues,
            submit_durable_two_stage,
        )

        queues = resolve_two_stage_queues(priority)
        return submit_durable_two_stage(
            ref,
            parse_queue=queues["parse"],
            vision_queue=queues["vision"],
            dispatch_queue=queues["dispatch"],
            merge_queue=queues["merge"],
        )
    from src.config.config import CELERY_TASK_MINERU_QUEUE, CELERY_TASK_URGENT_QUEUE
    from src.services.tasks.mineru_tasks import run_mineru_task, run_mineru_with_images_task

    task = run_mineru_with_images_task if record["mode"] == "images" else run_mineru_task
    queue = CELERY_TASK_URGENT_QUEUE if priority == "urgent" else CELERY_TASK_MINERU_QUEUE
    return task.apply_async(args=[ref], queue=queue, task_id=record["job_id"])


def publish_existing_job(job_id):
    return job_store.publish_job(job_id, publish_record)


def _publish(job_id):
    try:
        publish_existing_job(job_id)
    except job_store.PublishUncertain as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "task_id": exc.job_id,
                "publication": "uncertain",
                "message": "Submission outcome uncertain; the job and input are retained.",
            },
        ) from exc
    return {"task_id": job_id, "state": "PENDING"}


def _create_and_publish(upload, mode, options, idempotency_key):
    # Keep the original filename in the fingerprint and move Office conversion
    # into the durable execution stage. The upload copy is private and immutable.
    filename = Path(upload.filename).name
    with tempfile.TemporaryDirectory(prefix="mineru-upload-") as temporary:
        source = Path(temporary) / filename
        persist_upload(upload, source)
        record = job_store.create_job(mode, source, options, idempotency_key=idempotency_key)
    if job_store.status(record["job_id"])["state"] == "EXPIRED":
        raise HTTPException(status_code=410, detail="The retained job has expired.")
    return _publish(record["job_id"])


async def submit_upload(upload, mode, options, idempotency_key):
    try:
        return await run_in_threadpool(_create_and_publish, upload, mode, options, idempotency_key)
    except job_store.JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def durable_status(task_id):
    if job_store.find_job(task_id) is None:
        return None
    return job_store.status(task_id)


def durable_payload(task_id):
    if job_store.find_job(task_id) is None:
        return None
    with job_store.stage_lock(task_id, "lifetime", shared=True):
        status = job_store.status(task_id)
        if status["state"] == "EXPIRED":
            raise HTTPException(status_code=410, detail="The retained job has expired.")
        if status["state"] == "SUCCESS":
            status["result"] = job_store.read_result(task_id)
        return status


def resume_submission(task_id):
    if job_store.find_job(task_id) is None:
        raise HTTPException(status_code=404, detail="Unknown durable task.")
    try:
        job_store.resume_job(task_id)
    except (job_store.JobBusy, job_store.JobConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _publish(task_id)
