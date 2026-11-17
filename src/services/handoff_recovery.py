"""Reconcile missing shared parse handoffs without scanning historical jobs."""

import logging
import math
import os
from pathlib import Path
import time

from dotenv import load_dotenv

from src.services import job_store as store

logger = logging.getLogger(__name__)
_TERMINAL = {"SUCCESS", "FAILURE", "EXPIRED"}


class HandoffRecovery:
    """Keep a directory cursor so queued jobs cannot delay later candidates indefinitely."""

    def __init__(self):
        self._entries = None

    def close(self):
        if self._entries is not None:
            self._entries.close()
            self._entries = None

    def scan(self, *, limit):
        """Examine at most limit entries and retain the cursor for the next poll."""
        if limit < 1:
            raise ValueError("Handoff recovery scan limit must be positive")
        if self._entries is None:
            try:
                self._entries = os.scandir(store.handoff_index_dir())
            except FileNotFoundError:
                return 0

        for checked in range(limit):
            try:
                entry = next(self._entries)
            except StopIteration:
                self.close()
                return checked
            if entry.name.startswith(".") or not entry.name.endswith(".json"):
                continue
            job_id, generation = None, None
            try:
                path = Path(entry.path)
                job_id, generation = self._reference(path)
                self._reconcile(path, job_id, generation)
            except store.JobBusy:
                continue
            except Exception as exc:
                logger.warning(
                    "Shared parse handoff recovery failed job=%s generation=%s type=%s",
                    job_id,
                    generation,
                    type(exc).__name__,
                )
        return limit

    @staticmethod
    def _settled(status, generation):
        return (
            status["generation"] != generation
            or status["state"] in _TERMINAL
            or status.get("handoff") == "published"
        )

    @staticmethod
    def _reference(path):
        job_id, generation_text = path.stem.rsplit(".", 1)
        generation = int(generation_text)
        if path != store.handoff_index_path(job_id, generation):
            raise ValueError("Invalid shared parse handoff index")
        return job_id, generation

    def _reconcile(self, path, job_id, generation):
        try:
            status = store.status(job_id)
        except FileNotFoundError:
            path.unlink(missing_ok=True)
            return
        if self._settled(status, generation):
            path.unlink(missing_ok=True)
            return
        if status["publication"] != "published" or status["state"] != "STARTED":
            return
        if not (store.job_dir(job_id) / "parse" / "manifest.json").is_file():
            return

        from src.services.shared_parse import recover_handoff

        recover_handoff(job_id, expected_generation=generation)
        if self._settled(store.status(job_id), generation):
            path.unlink(missing_ok=True)


def main():
    load_dotenv()
    interval = float(os.getenv("MINERU_HANDOFF_RECOVERY_INTERVAL_SECONDS", "30"))
    limit = int(os.getenv("MINERU_HANDOFF_RECOVERY_LIMIT", "64"))
    if not math.isfinite(interval) or interval <= 0 or limit < 1:
        raise ValueError("Handoff recovery interval and scan limit must be positive")
    logging.basicConfig(level=logging.INFO)
    recovery = HandoffRecovery()
    try:
        while True:
            try:
                recovery.scan(limit=limit)
            except OSError as exc:
                logger.warning("Shared parse handoff scan failed type=%s", type(exc).__name__)
                recovery.close()
            time.sleep(interval)
    finally:
        recovery.close()


if __name__ == "__main__":
    main()
