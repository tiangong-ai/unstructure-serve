import atexit
import ctypes
import logging
import math
import multiprocessing
import os
import signal
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, Future
from contextlib import suppress
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Any, Callable, Dict, List, Optional

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
    _run_child_job(q.put, _actual_parse, (path, pipeline, options), {})


def _run_child_job(publish, target, args, kwargs) -> None:
    _configure_parse_child_process()
    try:
        data = target(*args, **kwargs)
        publish({"ok": True, "data": data})
    except Exception as exc:  # noqa: BLE001 - propagate failure info through queue
        publish({"ok": False, "error": str(exc)})
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


def _pipe_child(sender, receiver, target, args, kwargs) -> None:
    # The parent closes its sender after start. No unrelated writer should keep
    # EOF hidden if the task dies before returning a result.
    receiver.close()
    try:
        _run_child_job(sender.send, target, args, kwargs)
    finally:
        sender.close()


def run_isolated_call(target: Callable[..., Any], *args, hard_timeout: float, **kwargs) -> Any:
    """Call a module-level function in a supervised task process group.

    Arguments/results must be pickleable. The deadline covers execution and
    result transfer. A receiver thread drains large pipe messages so the parent
    can still observe crashes/timeouts during transfer, without Queue feeder
    threads delaying child exit. Only this task's descendants are cleaned up.
    """
    if not math.isfinite(hard_timeout) or hard_timeout <= 0:
        raise ValueError("hard_timeout must be positive and finite")
    receiver, sender = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_pipe_child,
        args=(sender, receiver, target, args, kwargs),
        daemon=False,
    )
    try:
        process.start()
    except BaseException:
        receiver.close()
        sender.close()
        raise
    sender.close()
    received = Event()
    outcome = {}

    def receive():
        try:
            outcome["message"] = receiver.recv()
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            received.set()

    reader = Thread(target=receive, name="isolated-parse-result", daemon=True)
    reader.start()
    deadline = time.monotonic() + hard_timeout
    cleaned = False
    complete_message = False
    try:
        while not received.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Parse hard timeout after {hard_timeout:g}s")
            if received.wait(min(0.05, remaining)):
                break
            if process.exitcode is not None and not cleaned:
                # Descendants may have inherited the sender. Close those task
                # processes too, allowing the reader to observe EOF promptly.
                _cleanup_child_process(process, terminate=False)
                cleaned = True

        if "error" in outcome:
            process.join(timeout=0.1)
            raise RuntimeError(
                f"Isolated parse child exited without a complete result (exit code {process.exitcode})"
            ) from outcome["error"]
        message = outcome["message"]
        complete_message = True
        if not message.get("ok"):
            raise RuntimeError(message.get("error", "Unknown parse error"))
        return message["data"]
    finally:
        if not cleaned:
            _cleanup_child_process(process, terminate=not complete_message and process.is_alive())
        reader.join(timeout=1)
        receiver.close()
        process.close()


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

    payload = run_isolated_call(
        _actual_parse, file_path, pipeline, options, hard_timeout=hard_timeout
    )
    return {"result": payload} if isinstance(payload, list) else payload


@dataclass
class _GPUExecutor:
    gpu_id: str
    pool: ProcessPoolExecutor
    pending: int = 0


class GPUScheduler:
    """Isolated task dispatch; actual parsing shares host-wide capacity leases.

    GPU_IDS retains legacy child-environment routing. Multiple dispatch workers
    prevent HTTP connection affinity from serializing an otherwise idle host.
    """

    def __init__(self):
        gpu_ids_env = os.getenv("GPU_IDS")
        if gpu_ids_env:
            gpu_ids = [gid.strip() for gid in gpu_ids_env.split(",") if gid.strip()]
        else:
            # Conservative default: single GPU 0
            gpu_ids = ["0"]

        dispatch_workers = int(os.getenv("MINERU_SCHEDULER_WORKERS", "3"))
        if dispatch_workers < 1:
            raise ValueError("MINERU_SCHEDULER_WORKERS must be positive")
        self._executors: List[_GPUExecutor] = [
            _GPUExecutor(
                gpu_id=gid,
                pool=ProcessPoolExecutor(
                    max_workers=dispatch_workers, initializer=_worker_init, initargs=(gid,)
                ),
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
