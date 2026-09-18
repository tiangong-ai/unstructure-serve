"""Measure real vision requests against private image/context/check manifests."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import time


def main():
    from dotenv import load_dotenv

    load_dotenv()
    from src.services import vision_prompts
    from src.services.vision_prompts import build_vision_prompt, build_vision_messages
    from src.services.vision_service_openai_compatible import encode_image
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
    for case in cases:
        for pattern in [*case.get("required", []), *case.get("forbidden", [])]:
            re.compile(pattern)
        with Path(case["image"]).open("rb") as stream:
            case["image_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    if args.prompt_file:
        vision_prompts.DEFAULT_VISION_PROMPT = args.prompt_file.read_text().strip()
    prompt = None
    model = _resolve_model(VisionProvider.VLLM, None)
    clients = _CLIENT_POOL.get_clients_in_priority_order()
    if not clients:
        parser.error("No configured vLLM vision endpoints")
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cases.json").write_text(
        json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def run(index, repeat, case):
        started = time.perf_counter()
        row = {"case": case["name"], "repeat": repeat, "endpoint_index": index % len(clients)}
        try:
            response = clients[row["endpoint_index"]].chat.completions.create(
                model=model,
                messages=build_vision_messages(
                    case.get("context", ""),
                    prompt,
                    f"data:image/jpeg;base64,{encode_image(case['image'])}",
                ),
                extra_body=_build_extra_body(),
                seed=42 + repeat,
                **_build_request_options(),
            )
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
            futures = [
                pool.submit(run, i, repeat, case)
                for repeat in range(args.repetitions)
                for i, case in enumerate(cases)
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
        "seconds": time.perf_counter() - started,
        "requests": len(results),
        "passed": sum(row["ok"] for row in results),
        "completion_tokens": sum(
            row.get("usage", {}).get("completion_tokens", 0) for row in results
        ),
        "sampling": _build_request_options(),
        "extra_body": _build_extra_body(),
        "prompt": build_vision_prompt("", prompt),
    }
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "prompt"}))
    return 0 if report["passed"] == report["requests"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
