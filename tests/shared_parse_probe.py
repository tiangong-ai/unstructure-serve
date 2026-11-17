"""Subprocess entrypoint for the isolated Redis priority regression."""

import json
import os
from pathlib import Path
import sys

import redis

TASK = "shared_parse.parse_document"
QUEUE = "queue_parse_shared"


def main():
    action, *args = sys.argv[1:]
    if action == "publish":
        from src.services.job_store import create_job
        from src.services.job_submission import publish_existing_job

        source = Path("source.pdf")
        source.write_bytes(b"broker scheduling probe")
        jobs = {}
        for label, mode, priority in json.loads(args[0]):
            options = {} if priority is None else {"priority": priority}
            record = create_job(mode, source, options)
            job_id = record["job_id"]
            publish_existing_job(job_id)
            jobs[label] = job_id
        print(json.dumps(jobs))
        return

    assert action == "worker"
    from src.services.shared_parse_app import celery_app

    task = celery_app.tasks[TASK]
    broker = redis.Redis.from_url(os.environ["CELERY_BROKER_URL"])

    def hold(ref):
        job_id = ref["job_id"]
        broker.rpush("probe:started", job_id)
        if broker.blpop(f"probe:release:{job_id}", timeout=60) is None:
            raise TimeoutError("Test failed to release a probe")
        broker.rpush("probe:finished", job_id)

    # Preserve the actual task name, registration and acknowledgement settings.
    # Only replace the task body: no parsing, checkpointing or downstream work.
    task.run = hold
    celery_app.worker_main(
        [
            "worker",
            "--pool=solo",
            "--concurrency=1",
            "--prefetch-multiplier=1",
            f"--queues={QUEUE}",
            f"--hostname={args[0]}@%h",
            "--loglevel=INFO",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
        ]
    )


if __name__ == "__main__":
    main()
