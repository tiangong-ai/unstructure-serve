"""Benchmark warm, independent parser processes against actual PDFs.

Run as a module. Each output directory must be new; input and assets stay private.
This measures SDK parsing, excluding Celery queueing and extra vision descriptions.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
from importlib.metadata import version
import json
import logging
import math
import multiprocessing
import os
from pathlib import Path
import platform
import sys
import time

_barrier = None


def _runtime_metadata():
    from src.services.mineru_service_full import get_mineru_config

    config = get_mineru_config()
    return {
        "python": platform.python_version(),
        "platform": sys.platform,
        "packages": {
            package: version(package)
            for package in (
                "mineru",
                "docvortex",
                "mineru-vl-utils",
                "onnxruntime",
                "pypdfium2",
                "pillow",
            )
        },
        "small_backend": config.model.small_backend,
        "onnx_device": config.model.onnx_device,
        "vlm_engine_config": config.model.vlm.engine,
        "vlm_model": config.model.vlm.model,
        "vlm_max_concurrency": config.model.vlm.max_concurrency,
        "vlm_http_timeout": config.model.vlm.http_timeout,
        "ocr_mode": os.getenv("MINERU_DEFAULT_METHOD", "").strip() or "auto",
    }


def _parse(job):
    from src.services.mineru_service_full import parse_doc
    import pypdfium2 as pdfium
    from PIL import Image

    source, destination, submitted, tier = job
    source = Path(source)
    started = time.perf_counter()
    items, artifact_dir, _ = parse_doc([source], Path(destination), tier=tier)
    parsed = time.perf_counter()
    with pdfium.PdfDocument(str(source)) as document:
        pages = len(document)
    middle = json.loads((Path(artifact_dir) / "middle_json.json").read_text())
    assert middle["is_full_document"] is True
    assert {page["page_idx"] for page in middle["pages"]} == set(range(pages))
    assert items
    decoded_images = set()
    for item in items:
        if item.get("img_path"):
            image_path = Path(artifact_dir) / item["img_path"]
            if image_path not in decoded_images:
                with Image.open(image_path) as image:
                    image.load()
                    assert image.width > 0 and image.height > 0
                decoded_images.add(image_path)
    if source.name == "p2.pdf":
        tables = "\n".join(item.get("table_body", "") for item in items)
        assert "项目名称" in tables and "1600" in tables
        assert "■公开竞争" in tables.replace("☑", "■").replace(" ", "").replace("\n", "")
    with source.open("rb") as stream:
        source_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    ended = time.perf_counter()
    return {
        "source": source.name,
        "source_sha256": source_sha256,
        "source_bytes": source.stat().st_size,
        "tier": tier,
        "pid": os.getpid(),
        "pages": pages,
        "blocks": len(items),
        "decoded_images": len(decoded_images),
        "parse_seconds": parsed - started,
        "service_seconds": ended - started,
        "queue_seconds": started - submitted,
        "completion_seconds": ended - submitted,
        "artifact_dir": artifact_dir,
        "runtime": _runtime_metadata(),
    }


def _cleanup_worker():
    # Only close resources this worker has loaded, before interpreter teardown.
    # An SDK cleanup failure must not skip the nested PDF rendering pool.
    for name, method in (
        ("mineru.model.vlm.runtime", "shutdown_cached_models"),
        ("docvortex.document.pdf.images", "shutdown_pdf_render_executor"),
    ):
        module = sys.modules.get(name)
        if module is not None:
            try:
                getattr(module, method)()
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Benchmark worker cleanup failed for %s (%s)", name, type(exc).__name__
                )


def _init_worker(concurrency, window, warmup, output, barrier, tier):
    global _barrier
    _barrier = barrier
    os.environ["MINERU_MODEL_VLM_MAX_CONCURRENCY"] = str(concurrency)
    os.environ["MINERU_PROCESSING_WINDOW_SIZE"] = str(window)
    try:
        warmup_dir = Path(output) / f"warmup-{os.getpid()}"
        result = _parse((warmup, str(warmup_dir), time.perf_counter(), tier))
        warmup_dir.mkdir(parents=True, exist_ok=True)
        (warmup_dir / "report.json").write_text(json.dumps(result, indent=2))
        barrier.wait(timeout=300)
    except BaseException:
        barrier.abort()
        _cleanup_worker()
        raise


def _ping():
    return os.getpid()


def _finish_worker():
    # Nested multiprocessing workers cannot rely on the normal atexit hook.
    _cleanup_worker()
    _barrier.wait(timeout=60)


def main():
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--window", type=int, default=64)
    parser.add_argument("--jobs", type=int, default=30)
    parser.add_argument(
        "--tier", choices=("flash", "basic", "standard", "advanced"), default="advanced"
    )
    parser.add_argument("--files", nargs="+", type=Path, default=[Path("input/p2.pdf")])
    parser.add_argument("--warmup", type=Path, default=Path("input/p2.pdf"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.workers, args.concurrency, args.window, args.jobs) < 1:
        parser.error("workers, concurrency, window and jobs must be positive")
    for path in [*args.files, args.warmup]:
        if not path.is_file():
            parser.error(f"Missing input PDF: {path}")
    args.output.mkdir(parents=True, exist_ok=False)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(args.workers)
    with ProcessPoolExecutor(
        max_workers=args.workers,
        mp_context=context,
        initializer=_init_worker,
        initargs=(
            args.concurrency,
            args.window,
            str(args.warmup),
            str(args.output),
            barrier,
            args.tier,
        ),
    ) as pool:
        ready = [pool.submit(_ping) for _ in range(args.workers)]
        for future in ready:
            future.result(timeout=600)
        started = time.perf_counter()
        jobs = [
            (
                str(args.files[i % len(args.files)]),
                str(args.output / f"job-{i:03}"),
                started,
                args.tier,
            )
            for i in range(args.jobs)
        ]
        try:
            results = list(pool.map(_parse, jobs))
            elapsed = time.perf_counter() - started
        finally:
            finished = [pool.submit(_finish_worker) for _ in range(args.workers)]
            for future in finished:
                future.result(timeout=120)
    service = sorted(item["service_seconds"] for item in results)
    completed = sorted(item["completion_seconds"] for item in results)
    warmup_jobs = [
        json.loads(path.read_text()) for path in sorted(args.output.glob("warmup-*/report.json"))
    ]
    summary = {
        "tier": args.tier,
        "workers": args.workers,
        "concurrency": args.concurrency,
        "onnx_intra_threads": os.getenv("MINERU_INTRA_OP_NUM_THREADS", "auto"),
        "onnx_inter_threads": os.getenv("MINERU_INTER_OP_NUM_THREADS", "auto"),
        "window": args.window,
        "jobs": args.jobs,
        "elapsed_seconds": elapsed,
        "pages_per_second": sum(item["pages"] for item in results) / elapsed,
        "service_p50_seconds": service[math.ceil(len(service) * 0.5) - 1],
        "service_p95_seconds": service[math.ceil(len(service) * 0.95) - 1],
        "completion_p95_seconds": completed[math.ceil(len(completed) * 0.95) - 1],
        "worker_pids": sorted({item["pid"] for item in results}),
        "warmup": {
            "parses_per_worker": 1,
            "included_in_elapsed": False,
            "source": args.warmup.name,
            "worker_pids": sorted({item["pid"] for item in warmup_jobs}),
        },
        "measurement_scope": "warm SDK processes; includes output validation; excludes Celery and extra vision",
    }
    (args.output / "report.json").write_text(
        json.dumps({"summary": summary, "warmup_jobs": warmup_jobs, "jobs": results}, indent=2)
    )
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
