"""Bounded upload persistence and safe cleanup for isolated parsing jobs."""

import asyncio
import os
from pathlib import Path
import shutil
import tempfile
from concurrent.futures import Future

from fastapi import UploadFile

COPY_BUFFER_SIZE = 1024 * 1024


def persist_upload(upload: UploadFile, target: Path | None = None, *, suffix: str = "") -> str:
    """Run in a worker thread; never materialize a complete uploaded PDF in RAM."""
    if target is None:
        fd, name = tempfile.mkstemp(suffix=suffix)
        destination = os.fdopen(fd, "wb")
        target = Path(name)
    else:
        destination = target.open("wb")
    try:
        with destination:
            shutil.copyfileobj(upload.file, destination, length=COPY_BUFFER_SIZE)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return str(target)


async def await_parse_future(future: Future):
    # Shield prevents an HTTP timeout/disconnect from cancelling the wrapped
    # Future while its process is still reading the uploaded source.
    return await asyncio.shield(asyncio.wrap_future(future))


def cleanup_after_parse(paths, future: Future | None = None):
    def cleanup(_future=None):
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    if future is not None and not future.done() and not future.cancel():
        future.add_done_callback(cleanup)
    else:
        cleanup()
