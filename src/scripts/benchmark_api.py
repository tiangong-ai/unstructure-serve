"""Compare real PDF HTTP latency and health responsiveness on loopback Gunicorn.

Uses the configured model service. Outputs are private; every candidate receives
identical documents and a fixed parse-capacity limit. Never uses the live API port.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time

import httpx


def percentile(values, fraction):
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * fraction))] if values else None


async def measure(base, source, jobs):
    health = []
    stop = asyncio.Event()
    async with httpx.AsyncClient(base_url=base, timeout=240) as client:

        async def parse():
            started = time.perf_counter()
            with source.open("rb") as file:
                response = await client.post(
                    "/mineru?return_txt=true",
                    files={"file": (source.name, file, "application/pdf")},
                )
            response.raise_for_status()
            payload = response.json()
            assert {item["page_number"] for item in payload["result"]} == {1, 2}
            assert "1600" in payload["txt"]
            return time.perf_counter() - started

        await parse()  # Warm the remote model; each isolated parser still loads its own state.

        async def poll():
            while not stop.is_set():
                started = time.perf_counter()
                response = await client.get("/health")
                response.raise_for_status()
                health.append(time.perf_counter() - started)
                await asyncio.sleep(0.1)

        monitor = asyncio.create_task(poll())
        started = time.perf_counter()
        try:
            durations = await asyncio.gather(*(parse() for _ in range(jobs)))
            elapsed = time.perf_counter() - started
        finally:
            stop.set()
            await monitor
    return {
        "batch_seconds": elapsed,
        "request_p50_seconds": statistics.median(durations),
        "request_p95_seconds": percentile(durations, 0.95),
        "health_samples": len(health),
        "health_p95_ms": percentile(health, 0.95) * 1000,
        "durations": durations,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", nargs="+", type=int, default=[2, 4, 8])
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--port", type=int, default=17771)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.port in {7770, 8770, 30000}:
        parser.error("Use a dedicated benchmark port")
    args.output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[2]
    source = root / "input/p2.pdf"
    assert source.is_file(), source
    report = {
        "python": sys.version,
        "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "jobs": args.jobs,
        "parse_slots": 3,
        "runs": [],
    }
    base = f"http://127.0.0.1:{args.port}"
    for workers in args.workers:
        env = {
            **os.environ,
            "API_BIND": f"127.0.0.1:{args.port}",
            "API_WORKERS": str(workers),
            "FASTAPI_AUTH": "false",
            "MINERU_PARSE_SLOTS": "3",
            "MINERU_INTRA_OP_NUM_THREADS": "16",
            "MINERU_INTER_OP_NUM_THREADS": "1",
            "MINERU_PROCESSING_WINDOW_SIZE": "64",
        }
        with (args.output / f"workers-{workers}.log").open("w") as log:
            proc = subprocess.Popen(
                [str(root / ".venv/bin/gunicorn"), "-c", "deploy/gunicorn.conf.py", "src.main:app"],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                for _ in range(100):
                    if proc.poll() is not None:
                        raise RuntimeError("Benchmark API exited during startup")
                    try:
                        if httpx.get(base + "/health", timeout=1).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise TimeoutError("Benchmark API startup timed out")
                result = asyncio.run(measure(base, source, args.jobs))
                report["runs"].append({"workers": workers, **result})
                (args.output / "report.json").write_text(json.dumps(report, indent=2))
                print(json.dumps(report["runs"][-1]), flush=True)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()


if __name__ == "__main__":
    main()
