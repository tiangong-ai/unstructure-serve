import atexit
import ctypes
import logging
import multiprocessing
import os
import queue
import signal
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, Future
from contextlib import suppress
from dataclasses import dataclass
from threading import Lock
from typing import Dict, List, Optional

from src.utils.text_output import build_plain_text, clean_text as _clean_text

_LINUX_PR_SET_PDEATHSIG = 1
_CHILD_EXIT_GRACE_SECONDS = 5
_CHILD_TERMINATE_GRACE_SECONDS = 5


def _set_parent_death_signal(signum: int = signal.SIGTERM) -> bool:
    """Ask Linux to signal this process when its parent dies."""
    if not sys_platform_is_linux():
        return False
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        result = libc.prctl(_LINUX_PR_SET_PDEATHSIG, signum, 0, 0, 0)
    except OSError:
        return False
    return result == 0


def sys_platform_is_linux() -> bool:
    return os.name == "posix" and hasattr(os, "killpg") and os.uname().sysname == "Linux"


def _exit_and_signal_child_group(signum: int, _frame: object) -> None:
    """Terminate all descendants in the isolated parse process group, then exit."""
    with suppress(Exception):
        signal.signal(signum, signal.SIG_IGN)
    with suppress(Exception):
        os.killpg(os.getpgrp(), signum)
    time.sleep(0.5)
    with suppress(Exception):
        os.killpg(os.getpgrp(), signal.SIGKILL)
    os._exit(128 + signum)


def _configure_parse_child_process() -> None:
    parent_death_signal_set = _set_parent_death_signal(signal.SIGTERM)
    if os.name == "posix":
        with suppress(OSError):
            os.setsid()
        signal.signal(signal.SIGTERM, _exit_and_signal_child_group)
        signal.signal(signal.SIGINT, _exit_and_signal_child_group)
    if parent_death_signal_set and os.getppid() == 1:
        _exit_and_signal_child_group(signal.SIGTERM, None)


def _signal_process_group(pid: int, signum: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        return False
    except OSError:
        return False
    return True


def _cleanup_child_process(
    proc: multiprocessing.Process,
    *,
    terminate: bool,
) -> None:
    if terminate:
        if not _signal_process_group(proc.pid, signal.SIGTERM) and proc.is_alive():
            proc.terminate()
        proc.join(timeout=_CHILD_TERMINATE_GRACE_SECONDS)
    else:
        proc.join(timeout=_CHILD_EXIT_GRACE_SECONDS)

    if proc.is_alive():
        if not _signal_process_group(proc.pid, signal.SIGTERM):
            proc.terminate()
        proc.join(timeout=_CHILD_TERMINATE_GRACE_SECONDS)

    if proc.is_alive():
        if not _signal_process_group(proc.pid, signal.SIGKILL):
            with suppress(AttributeError):
                proc.kill()
        proc.join(timeout=_CHILD_TERMINATE_GRACE_SECONDS)

    # The parse child is a process-group leader. After it exits, MinerU helper
    # processes can still remain in the same group, so sweep the group too.
    if _signal_process_group(proc.pid, signal.SIGTERM):
        time.sleep(0.2)
        _signal_process_group(proc.pid, signal.SIGKILL)


def _worker_init(gpu_id: str):
    """Initializer for each worker process to pin visibility to a single GPU."""
    parent_death_signal_set = _set_parent_death_signal(signal.SIGTERM)
    if parent_death_signal_set and os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGTERM)
    # Only expose the target GPU to libraries inside this process
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    # Optional: set Paddle/other OCR backends to GPU if supported. They usually auto-detect.


def _image_text(item: dict) -> str:
    captions = item.get("img_caption") or []
    footnotes = item.get("img_footnote") or []
    combined_text = "\n".join([*captions, *footnotes])
    return _clean_text(combined_text)


def _table_text(item: dict) -> str:
    text_parts = [
        "\n".join(item.get("table_caption", [])),
        item.get("table_body", ""),
        "\n".join(item.get("table_footnote", [])),
    ]
    combined_text = "\n".join(filter(None, text_parts))
    return _clean_text(combined_text)


def _list_text(item: dict) -> str:
    list_items = item.get("list_items") or []
    if list_items:
        combined_text = "\n".join(list_items)
    else:
        combined_text = item.get("text", "")
    return _clean_text(combined_text)


def _actual_parse(
    file_path: str, pipeline: str, options: Optional[Dict[str, object]] = None
) -> Dict[str, object]:
    """Inner heavy parse logic (run inside an isolated subprocess watchdog)."""
    options = dict(options or {})
    chunk_type = bool(options.pop("chunk_type", False))
    return_txt = bool(options.pop("return_txt", False))
    if pipeline == "images":
        from src.services.mineru_with_images_service import parse_with_images

        result_items, txt_text = parse_with_images(
            file_path,
            chunk_type=chunk_type,
            return_txt=return_txt,
            **options,
        )
        return {"result": result_items, "txt": txt_text}
    if pipeline == "sci":
        from src.services.mineru_sci_service import parse_doc
    else:  # default
        from src.services.mineru_service_full import parse_doc

    with tempfile.TemporaryDirectory() as tmp_dir:
        content_list_content, _, txt_candidate = parse_doc(
            [file_path],
            tmp_dir,
            return_txt=return_txt,
            **options,
        )
        results: List[Dict[str, object]] = []
        filtered_items: List[dict] = []
        for item in content_list_content:
            itype = item.get("type")
            text: Optional[str] = None

            if itype in ("text", "equation"):
                candidate = item.get("text", "")
                if candidate and candidate.strip():
                    text = _clean_text(candidate)
            elif itype in ("header", "footer"):
                if not chunk_type:
                    continue
                candidate = item.get("text", "")
                if candidate and candidate.strip():
                    text = _clean_text(candidate)
            elif itype == "list" and (
                any(text.strip() for text in item.get("list_items", []))
                or item.get("text", "").strip()
            ):
                text = _list_text(item)
            elif itype == "image" and (item.get("img_caption") or item.get("img_footnote")):
                text = _image_text(item)
            elif itype == "table" and (
                item.get("table_caption") or item.get("table_body") or item.get("table_footnote")
            ):
                text = _table_text(item)
            else:
                continue

            if not text:
                continue

            chunk: Dict[str, object] = {
                "text": text,
                "page_number": int(item.get("page_idx", 0)) + 1,
            }
            if chunk_type:
                if itype == "text" and item.get("text_level") is not None:
                    chunk["type"] = "title"
                elif itype in ("header", "footer"):
                    chunk["type"] = itype
            results.append(chunk)
            filtered_items.append(item)

        txt_text = None
        if return_txt:
            if txt_candidate:
                txt_text = txt_candidate
            else:
                txt_text = build_plain_text(results)

        return {"result": results, "txt": txt_text}


def _child_worker(
    q: multiprocessing.Queue, path: str, pipeline: str, options: Optional[Dict[str, object]]
) -> None:
    """Wrapper run in a separate process to enforce a hard timeout."""  # pragma: no cover
    _configure_parse_child_process()
    try:
        data = _actual_parse(path, pipeline, options)
        q.put({"ok": True, "data": data})
    except Exception as exc:  # noqa: BLE001 - propagate failure info through queue
        q.put({"ok": False, "error": str(exc)})
    finally:
        # multiprocessing waits for nested children before normal atexit hooks.
        # Close only this task's already-loaded render pool, while the watchdog
        # remains responsible for hard timeouts and failed cleanup.
        images = sys.modules.get("docvortex.document.pdf.images")
        if images is not None:
            try:
                images.shutdown_pdf_render_executor()
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "PDF render cleanup failed (%s); watchdog will finish cleanup",
                    type(exc).__name__,
                )


def _worker_process_file(
    file_path: str,
    pipeline: str,
    options: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Run MinerU parsing with a per-task hard timeout using an isolated child process.

    This prevents a single stuck PDF from blocking the GPU worker forever.
    Env variables (seconds):
      MINERU_TASK_HARD_TIMEOUT_SECONDS (global fallback, default 600)
      MINERU_SCI_HARD_TIMEOUT_SECONDS (pipeline == 'sci')
      MINERU_IMAGES_HARD_TIMEOUT_SECONDS (pipeline == 'images')
      MINERU_DEFAULT_HARD_TIMEOUT_SECONDS (pipeline == 'default')
    """
    # Resolve hard timeout
    global_default = int(os.getenv("MINERU_TASK_HARD_TIMEOUT_SECONDS", "600"))
    if pipeline == "sci":
        hard_timeout = int(os.getenv("MINERU_SCI_HARD_TIMEOUT_SECONDS", str(global_default)))
    elif pipeline == "images":
        hard_timeout = int(os.getenv("MINERU_IMAGES_HARD_TIMEOUT_SECONDS", str(global_default)))
    else:
        hard_timeout = int(os.getenv("MINERU_DEFAULT_HARD_TIMEOUT_SECONDS", str(global_default)))

    result_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)

    proc = multiprocessing.Process(
        target=_child_worker,
        args=(result_queue, file_path, pipeline, options),
        daemon=False,  # allow downstream libraries to spawn worker processes
    )
    proc.start()

    try:
        try:
            msg = result_queue.get(timeout=hard_timeout)
        except queue.Empty:
            # Timeout -> kill child
            _cleanup_child_process(proc, terminate=True)
            raise TimeoutError(f"Parse hard timeout after {hard_timeout}s (pipeline={pipeline})")

        if not msg.get("ok"):
            raise RuntimeError(msg.get("error", "Unknown parse error"))
        payload = msg.get("data")
        if isinstance(payload, list):
            payload = {"result": payload}
        return payload
    finally:
        _cleanup_child_process(proc, terminate=False)
        result_queue.close()
        result_queue.join_thread()


@dataclass
class _GPUExecutor:
    gpu_id: str
    pool: ProcessPoolExecutor
    pending: int = 0


class GPUScheduler:
    """A simple GPU-aware scheduler: one worker process per GPU, queued tasks per GPU.

    - Set env GPU_IDS="0,1,2" (default: "0") to control GPUs used.
    - Each GPU runs one task at a time; additional tasks on that GPU queue automatically.
    """

    def __init__(self):
        gpu_ids_env = os.getenv("GPU_IDS")
        if gpu_ids_env:
            gpu_ids = [gid.strip() for gid in gpu_ids_env.split(",") if gid.strip()]
        else:
            # Conservative default: single GPU 0
            gpu_ids = ["0"]

        self._executors: List[_GPUExecutor] = [
            _GPUExecutor(
                gpu_id=gid,
                pool=ProcessPoolExecutor(max_workers=1, initializer=_worker_init, initargs=(gid,)),
            )
            for gid in gpu_ids
        ]
        if not self._executors:
            raise RuntimeError(
                "No GPUs configured. Set GPU_IDS environment variable, e.g., '0,1,2'."
            )

        self._lock = Lock()
        self._closed = False

    def _pick_executor(self) -> _GPUExecutor:
        """Pick the GPU with the smallest pending queue."""
        with self._lock:
            if self._closed:
                raise RuntimeError("GPU scheduler is shut down")
            exec_ = min(self._executors, key=lambda e: e.pending)
            exec_.pending += 1
            return exec_

    def submit(
        self,
        file_path: str,
        pipeline: str = "default",
        **task_options: object,
    ) -> Future:
        """Submit a file for processing; returns a Future yielding a JSON-serializable dict."""
        exec_ = self._pick_executor()

        def _done_cb(_fut: Future):
            with self._lock:
                exec_.pending -= 1

        fut = exec_.pool.submit(_worker_process_file, file_path, pipeline, task_options or None)
        fut.add_done_callback(_done_cb)
        return fut

    def status(self) -> Dict[str, object]:
        with self._lock:
            gpus = [{"gpu_id": e.gpu_id, "pending": e.pending} for e in self._executors]
            total_pending = sum(e.pending for e in self._executors)
        return {"gpus": gpus, "total_pending": total_pending}

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            executors = list(self._executors)

        for exec_ in executors:
            exec_.pool.shutdown(wait=wait, cancel_futures=True)


# Singleton scheduler
scheduler = GPUScheduler()
atexit.register(scheduler.shutdown)
