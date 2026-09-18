"""Inspect, recover or collect local durable jobs. Collection defaults to preview."""

import argparse
import json
import math
import time

from src.services import job_store


def publish_existing_job(job_id):
    from src.services.job_submission import publish_existing_job as publish

    return publish(job_id)


def _records():
    for path in sorted(job_store.store_root().glob("*/job.json")):
        record = job_store.find_job(path.parent.name)
        if record:
            yield record


def _nonnegative_days(raw):
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("retention days must be finite and nonnegative")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List IDs and operational state, without document content")
    for name in ("recover", "resume"):
        sub = commands.add_parser(
            name,
            help=(
                "Republish uncertain submission"
                if name == "recover"
                else "Resume an inactive job with a new generation"
            ),
        )
        sub.add_argument("job_id")
    gc = commands.add_parser(
        "gc", help="Preview or remove inactive terminal jobs older than retention"
    )
    gc.add_argument("--retention-days", type=_nonnegative_days, default=7.0)
    gc.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "list":
        result = {
            "jobs": [
                {
                    key: value
                    for key, value in job_store.status(record["job_id"]).items()
                    if key != "error"
                }
                for record in _records()
            ]
        }
    elif args.command == "gc":
        seconds = args.retention_days * 86400
        candidates = [
            record["job_id"]
            for record in _records()
            if job_store.status(record["job_id"])["state"] in {"SUCCESS", "FAILURE"}
            and time.time() - job_store.retention_timestamp(record) >= seconds
        ]
        result = {"candidates": candidates, "removed": [], "preview": not args.apply}
        if args.apply:
            result["removed"] = [
                job_id
                for job_id in candidates
                if job_store.collect_job(job_id, retention_seconds=seconds)
            ]
    else:
        try:
            record = job_store.read_job(args.job_id)
            if args.command == "resume":
                job_store.resume_job(args.job_id)
                publish_existing_job(args.job_id)
            elif record["publication"] != "published" and job_store.status(args.job_id)[
                "state"
            ] not in {"SUCCESS", "EXPIRED"}:
                publish_existing_job(args.job_id)
            result = job_store.status(args.job_id)
            result.pop("error", None)
        except (
            job_store.JobBusy,
            job_store.JobConflict,
            job_store.PublishUncertain,
            FileNotFoundError,
            ValueError,
        ) as exc:
            # Do not print exception chains containing broker URLs or document paths.
            parser.exit(1, f"{type(exc).__name__}: job action could not be completed\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
