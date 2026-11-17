"""Parse each durable job, then publish its independent follow-up stage."""

from contextlib import suppress

from celery.utils.log import get_task_logger

from src.services import job_store as store

logger = get_task_logger(__name__)
WORKFLOW = store.SHARED_PARSE_WORKFLOW
_PUBLISH_RETRY = {"max_retries": 2}


def _publish_followup(record):
    ref = {"job_id": record["job_id"], "generation": record["generation"]}
    priority = record["options"].get("priority", "normal")
    if record["mode"] == "two-stage":
        from src.services.two_stage_pipeline import dispatch, resolve_two_stage_queues

        queues = resolve_two_stage_queues(priority)
        return dispatch.apply_async(
            args=[ref],
            queue=queues["dispatch"],
            kwargs={
                "dispatch_queue": queues["dispatch"],
                "vision_queue": queues["vision"],
                "merge_queue": queues["merge"],
            },
            retry=True,
            retry_policy=_PUBLISH_RETRY,
        )

    from src.config.config import CELERY_TASK_MINERU_QUEUE, CELERY_TASK_URGENT_QUEUE
    from src.services.tasks.mineru_tasks import finish_mineru_task

    queue = CELERY_TASK_URGENT_QUEUE if priority == "urgent" else CELERY_TASK_MINERU_QUEUE
    return finish_mineru_task.apply_async(
        args=[ref], queue=queue, retry=True, retry_policy=_PUBLISH_RETRY
    )


def _handoff_state(record):
    handoff = record.get("handoff") or {}
    if handoff.get("generation") == record["generation"]:
        return handoff.get("state")
    return None


def _log_context(record):
    return (
        record["job_id"],
        record["generation"],
        record["mode"],
        record["options"].get("priority", "normal"),
    )


def _handoff(record):
    """Publish under the shared parse lease. The recipient tolerates replay."""
    job_id = record["job_id"]
    generation = record["generation"]
    try:
        if not store.record_handoff(job_id, generation, "pending"):
            return
        _publish_followup(record)
        store.record_handoff(job_id, generation, "published")
    except Exception as exc:
        # The broker may already have accepted the message. Keep the public
        # job nonterminal so its client continues tracking the original ID.
        with suppress(OSError):
            if not store.record_handoff(job_id, generation, "uncertain"):
                return
        logger.warning(
            "Parse handoff uncertain job=%s generation=%s mode=%s priority=%s type=%s",
            *_log_context(record),
            type(exc).__name__,
        )
        raise store.PublishUncertain(job_id) from None
    with suppress(OSError):
        store.handoff_index_path(job_id, generation).unlink(missing_ok=True)
    logger.info(
        "Parse handoff published job=%s generation=%s mode=%s priority=%s", *_log_context(record)
    )


def run_parse(ref):
    from src.services.durable_pipeline import ensure_parsed

    job_id, generation = ref["job_id"], ref["generation"]
    try:
        with store.execution(job_id, generation):
            with store.stage_lock(job_id, "shared-parse", blocking=False):
                record = store.read_job(job_id)
                logger.info(
                    "Parse claimed job=%s generation=%s mode=%s priority=%s",
                    *_log_context(record),
                )
                if (
                    store.status(job_id)["state"] in {"SUCCESS", "FAILURE"}
                    or _handoff_state(record) == "published"
                ):
                    return ref
                ensure_parsed(ref)
                logger.info(
                    "Parse checkpoint ready job=%s generation=%s mode=%s priority=%s",
                    *_log_context(record),
                )
                _handoff(record)
    except store.StaleGeneration:
        return {**ref, "stale": True}
    except store.JobBusy:
        # Another delivery owns this attempt. Leaving it in charge avoids
        # consuming a second parse worker while waiting on its file lock.
        # If that attempt is lost, the job remains explicitly resumable.
        logger.info("Duplicate parse delivery job=%s generation=%s", job_id, generation)
        return {**ref, "busy": True}
    return ref


def recover_handoff(job_id, *, expected_generation):
    """Republish a missing handoff for the generation selected by the caller."""
    from src.services.durable_pipeline import validate_parse_checkpoint

    try:
        with store.execution(job_id, expected_generation, blocking=False):
            with store.stage_lock(job_id, "shared-parse", blocking=False):
                record = store.read_job(job_id)
                if (
                    record.get("workflow") != WORKFLOW
                    or record["publication"] != "published"
                    or _handoff_state(record) == "published"
                    or store.status(job_id)["state"] != "STARTED"
                ):
                    return
                if not (store.job_dir(job_id) / "parse" / "manifest.json").is_file():
                    return
                validate_parse_checkpoint(record)
                _handoff(record)
    except store.StaleGeneration:
        return
