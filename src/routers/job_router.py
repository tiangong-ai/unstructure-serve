"""Lightweight durable status, streamed results, and explicit stage recovery."""

from fastapi import APIRouter, HTTPException
from starlette.responses import FileResponse

from src.services import job_store
from src.services.job_submission import durable_status, resume_submission

router = APIRouter()


class _LeasedFileResponse(FileResponse):
    def __init__(self, path, lease, **kwargs):
        super().__init__(path, **kwargs)
        self._lease = lease

    async def __call__(self, scope, receive, send):
        try:
            return await super().__call__(scope, receive, send)
        finally:
            # Also release on a broken connection, when normal background tasks
            # may not run. Retention cleanup cannot remove a file being streamed.
            self._lease.__exit__(None, None, None)


@router.get("/tasks/{task_id}/status", summary="Fetch lightweight durable task progress")
def task_status(task_id: str):
    status = durable_status(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Unknown durable task.")
    return status


@router.get("/tasks/{task_id}/result", summary="Download a completed durable task result")
def task_result(task_id: str):
    if job_store.find_job(task_id) is None:
        raise HTTPException(status_code=404, detail="Unknown durable task.")
    lease = job_store.stage_lock(task_id, "lifetime", shared=True)
    lease.__enter__()
    try:
        status = job_store.status(task_id)
        if status["state"] == "EXPIRED":
            raise HTTPException(status_code=410, detail="The retained job has expired.")
        if status["state"] != "SUCCESS":
            raise HTTPException(status_code=409, detail="The task result is not ready.")
        try:
            path = job_store.verified_result_path(task_id)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(
                status_code=500, detail="Stored result failed verification."
            ) from exc
        return _LeasedFileResponse(
            path,
            lease,
            media_type="application/json",
            filename=f"{task_id}.json",
            headers={"Cache-Control": "private, no-store"},
        )
    except BaseException:
        lease.__exit__(None, None, None)
        raise


@router.post("/tasks/{task_id}/resume", summary="Resume a durable task using retained stages")
def task_resume(task_id: str):
    return resume_submission(task_id)
