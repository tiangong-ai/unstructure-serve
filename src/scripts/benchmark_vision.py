"""Measure real vision requests against private image/context/check manifests."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import random
import time


def build_request_plan(*, case_count: int, endpoint_count: int, repetitions: int):
    """Cross every case with every endpoint and seed in a repeatable mixed order."""
    if min(case_count, endpoint_count, repetitions) < 1:
        raise ValueError("Case, endpoint and repetition counts must be positive")
    plan = [
        (case, endpoint, repeat)
        for repeat in range(repetitions)
        for endpoint in range(endpoint_count)
        for case in range(case_count)
    ]
    random.Random(42).shuffle(plan)
    return plan


def main():
    from dotenv import load_dotenv

    load_dotenv()
    from src.services import vision_prompts
    from src.services.vision_prompts import build_vision_prompt
    from src.services.vision_service_openai_compatible import prepare_vision_request
    from src.services.vision_capacity import EndpointScheduler
    from src.services.vision_service_vllm import (
        _CLIENT_POOL,
        _build_extra_body,
        _build_request_options,
    )
    from src.services.vision_service import _resolve_model, VisionProvider
    from src.utils.text_output import sanitize_vision_text

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if min(args.concurrency, args.repetitions) < 1:
        parser.error("concurrency and repetitions must be positive")
    cases = json.loads(args.cases.read_text())
    if not cases:
        parser.error("No vision cases")
    if len({case["name"] for case in cases}) != len(cases):
        parser.error("Vision case names must be unique")
    for case in cases:
        for pattern in [*case.get("required", []), *case.get("forbidden", [])]:
            re.compile(pattern)
        with Path(case["image"]).open("rb") as stream:
            case["image_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    if args.prompt_file:
        vision_prompts.DEFAULT_VISION_PROMPT = args.prompt_file.read_text().strip()
    prompt = None
    model = _resolve_model(VisionProvider.VLLM, None)
    endpoint_clients = _CLIENT_POOL.get_endpoint_clients()
    if not endpoint_clients:
        parser.error("No configured vLLM vision endpoints")
    scheduler = EndpointScheduler.from_env()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cases.json").write_text(
        json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def run(endpoint_index, repeat, case):
        started = time.perf_counter()
        row = {
            "case": case["name"],
            "repeat": repeat,
            "endpoint_index": endpoint_index,
            "seed": 42 + repeat,
        }
        try:
            key, client = endpoint_clients[endpoint_index]
            request = prepare_vision_request(
                case["image"],
                context=case.get("context", ""),
                prompt=prompt,
                default_model=model,
                extra_body=_build_extra_body(),
                request_options={**_build_request_options(), "seed": 42 + repeat},
            )
            row["endpoint_id"] = key
            row["context_chars"] = len(case.get("context", ""))
            row["image_bytes"] = Path(case["image"]).stat().st_size
            queued = time.perf_counter()
            row["prepare_seconds"] = queued - started
            with scheduler.acquire([key]):
                row["capacity_wait_seconds"] = time.perf_counter() - queued
                call_started = time.perf_counter()
                response = client.chat.completions.create(**request)
                row["inference_seconds"] = time.perf_counter() - call_started
            if not response.choices or response.choices[0].finish_reason != "stop":
                raise RuntimeError("Incomplete vision response")
            raw = response.choices[0].message.content
            text = sanitize_vision_text(raw)
            row.update(
                raw=raw, text=text, usage=response.usage.model_dump() if response.usage else {}
            )
            row["missing"] = [
                pattern
                for pattern in case.get("required", [])
                if not re.search(pattern, text, re.I)
            ]
            row["forbidden"] = [
                pattern for pattern in case.get("forbidden", []) if re.search(pattern, text, re.I)
            ]
            row["ok"] = not row["missing"] and not row["forbidden"]
        except Exception as exc:
            row.update(ok=False, error=type(exc).__name__ + ": " + str(exc))
        row["seconds"] = time.perf_counter() - started
        return row

    started = time.perf_counter()
    results = []
    with (args.output / "requests.jsonl").open("x", encoding="utf-8") as journal:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            plan = build_request_plan(
                case_count=len(cases),
                endpoint_count=len(endpoint_clients),
                repetitions=args.repetitions,
            )
            futures = [
                pool.submit(run, endpoint, repeat, cases[index]) for index, endpoint, repeat in plan
            ]
            for future in as_completed(futures):
                row = future.result()
                results.append(row)
                journal.write(json.dumps(row, ensure_ascii=False) + "\n")
                journal.flush()
    report = {
        "model": model,
        "concurrency": args.concurrency,
        "repetitions": args.repetitions,
        "endpoint_count": len(endpoint_clients),
        "endpoint_ids": [key for key, _ in endpoint_clients],
        "design": "every case x endpoint x repetition; shuffled with seed 42",
        "case_manifest_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "seconds": time.perf_counter() - started,
        "requests": len(results),
        "passed": sum(row["ok"] for row in results),
        "completion_tokens": sum(
            row.get("usage", {}).get("completion_tokens", 0) for row in results
        ),
        "sampling": _build_request_options(),
        "extra_body": _build_extra_body(),
        "prompt": build_vision_prompt("", prompt),
        "endpoint_results": [
            {
                "endpoint_index": index,
                "requests": sum(row["endpoint_index"] == index for row in results),
                "passed": sum(row["endpoint_index"] == index and row["ok"] for row in results),
            }
            for index in range(len(endpoint_clients))
        ],
    }
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "prompt"}))
    return 0 if report["passed"] == report["requests"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
