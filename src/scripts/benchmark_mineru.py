"""Benchmark warm, independent parser processes against actual PDFs.

Run as a module. Each output directory must be new; input and assets stay private.
This measures SDK parsing, excluding Celery queueing and extra vision descriptions.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import math
import multiprocessing
import os
from pathlib import Path
import time

_barrier = None


def _parse(job):
    from src.services.mineru_service_full import parse_doc
    import pypdfium2 as pdfium

    source, destination, submitted = job
    source = Path(source)
    started = time.perf_counter()
    items, artifact_dir, _ = parse_doc([source], Path(destination), tier="advanced")
    with pdfium.PdfDocument(str(source)) as document:
        pages = len(document)
    middle = json.loads((Path(artifact_dir) / "middle_json.json").read_text())
    assert middle["is_full_document"] is True
    assert {page["page_idx"] for page in middle["pages"]} == set(range(pages))
    assert items
    for item in items:
        if item.get("img_path"):
            assert (Path(artifact_dir) / item["img_path"]).stat().st_size > 0
    if source.name == "p2.pdf":
        tables = "\n".join(item.get("table_body", "") for item in items)
        assert "项目名称" in tables and "1600" in tables
        assert "■公开竞争" in tables.replace("☑", "■").replace(" ", "").replace("\n", "")
    ended = time.perf_counter()
    return {
        "source": source.name,
        "pid": os.getpid(),
        "pages": pages,
        "blocks": len(items),
        "service_seconds": ended - started,
        "queue_seconds": started - submitted,
        "completion_seconds": ended - submitted,
        "artifact_dir": artifact_dir,
    }


def _init_worker(concurrency, window, warmup, output, barrier):
    global _barrier
    _barrier = barrier
    os.environ["MINERU_MODEL_VLM_MAX_CONCURRENCY"] = str(concurrency)
    os.environ["MINERU_PROCESSING_WINDOW_SIZE"] = str(window)
    from docvortex.document.pdf.images import shutdown_pdf_render_executor

    try:
        _parse((warmup, str(Path(output) / f"warmup-{os.getpid()}"), time.perf_counter()))
        barrier.wait(timeout=300)
    except BaseException:
        barrier.abort()
        shutdown_pdf_render_executor()
        raise


def _ping():
    return os.getpid()


def _finish_worker():
    from docvortex.document.pdf.images import shutdown_pdf_render_executor

    # Nested multiprocessing workers cannot rely on the normal atexit hook.
    shutdown_pdf_render_executor()
    _barrier.wait(timeout=60)


def main():
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--window", type=int, default=64)
    parser.add_argument("--jobs", type=int, default=30)
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
        initargs=(args.concurrency, args.window, str(args.warmup), str(args.output), barrier),
    ) as pool:
        ready = [pool.submit(_ping) for _ in range(args.workers)]
        for future in ready:
            future.result(timeout=600)
        started = time.perf_counter()
        jobs = [
            (str(args.files[i % len(args.files)]), str(args.output / f"job-{i:03}"), started)
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
    summary = {
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
    }
    (args.output / "report.json").write_text(
        json.dumps({"summary": summary, "jobs": results}, indent=2)
    )
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
